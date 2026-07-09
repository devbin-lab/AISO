"""하네스 웹 검증 — 에이전트가 만든 HTML을 헤드리스 브라우저로 실제 실행해
콘솔/런타임 에러를 수집한다. "생성 → 실행·검증 → 수리" 루프의 검증 단계.

⚠️ Windows + uvicorn 이벤트 루프에서는 asyncio 서브프로세스가 막혀(NotImplementedError)
Playwright의 async API로 브라우저를 못 띄운다. 그래서 동기 API(sync_playwright)를
별도 스레드(asyncio.to_thread)에서 실행한다 — 이벤트 루프 정책과 무관하게 동작한다.

시스템에 설치된 Edge(채널=msedge)를 사용해 별도 브라우저 다운로드가 필요 없다.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from tools import ToolError, _resolve

# 캔버스가 실제로 그려졌는지 검사 (모든 픽셀이 같으면 = 빈 화면 = 렌더링 안 됨)
_CANVAS_PROBE = """() => {
  const c = document.querySelector('canvas');
  let blank = false;
  if (c) {
    try {
      const ctx = c.getContext('2d');
      const w = Math.min(c.width, 160), h = Math.min(c.height, 160);
      if (ctx && w > 0 && h > 0) {
        const d = ctx.getImageData(0, 0, w, h).data;
        blank = true;
        for (let i = 4; i < d.length; i += 4) {
          if (d[i] !== d[0] || d[i+1] !== d[1] || d[i+2] !== d[2] || d[i+3] !== d[3]) { blank = false; break; }
        }
      }
    } catch (e) { /* getImageData 실패 시 판단 보류 */ }
  }
  return { canvas: !!c, blank, nodes: document.body ? document.body.querySelectorAll('*').length : 0 };
}"""

RUN_WEB_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_web",
        "description": (
            "작성한 HTML 파일을 헤드리스 브라우저로 실제 실행해 콘솔/런타임 에러를 검사한다. "
            "웹 산출물(HTML/JS)을 만들거나 고친 뒤에는 반드시 이 툴로 검증하고, 에러가 있으면 "
            "고쳐서 다시 검증하라. "
            "**게임처럼 키보드로 조작하는 페이지는 에러 0건·화면 렌더링만으로는 부족하다 — "
            "actions로 실제 방향키를 눌러보고 화면이 그 방향에 맞게 반응하는지까지 검증하라.** "
            "actions를 주면 키보드 리스너 등록 여부와, 눌린 방향 쪽으로 실제 변화가 쏠리는지(방향성)를 "
            "좌/우·상/하 화면 변화 비율로 알려준다 — 좌우를 다 눌러봤는데 변화가 비슷하면 이동이 "
            "실제로는 반영되지 않는 버그일 가능성이 높다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "작업 폴더 기준 HTML 파일 경로."},
                "actions": {
                    "type": "array",
                    "description": (
                        "선택. 로드 후 실제로 눌러볼 키 입력 순서. 게임 조작 검증에 사용. "
                        "예: [{\"key\":\"ArrowLeft\",\"times\":10},{\"key\":\"ArrowRight\",\"times\":10},"
                        "{\"key\":\"ArrowUp\",\"times\":3}]. key는 'ArrowLeft/Right/Up/Down', 'Space', "
                        "'Enter', 'KeyA' 같은 표준 키 이름(또는 'left'/'right' 등 방향 단어)도 된다."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "누를 키."},
                            "times": {"type": "integer", "description": "누를 횟수(기본 1, 최대 40)."},
                        },
                        "required": ["key"],
                    },
                },
            },
            "required": ["path"],
        },
    },
}

# 방향 판정용 키 별칭 그룹 (좌/우, 상/하 변화 비교에 사용)
_LEFT_KEYS = {"arrowleft", "left", "a", "keya"}
_RIGHT_KEYS = {"arrowright", "right", "d", "keyd"}
_UP_KEYS = {"arrowup", "up", "w", "keyw"}
_DOWN_KEYS = {"arrowdown", "down", "s", "keys"}
_KEY_ALIASES = {
    "left": "ArrowLeft", "right": "ArrowRight", "up": "ArrowUp", "down": "ArrowDown",
    "space": "Space", "spacebar": "Space", "enter": "Enter", "return": "Enter",
}

MAX_ACTIONS = 12
MAX_TIMES_PER_ACTION = 40
MAX_TOTAL_PRESSES = 200
PRESS_DELAY_MS = 25

# 페이지 스크립트가 실행되기 전에 주입 — addEventListener 호출을 가로채 어떤 이벤트
# 타입이 리스닝되는지 기록한다 (키보드 입력을 아예 안 받는 페이지를 값싸게, 오탐 없이 잡아낸다).
_LISTENER_SNIFFER_JS = """
(() => {
  window.__aisoListened = {};
  const orig = EventTarget.prototype.addEventListener;
  EventTarget.prototype.addEventListener = function(type, ...rest) {
    window.__aisoListened[type] = (window.__aisoListened[type] || 0) + 1;
    return orig.call(this, type, ...rest);
  };
})();
"""

_BASELINE_JS = """() => {
  const c = document.querySelector('canvas');
  if (!c) return false;
  const ctx = c.getContext('2d');
  if (!ctx) return false;
  window.__aisoBaseline = ctx.getImageData(0, 0, c.width, c.height).data;
  return true;
}"""

# 베이스라인 대비 변화한 픽셀을, 좌/우·상/하 절반으로 나눠 센다. 방향키를 눌렀을 때
# '눌린 쪽으로 변화가 쏠리는지'를 이 좌/우(상/하) 비율 차이로 판단한다.
_DIFF_JS = """() => {
  const c = document.querySelector('canvas');
  if (!c || !window.__aisoBaseline) return null;
  const ctx = c.getContext('2d');
  const cur = ctx.getImageData(0, 0, c.width, c.height).data;
  const base = window.__aisoBaseline;
  const w = c.width, h = c.height, midX = w / 2, midY = h / 2;
  let total = 0, changed = 0;
  let leftT = 0, leftC = 0, rightT = 0, rightC = 0, topT = 0, topC = 0, bottomT = 0, bottomC = 0;
  for (let y = 0; y < h; y++) {
    for (let x = 0; x < w; x++) {
      const i = (y * w + x) * 4;
      const d = Math.abs(cur[i] - base[i]) + Math.abs(cur[i+1] - base[i+1]) + Math.abs(cur[i+2] - base[i+2]);
      const ch = d > 24;
      total++; if (ch) changed++;
      if (x < midX) { leftT++; if (ch) leftC++; } else { rightT++; if (ch) rightC++; }
      if (y < midY) { topT++; if (ch) topC++; } else { bottomT++; if (ch) bottomC++; }
    }
  }
  return {total, changed, leftT, leftC, rightT, rightC, topT, topC, bottomT, bottomC};
}"""


def _normalize_key(k: str) -> str:
    return _KEY_ALIASES.get(k.strip().lower(), k.strip())


def _clamp_actions(raw: object) -> list[tuple[str, int]]:
    """actions 입력을 검증·상한 적용해 (키, 횟수) 목록으로 정리한다."""
    out: list[tuple[str, int]] = []
    if not isinstance(raw, list):
        return out
    total = 0
    for item in raw[:MAX_ACTIONS]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        try:
            times = int(item.get("times") or 1)
        except (TypeError, ValueError):
            times = 1
        times = max(1, min(times, MAX_TIMES_PER_ACTION))
        if total + times > MAX_TOTAL_PRESSES:
            times = MAX_TOTAL_PRESSES - total
        if times <= 0:
            break
        out.append((key, times))
        total += times
    return out


def _run_action_probe(page, clamped: list[tuple[str, int]]) -> str:
    """실제로 키를 눌러보고, 리스너 등록 여부 + 방향별 화면 변화를 리포트 텍스트로 만든다."""
    try:
        box = page.locator("canvas").bounding_box()  # 일부 게임은 캔버스 포커스가 있어야 입력을 받는다
        if box:
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    except Exception:  # noqa: BLE001
        pass

    has_baseline = False
    try:
        has_baseline = bool(page.evaluate(_BASELINE_JS))
    except Exception:  # noqa: BLE001
        pass

    tested_left = tested_right = tested_up = tested_down = False
    for raw_key, times in clamped:
        lo = raw_key.lower()
        tested_left |= lo in _LEFT_KEYS
        tested_right |= lo in _RIGHT_KEYS
        tested_up |= lo in _UP_KEYS
        tested_down |= lo in _DOWN_KEYS
        norm = _normalize_key(raw_key)
        for _ in range(times):
            try:
                page.keyboard.press(norm)
            except Exception:  # noqa: BLE001 — 알 수 없는 키 이름 등은 조용히 건너뛴다
                pass
            page.wait_for_timeout(PRESS_DELAY_MS)
    page.wait_for_timeout(300)

    lines = ["[조작 테스트] 입력: " + ", ".join(f"{k}×{t}" for k, t in clamped)]
    try:
        listened = page.evaluate("window.__aisoListened || {}")
    except Exception:  # noqa: BLE001
        listened = {}
    kb_types = [t for t in ("keydown", "keyup", "keypress") if listened.get(t)]
    if not kb_types:
        lines.append("  ❌ 키보드 리스너가 전혀 등록되지 않았습니다 — 키를 눌러도 반응하지 않을 가능성이 매우 높습니다.")
    else:
        lines.append(f"  키보드 리스너 등록됨: {', '.join(kb_types)}")

    diff = page.evaluate(_DIFF_JS) if has_baseline else None
    if diff and diff.get("total"):
        overall = diff["changed"] / diff["total"] * 100
        lp = diff["leftC"] / diff["leftT"] * 100 if diff["leftT"] else 0.0
        rp = diff["rightC"] / diff["rightT"] * 100 if diff["rightT"] else 0.0
        tp = diff["topC"] / diff["topT"] * 100 if diff["topT"] else 0.0
        bp = diff["bottomC"] / diff["bottomT"] * 100 if diff["bottomT"] else 0.0
        lines.append(f"  전체 화면 변화: {overall:.1f}%")
        if tested_left or tested_right:
            lines.append(f"  좌측 변화 {lp:.1f}% vs 우측 변화 {rp:.1f}%")
            if tested_left and tested_right and overall > 3 and abs(lp - rp) < 6:
                lines.append(
                    "  ⚠ 좌/우 방향키를 둘 다 눌렀는데 좌우 변화량이 비슷합니다 — "
                    "이동이 실제로 반영되지 않고 있을 가능성이 있습니다. "
                    "이동·충돌 판정 로직(좌표가 정수인지 등)을 점검하고 다시 검증하라."
                )
        if tested_up or tested_down:
            lines.append(f"  상단 변화 {tp:.1f}% vs 하단 변화 {bp:.1f}%")
    else:
        lines.append("  (캔버스 변화를 측정하지 못했습니다)")
    return "\n" + "\n".join(lines)


def _run_web_sync(target: Path, rel: str, actions: list | None = None) -> tuple[str, str | None]:
    """별도 스레드에서 실행되는 동기 검증. (리포트, 스크린샷 base64) 반환."""
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    title = ""
    node_count = 0
    has_canvas = False
    blank = False
    shot: str | None = None
    action_section = ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page(viewport={"width": 900, "height": 620})
                page.add_init_script(_LISTENER_SNIFFER_JS)  # 페이지 스크립트 실행 전에 리스너 감지기를 심는다
                page.on(
                    "console",
                    lambda m: errors.append(f"[콘솔에러] {m.text}") if m.type == "error" else None,
                )
                page.on("pageerror", lambda e: errors.append(f"[예외] {e}"))
                try:
                    page.goto(target.as_uri(), wait_until="load", timeout=15000)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"[로드 실패] {e!r}")
                page.wait_for_timeout(1500)  # JS 실행 시간
                try:
                    title = page.title()
                    info = page.evaluate(_CANVAS_PROBE)
                    has_canvas = bool(info.get("canvas"))
                    node_count = int(info.get("nodes") or 0)
                    blank = bool(info.get("blank"))
                    shot = base64.b64encode(page.screenshot(type="png")).decode()
                except Exception:  # noqa: BLE001
                    pass

                clamped = _clamp_actions(actions) if actions else []
                if clamped and has_canvas:
                    try:
                        action_section = _run_action_probe(page, clamped)
                    except Exception as e:  # noqa: BLE001 — 조작 테스트 실패는 기본 검증 결과를 덮지 않는다
                        action_section = f"\n[조작 테스트 실패] {type(e).__name__}: {e}"
                elif clamped and not has_canvas:
                    action_section = "\n[조작 테스트 건너뜀] 캔버스가 없는 페이지입니다."
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 — 브라우저 자체가 안 뜨는 경우 (타입까지 보고)
        return (
            f"[검증 불가] 헤드리스 브라우저 실행 실패: {type(e).__name__}: {e}. "
            "Edge 설치 또는 playwright 설정을 확인하세요 (파일 내용 자체는 정상일 수 있음).",
            None,
        )

    seen: list[str] = []
    for e in errors:
        if e not in seen:
            seen.append(e)

    if seen:
        lines = "\n".join(f"  - {e[:220]}" for e in seen[:15])
        more = f"\n  … 외 {len(seen) - 15}건" if len(seen) > 15 else ""
        report = (
            f"❌ {rel} 실행 결과: 에러 {len(seen)}건 (title={title!r}, DOM 요소 {node_count}개).\n"
            f"{lines}{more}\n"
            "→ 위 에러의 원인을 파일에서 고친 뒤, run_web으로 다시 검증하라."
        )
    elif has_canvas and blank:
        report = (
            f"⚠ {rel} 로드는 됐지만 캔버스가 비어있습니다(그림이 전혀 안 그려짐). "
            f"콘솔 에러는 0건이나 렌더링/그리기 로직에 문제가 있을 수 있습니다. "
            "draw/render 호출과 좌표·색상을 점검해 다시 검증하라."
        )
    else:
        report = (
            f"✅ {rel} 정상 실행: 콘솔/런타임 에러 0건 "
            f"(title={title!r}, canvas={has_canvas}, DOM 요소 {node_count}개). "
            "페이지가 오류 없이 렌더링됩니다."
        )
    return report + action_section, shot


async def run_web(root: Path, path: str, actions: list | None = None, **_ignore) -> tuple[str, str | None]:
    """HTML을 실행해 (에러 리포트, 스크린샷 base64) 반환. 경로 검증은 여기서."""
    target = _resolve(root, path)
    if not target.is_file():
        raise ToolError(f"파일이 없습니다: {path}")
    if target.suffix.lower() not in (".html", ".htm"):
        raise ToolError("HTML 파일만 실행·검사할 수 있습니다.")
    return await asyncio.to_thread(_run_web_sync, target, path, actions)
