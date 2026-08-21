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
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, cast
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from process_env import sanitized_child_environment
from tools import ToolError, _resolve, is_path_within


_LOG = logging.getLogger(__name__)

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
            "작성한 HTML 파일을 격리된 로컬 Edge에서 실행해 콘솔·런타임 오류와 명시한 상호작용을 검사한다. "
            "단순 로드 통과는 기능 정상의 증거가 아니다. 버튼·키보드·게임 상태가 있는 페이지는 steps 한 번에 "
            "click/press/wait와 기대 조건을 묶어 실행하고, 실패한 조건만 고친 뒤 같은 시나리오를 재검증한다. "
            "여러 동작을 각각 run_web으로 호출하지 마라. 조작 후 마지막 화면이 스크린샷으로 반환된다. "
            "게임 내부 상태 검증이 필요하면 읽기 전용 window.__AISO_TEST__ 값을 만들고 state 단언으로 읽는다. "
            "steps 없이 통과한 결과는 기본 실행 검사일 뿐이며 전체 기능이 검증됐다고 주장하지 마라."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "작업 폴더 기준 HTML 파일 경로."},
                "actions": {
                    "type": "array",
                    "maxItems": 12,
                    "description": (
                        "이전 버전 호환용 키 입력 목록. 새 검증은 steps 사용을 권장한다. "
                        "예: [{\"key\":\"ArrowLeft\",\"times\":10},{\"key\":\"ArrowRight\",\"times\":10},"
                        "{\"key\":\"ArrowUp\",\"times\":3}]. key는 'ArrowLeft/Right/Up/Down', 'Space', "
                        "'Enter', 'KeyA' 같은 표준 키 이름(또는 'left'/'right' 등 방향 단어)도 된다."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "누를 키."},
                            "times": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 40,
                                "description": "누를 횟수(기본 1, 최대 40).",
                            },
                        },
                        "required": ["key"],
                    },
                },
                "steps": {
                    "type": "array",
                    "maxItems": 24,
                    "description": (
                        "선택. 한 번의 로컬 브라우저 실행에서 순서대로 수행할 조작·단언(최대 24단계). "
                        "click은 by=role(role+name), testid(value), css(selector), text(value)를 사용하며 "
                        "대상 내부 좌표는 x_ratio/y_ratio(0~1)로 지정한다. "
                        "press는 key/times, wait는 ms를 사용한다. assert는 visible, text, state, "
                        "canvas_changed, canvas_unchanged, no_console_errors를 지원한다. state.path는 "
                        "window.__AISO_TEST__.state 같은 안전한 점 표기 경로만 허용한다."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["click", "press", "wait"]},
                            "assert": {
                                "type": "string",
                                "enum": [
                                    "visible", "text", "state", "canvas_changed",
                                    "canvas_unchanged", "no_console_errors",
                                ],
                            },
                            "by": {"type": "string", "enum": ["role", "testid", "css", "text"]},
                            "role": {"type": "string", "description": "접근성 역할(예: button)."},
                            "name": {"type": "string", "description": "접근성 표시 이름."},
                            "value": {"type": "string", "description": "testid 또는 표시 텍스트."},
                            "selector": {"type": "string", "description": "작업 페이지 내부 CSS 선택자."},
                            "x_ratio": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                                "description": "클릭 대상 내부 가로 상대 좌표(0~1). y_ratio와 함께 사용.",
                            },
                            "y_ratio": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                                "description": "클릭 대상 내부 세로 상대 좌표(0~1). x_ratio와 함께 사용.",
                            },
                            "key": {"type": "string", "description": "Playwright 표준 키 이름."},
                            "times": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 40,
                                "description": "키 입력 횟수(기본 1, 최대 40).",
                            },
                            "ms": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 3000,
                                "description": "대기 시간(ms, 단계당 최대 3000).",
                            },
                            "path": {"type": "string", "description": "읽기 전용 상태의 안전한 점 표기 경로."},
                            "equals": {"description": "정확히 같아야 하는 JSON 값 또는 텍스트."},
                            "contains": {"type": "string", "description": "포함해야 하는 텍스트."},
                        },
                    },
                },
            },
            "required": ["path"],
        },
    },
}

_KEY_ALIASES = {
    "left": "ArrowLeft", "right": "ArrowRight", "up": "ArrowUp", "down": "ArrowDown",
    "space": "Space", "spacebar": "Space", "enter": "Enter", "return": "Enter",
}

MAX_ACTIONS = 12
MAX_TIMES_PER_ACTION = 40
MAX_TOTAL_PRESSES = 200
PRESS_DELAY_MS = 25
MAX_STEPS = 24
MAX_CLICKS = 20
MAX_WAIT_MS = 3000
MAX_TOTAL_WAIT_MS = 10_000
MAX_ERROR_SAMPLES = 20
MAX_BLOCKED_URL_SAMPLES = 10
MAX_WORKER_OUTPUT_BYTES = 8 * 1024 * 1024
# 검증 전체 벽시계 상한(초). 페이지 JS가 무한 루프에 빠지면 page.evaluate/screenshot이
# 영원히 대기하므로(evaluate엔 타임아웃 옵션이 없음) 여기서 반드시 끊어 에이전트가 멈추지 않게 한다.
RUN_WEB_TIMEOUT = 45
# 개별 조작(스크린샷·클릭·bounding_box 등)의 기본 타임아웃(ms). evaluate는 여기에 안 걸리지만,
# 나머지가 유한해져 대부분의 hang은 finally의 browser.close()로 스스로 정리된다.
PAGE_OP_TIMEOUT_MS = 10000
_STRICT_LOCAL_WEB_ASSET_SUFFIXES = frozenset({
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".svg", ".png",
    ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".woff", ".woff2",
    ".ttf", ".otf", ".wasm", ".mp3", ".wav", ".ogg", ".mp4", ".webm",
})

_STATE_PATH_RE = re.compile(r"^(?:window|globalThis)(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+$")

_CANVAS_FINGERPRINT_JS = """() => {
  const c = document.querySelector('canvas');
  if (!c || c.width <= 0 || c.height <= 0) return null;
  try {
    const ctx = c.getContext('2d');
    if (!ctx) return null;
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let hash = 2166136261;
    for (let i = 0; i < d.length; i++) {
      hash ^= d[i];
      hash = Math.imul(hash, 16777619);
    }
    return `${c.width}x${c.height}:${hash >>> 0}`;
  } catch (e) {
    return null;
  }
}"""

_READ_STATE_JS = """parts => {
  let value = window;
  for (const part of parts) {
    if (value === null || value === undefined || !(part in Object(value))) {
      return { found: false, value: null };
    }
    value = value[part];
  }
  if (value === undefined) return { found: false, value: null };
  return { found: true, value };
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


class _StepFailure(Exception):
    pass


def _safe_repr(value: object, limit: int = 180) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = repr(value)
    return rendered if len(rendered) <= limit else rendered[:limit] + "…"


def _prepare_steps(actions: object, steps: object) -> tuple[list[dict[str, Any]], str | None]:
    prepared: list[dict[str, Any]] = [
        {"action": "press", "key": key, "times": times, "_legacy": True}
        for key, times in _clamp_actions(actions)
    ]
    if steps is not None:
        if not isinstance(steps, list):
            return prepared, "steps는 배열이어야 합니다."
        for item in steps:
            if not isinstance(item, dict):
                return prepared, "각 steps 항목은 객체여야 합니다."
            prepared.append(dict(item))
    if len(prepared) > MAX_STEPS:
        return prepared[:MAX_STEPS], f"단계 수가 최대 {MAX_STEPS}개를 넘었습니다."
    return prepared, None


def _locator_for(page, step: dict[str, Any]):
    by = str(step.get("by") or "").strip().lower()
    if not by and step.get("selector"):
        by = "css"
    if by == "role":
        role = str(step.get("role") or "").strip()
        name = str(step.get("name") or "").strip()
        if not role or not name:
            raise _StepFailure("role 클릭·검증에는 role과 name이 모두 필요합니다.")
        return page.get_by_role(role, name=name, exact=True), f"role={role!r} name={name!r}"
    if by == "testid":
        value = str(step.get("value") or "").strip()
        if not value:
            raise _StepFailure("testid 대상에는 value가 필요합니다.")
        return page.get_by_test_id(value), f"testid={value!r}"
    if by == "css":
        selector = str(step.get("selector") or "").strip()
        if not selector:
            raise _StepFailure("CSS 대상에는 selector가 필요합니다.")
        return page.locator(selector), f"selector={selector!r}"
    if by == "text":
        value = str(step.get("value") or step.get("name") or "").strip()
        if not value:
            raise _StepFailure("text 대상에는 value가 필요합니다.")
        return page.get_by_text(value, exact=True), f"text={value!r}"
    raise _StepFailure("대상 지정 방식은 role, testid, css, text 중 하나여야 합니다.")


def _unique_locator(page, step: dict[str, Any]):
    locator, description = _locator_for(page, step)
    count = locator.count()
    if count == 0:
        raise _StepFailure(f"대상을 찾지 못했습니다: {description}")
    if count > 1:
        raise _StepFailure(f"대상이 {count}개라 하나를 안전하게 선택할 수 없습니다: {description}")
    return locator, description


def _canvas_fingerprint(page) -> str | None:
    try:
        value = page.evaluate(_CANVAS_FINGERPRINT_JS)
        return str(value) if value is not None else None
    except Exception:  # noqa: BLE001 - WebGL/tainted canvas는 판단 불가로 처리
        return None


def _read_state(page, raw_path: object) -> object:
    path = str(raw_path or "").strip()
    if not _STATE_PATH_RE.fullmatch(path):
        raise _StepFailure(
            "안전하지 않은 상태 경로입니다. window.__AISO_TEST__.state 같은 점 표기 식별자만 허용됩니다."
        )
    parts = path.split(".")[1:]
    result = page.evaluate(_READ_STATE_JS, parts)
    if not isinstance(result, dict) or result.get("found") is not True:
        raise _StepFailure(f"상태 경로가 존재하지 않습니다: {path}")
    return result.get("value")


def _execute_steps(
    page,
    prepared: list[dict[str, Any]],
    errors: list[str],
) -> tuple[list[str], list[str], int]:
    results: list[str] = []
    failures: list[str] = []
    passed = 0
    total_presses = 0
    total_clicks = 0
    total_wait_ms = 0
    canvas_checkpoint = _canvas_fingerprint(page)
    legacy_canvas_focused = False

    for index, step in enumerate(prepared, start=1):
        action = str(step.get("action") or "").strip().lower()
        assertion = str(step.get("assert") or "").strip().lower()
        try:
            if bool(action) == bool(assertion):
                raise _StepFailure("각 단계에는 action 또는 assert 중 정확히 하나만 필요합니다.")

            if action == "click":
                total_clicks += 1
                if total_clicks > MAX_CLICKS:
                    raise _StepFailure(f"클릭 횟수가 최대 {MAX_CLICKS}회를 넘었습니다.")
                locator, target = _unique_locator(page, step)
                if not locator.is_visible():
                    raise _StepFailure(f"클릭 대상이 보이지 않습니다: {target}")
                if not locator.is_enabled():
                    raise _StepFailure(f"클릭 대상이 비활성 상태입니다: {target}")
                has_x_ratio = "x_ratio" in step
                has_y_ratio = "y_ratio" in step
                if has_x_ratio != has_y_ratio:
                    raise _StepFailure("상대 좌표 클릭에는 x_ratio와 y_ratio가 모두 필요합니다.")
                if has_x_ratio:
                    try:
                        # step은 사용자/모델이 준 임의 JSON이라 값이 None이나 문자열일 수 있다.
                        # 그때 float()가 내는 TypeError/ValueError를 아래 except가 사용자 메시지로
                        # 바꾸는 것이 이 자리의 계약이므로, 값은 좁히지 않고 Any로 받는다.
                        raw_x: Any = step.get("x_ratio")
                        raw_y: Any = step.get("y_ratio")
                        x_ratio = float(raw_x)
                        y_ratio = float(raw_y)
                    except (TypeError, ValueError) as exc:
                        raise _StepFailure("x_ratio와 y_ratio는 0~1 사이 숫자여야 합니다.") from exc
                    if not (0.0 <= x_ratio <= 1.0 and 0.0 <= y_ratio <= 1.0):
                        raise _StepFailure("x_ratio와 y_ratio는 0~1 범위여야 합니다.")
                    box = locator.bounding_box()
                    if box is None or box["width"] <= 0 or box["height"] <= 0:
                        raise _StepFailure(f"클릭 대상의 크기를 측정하지 못했습니다: {target}")
                    x = min(max(0.0, box["width"] * x_ratio), max(0.0, box["width"] - 1))
                    y = min(max(0.0, box["height"] * y_ratio), max(0.0, box["height"] - 1))
                    locator.click(position={"x": x, "y": y})
                    detail = f"click {target} position=({x_ratio:.2f},{y_ratio:.2f})"
                else:
                    locator.click()
                    detail = f"click {target}"

            elif action == "press":
                key = str(step.get("key") or "").strip()
                if not key:
                    raise _StepFailure("press 단계에는 key가 필요합니다.")
                try:
                    times = int(step.get("times") or 1)
                except (TypeError, ValueError) as exc:
                    raise _StepFailure("times는 정수여야 합니다.") from exc
                if times < 1 or times > MAX_TIMES_PER_ACTION:
                    raise _StepFailure(f"times는 1~{MAX_TIMES_PER_ACTION} 범위여야 합니다.")
                total_presses += times
                if total_presses > MAX_TOTAL_PRESSES:
                    raise _StepFailure(f"전체 키 입력이 최대 {MAX_TOTAL_PRESSES}회를 넘었습니다.")
                if step.get("_legacy") and not legacy_canvas_focused:
                    legacy_canvas_focused = True
                    try:
                        canvas = page.locator("canvas")
                        if canvas.count() == 1 and canvas.is_visible():
                            canvas.click()
                    except Exception:  # noqa: BLE001 - 이전 actions의 포커스 보조는 호환용 최선 노력
                        pass
                normalized = _normalize_key(key)
                for _ in range(times):
                    page.keyboard.press(normalized)
                    page.wait_for_timeout(PRESS_DELAY_MS)
                detail = f"press key={normalized!r} times={times}"

            elif action == "wait":
                try:
                    wait_ms = int(step.get("ms") or 0)
                except (TypeError, ValueError) as exc:
                    raise _StepFailure("wait.ms는 정수여야 합니다.") from exc
                if wait_ms < 0 or wait_ms > MAX_WAIT_MS:
                    raise _StepFailure(f"wait.ms는 0~{MAX_WAIT_MS} 범위여야 합니다.")
                total_wait_ms += wait_ms
                if total_wait_ms > MAX_TOTAL_WAIT_MS:
                    raise _StepFailure(f"전체 대기 시간이 최대 {MAX_TOTAL_WAIT_MS}ms를 넘었습니다.")
                page.wait_for_timeout(wait_ms)
                detail = f"wait {wait_ms}ms"

            elif action:
                raise _StepFailure(f"지원하지 않는 action입니다: {action}")

            elif assertion == "visible":
                locator, target = _unique_locator(page, step)
                if not locator.is_visible():
                    raise _StepFailure(f"대상이 보이지 않습니다: {target}")
                detail = f"assert visible {target}"

            elif assertion == "text":
                locator, target = _unique_locator(page, step)
                actual = locator.inner_text().strip()
                if "equals" in step:
                    # 아래 state 단언에서는 같은 이름에 원본 JSON 값(문자열이 아닐 수 있음)이
                    # 다시 담긴다. 첫 대입으로 str에 고정되지 않도록 Any로 선언해 둔다.
                    expected: Any = str(
                        step.get("equals") if step.get("equals") is not None else ""
                    ).strip()
                    if actual != expected:
                        raise _StepFailure(
                            f"텍스트가 다릅니다: {target}, expected={expected!r}, actual={actual!r}"
                        )
                    detail = f"assert text equals {target} value={expected!r}"
                elif "contains" in step:
                    expected = str(step.get("contains") or "")
                    if expected not in actual:
                        raise _StepFailure(
                            f"텍스트가 포함되지 않습니다: {target}, contains={expected!r}, actual={actual!r}"
                        )
                    detail = f"assert text contains {target} value={expected!r}"
                else:
                    raise _StepFailure("text 단언에는 equals 또는 contains가 필요합니다.")

            elif assertion == "state":
                if "equals" not in step:
                    raise _StepFailure("state 단언에는 equals가 필요합니다.")
                actual = _read_state(page, step.get("path"))
                expected = step.get("equals")
                if actual != expected:
                    raise _StepFailure(
                        f"상태 값이 다릅니다: path={step.get('path')!r}, "
                        f"expected={_safe_repr(expected)}, actual={_safe_repr(actual)}"
                    )
                detail = f"assert state path={step.get('path')!r} equals={_safe_repr(expected)}"

            elif assertion in ("canvas_changed", "canvas_unchanged"):
                current = _canvas_fingerprint(page)
                if canvas_checkpoint is None or current is None:
                    raise _StepFailure("2D 캔버스 상태를 측정하지 못했습니다.")
                changed = current != canvas_checkpoint
                expected_changed = assertion == "canvas_changed"
                if changed != expected_changed:
                    expectation = "변경" if expected_changed else "유지"
                    raise _StepFailure(f"캔버스가 예상대로 {expectation}되지 않았습니다.")
                canvas_checkpoint = current
                detail = f"assert {assertion}"

            elif assertion == "no_console_errors":
                if errors:
                    raise _StepFailure(f"콘솔·런타임 오류가 {len(errors)}건 있습니다: {errors[0][:160]}")
                detail = "assert no_console_errors"

            else:
                raise _StepFailure(f"지원하지 않는 assert입니다: {assertion}")

            passed += 1
            results.append(f"- {index} PASS {detail}")
        except _StepFailure as exc:
            message = f"[단계 {index} 실패] {exc}"
            failures.append(message)
            results.append(f"- {index} FAIL {exc}")
            break
        except Exception as exc:  # noqa: BLE001 - Playwright 오류를 단계 실패로 압축
            message = f"[단계 {index} 실패] {type(exc).__name__}: {exc}"
            failures.append(message)
            results.append(f"- {index} FAIL {type(exc).__name__}: {exc}")
            break
    return results, failures, passed


def _file_url_path(url: str) -> Path | None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "file" or parsed.netloc:
        return None
    try:
        return Path(url2pathname(unquote(parsed.path))).resolve()
    except (OSError, ValueError):
        return None


def _is_within(path: Path, root: Path) -> bool:
    return is_path_within(path, root)


def _install_browser_sandbox(
    context,
    page,
    allowed_root: Path,
    security: dict[str, Any],
    *,
    strict_local_assets: bool = False,
    entry_target: Path | None = None,
) -> None:
    allowed_root = allowed_root.resolve()
    entry_target = entry_target.resolve() if entry_target is not None else None

    def record_network(url: str) -> None:
        security["network"] += 1
        if len(security["network_samples"]) < MAX_BLOCKED_URL_SAMPLES:
            security["network_samples"].append(url[:240])

    def route_request(route) -> None:
        url = route.request.url
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        if scheme == "file":
            local_path = _file_url_path(url)
            if local_path is not None and _is_within(local_path, allowed_root):
                if (
                    not strict_local_assets
                    or local_path == entry_target
                    or local_path.suffix.casefold() in _STRICT_LOCAL_WEB_ASSET_SUFFIXES
                ):
                    route.continue_()
                    return
                security["local_file"] += 1
                route.abort("blockedbyclient")
                return
        elif scheme in ("data", "blob", "about"):
            route.continue_()
            return
        record_network(url)
        route.abort("blockedbyclient")

    def route_socket(socket_route) -> None:
        record_network(socket_route.url)
        socket_route.close(code=1008, reason="Aiso web verification sandbox")

    def close_popup(popup) -> None:
        security["popup"] += 1
        try:
            popup.close()
        except Exception:  # noqa: BLE001
            pass

    def cancel_download(download) -> None:
        security["download"] += 1
        try:
            download.cancel()
        except Exception:  # noqa: BLE001
            pass

    def dismiss_dialog(dialog) -> None:
        security["dialog"] += 1
        try:
            dialog.dismiss()
        except Exception:  # noqa: BLE001
            pass

    context.route("**/*", route_request)
    if hasattr(context, "route_web_socket"):
        context.route_web_socket("**/*", route_socket)
    page.on("popup", close_popup)
    page.on("download", cancel_download)
    page.on("dialog", dismiss_dialog)


def _run_web_sync(
    target: Path,
    rel: str,
    actions: list | None = None,
    steps: list | None = None,
    *,
    allowed_root: Path | None = None,
    strict_local_assets: bool = False,
) -> tuple[str, str | None]:
    """격리된 Edge에서 한 번의 검증 시나리오를 실행한다."""
    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    error_total = 0
    title = ""
    node_count = 0
    has_canvas = False
    blank = False
    shot: str | None = None
    step_results: list[str] = []
    step_failures: list[str] = []
    step_passed = 0
    prepared, preparation_error = _prepare_steps(actions, steps)
    security: dict[str, Any] = {
        "network": 0,
        "network_samples": [],
        "popup": 0,
        "download": 0,
        "dialog": 0,
        "local_file": 0,
    }

    def add_error(message: str) -> None:
        nonlocal error_total
        error_total += 1
        if len(errors) < MAX_ERROR_SAMPLES and message not in errors:
            errors.append(message)

    def add_console_error(message) -> None:
        if message.type != "error":
            return
        text = message.text
        # 격리기가 의도적으로 막은 요청의 Chromium 진단은 앱 런타임 오류가 아니다.
        if (security["network"] or security["local_file"]) and (
            "ERR_BLOCKED_BY_CLIENT" in text or "ERR_INTERNET_DISCONNECTED" in text
        ):
            return
        add_error(f"[콘솔에러] {text}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="msedge",
                headless=True,
                # playwright의 env는 dict[str, str | float | bool]인데 dict는 불변(invariant)이라
                # dict[str, str]이 그대로 들어가지 않는다. 값은 전부 str이므로 표기만 넓힌다.
                env=cast("dict[str, str | float | bool]", sanitized_child_environment()),
                args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
            )
            context = None
            try:
                context = browser.new_context(
                    viewport={"width": 900, "height": 620},
                    offline=True,
                    accept_downloads=False,
                    service_workers="block",
                )
                page = context.new_page()
                page.set_default_timeout(PAGE_OP_TIMEOUT_MS)
                _install_browser_sandbox(
                    context,
                    page,
                    (allowed_root or target.parent).resolve(),
                    security,
                    strict_local_assets=strict_local_assets,
                    entry_target=target,
                )
                page.on("console", add_console_error)
                page.on("pageerror", lambda error: add_error(f"[예외] {error}"))
                try:
                    page.goto(target.as_uri(), wait_until="load", timeout=15000)
                except Exception as error:  # noqa: BLE001
                    add_error(f"[로드 실패] {error!r}")
                page.wait_for_timeout(500)

                if preparation_error:
                    step_failures.append(f"[단계 1 실패] {preparation_error}")
                    step_results.append(f"- 1 FAIL {preparation_error}")
                elif prepared:
                    step_results, step_failures, step_passed = _execute_steps(page, prepared, errors)

                try:
                    title = page.title()
                    info = page.evaluate(_CANVAS_PROBE)
                    has_canvas = bool(info.get("canvas"))
                    node_count = int(info.get("nodes") or 0)
                    blank = bool(info.get("blank"))
                except Exception as error:  # noqa: BLE001
                    add_error(f"[상태 검사 실패] {type(error).__name__}: {error}")
                try:
                    # 사용자가 보는 카드는 반드시 모든 조작 또는 실패 단계가 끝난 뒤의 화면이다.
                    shot = base64.b64encode(page.screenshot(type="png", timeout=8000)).decode()
                except Exception as error:  # noqa: BLE001
                    add_error(f"[스크린샷 실패] {type(error).__name__}: {error}")
            finally:
                if context is not None:
                    context.close()
                browser.close()
    except Exception as error:  # noqa: BLE001 - 브라우저 자체 실행 실패
        return (
            "[WEB_VALIDATION v1]\n"
            f"status=INCONCLUSIVE level=runtime entry={rel}\n"
            f"summary=검증 불가: {type(error).__name__}: {error}\n"
            "next=Edge 설치와 Playwright 실행 환경을 확인한 뒤 다시 검증하십시오.",
            None,
        )

    runtime_failed = error_total > 0
    canvas_failed = has_canvas and blank
    steps_failed = bool(step_failures)
    security_limited = any(
        security[key] for key in ("network", "popup", "download", "dialog", "local_file")
    )
    if runtime_failed or canvas_failed or steps_failed:
        status = "FAIL"
    elif security_limited:
        status = "INCONCLUSIVE"
    else:
        status = "PASS"
    if any(step.get("assert") for step in prepared):
        level = "function"
    elif prepared:
        level = "interaction"
    else:
        level = "runtime"

    if prepared or preparation_error:
        requested_steps = len(prepared) if not preparation_error else max(1, len(prepared))
        summary = f"단계 검증: {step_passed}/{requested_steps} 통과"
    elif status == "PASS":
        summary = "기본 실행 검사 통과 — 조작과 기능은 별도로 검증되지 않았습니다."
    else:
        summary = "기본 실행 검사에서 문제를 발견했습니다."

    report_lines = [
        "[WEB_VALIDATION v1]",
        f"status={status} level={level} entry={rel}",
        f"summary={summary}",
    ]
    if step_failures:
        report_lines.append("failures:")
        report_lines.extend(f"- {failure}" for failure in step_failures[:5])
    if runtime_failed:
        report_lines.append(f"runtime=FAIL errors={error_total}")
        report_lines.extend(f"- {error[:220]}" for error in errors[:10])
    elif canvas_failed:
        report_lines.append("runtime=FAIL canvas_blank=true")
    else:
        report_lines.append(
            f"runtime=PASS console=PASS render=PASS title={title!r} canvas={has_canvas} dom_nodes={node_count}"
        )
    if security["network"]:
        report_lines.append(f"security=네트워크 차단 {security['network']}건")
        report_lines.extend(
            f"- blocked_url={url}" for url in security["network_samples"][:3]
        )
    if security["popup"]:
        report_lines.append(f"security=팝업 차단 {security['popup']}건")
    if security["download"]:
        report_lines.append(f"security=다운로드 차단 {security['download']}건")
    if security["dialog"]:
        report_lines.append(f"security=대화상자 차단 {security['dialog']}건")
    if security["local_file"]:
        report_lines.append(f"security=로컬 파일 차단 {security['local_file']}건")
    if step_results:
        report_lines.append("steps:")
        report_lines.extend(step_results)
    if status == "FAIL":
        report_lines.append("next=실패한 조건만 수정한 뒤 동일한 steps로 다시 검증하십시오.")
    elif status == "INCONCLUSIVE":
        report_lines.append("next=격리 환경에서 차단된 의존성 또는 판단 불가 항목을 사용자에게 알리십시오.")
    else:
        report_lines.append("next=통과 범위 밖의 기능까지 검증됐다고 주장하지 마십시오.")
    return "\n".join(report_lines), shot


def _timeout_report(path: str) -> tuple[str, None]:
    return (
        "[WEB_VALIDATION v1]\n"
        f"status=INCONCLUSIVE level=runtime entry={path}\n"
        f"summary=검증 시간 초과: {RUN_WEB_TIMEOUT}초를 넘겨 브라우저 프로세스를 종료했습니다.\n"
        "next=끝나지 않는 반복문·재귀·동기 작업을 고친 뒤 다시 검증하십시오.",
        None,
    )


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    # os.name 대신 sys.platform으로 분기해야 타입 검사기가 플랫폼 분기를 인식해
    # 아래 else의 POSIX 전용 API(os.killpg/signal.SIGKILL)를 Windows에서 오탐하지 않는다.
    # CPython에서 sys.platform == "win32" ⟺ os.name == "nt" 이므로 판정은 동일하다.
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=sanitized_child_environment(),
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _cleanup_worker_temp_dir(path: Path) -> None:
    """Remove a worker directory without letting a late Windows handle fail a user run."""
    attempts = 8
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            sharing_violation = os.name == "nt" and getattr(error, "winerror", None) in {32, 33}
            if sharing_violation and attempt < attempts - 1:
                # taskkill /T can return just before a Playwright/Edge descendant
                # releases the inherited stdout handle. Keep cleanup off the user
                # result path and retry the narrow transient only.
                time.sleep(0.05 * (attempt + 1))
                continue
            _LOG.warning("web validation temporary directory cleanup deferred: %s (%s)", path, error)
            return


@contextmanager
def _worker_temp_directory():
    path = Path(tempfile.mkdtemp(prefix="aiso_webcheck_"))
    try:
        yield path
    finally:
        _cleanup_worker_temp_dir(path)


def _run_web_process(
    root: Path,
    path: str,
    actions: list | None,
    steps: list | None,
    cancel_event: threading.Event,
    strict_local_assets: bool = False,
) -> tuple[str, str | None]:
    payload = {
        "root": str(root.resolve()),
        "path": path,
        "actions": actions,
        "steps": steps,
        "strict_local_assets": strict_local_assets,
    }
    create_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    with _worker_temp_directory() as temp:
        input_path = temp / "input.json"
        output_path = temp / "output.json"
        error_path = temp / "stderr.txt"
        input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with output_path.open("wb") as output_file, error_path.open("wb") as error_file:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker", str(input_path)],
                cwd=str(root),
                env=sanitized_child_environment(PYTHONIOENCODING="utf-8", PYTHONUTF8="1"),
                stdin=subprocess.DEVNULL,
                stdout=output_file,
                stderr=error_file,
                creationflags=create_flags,
                start_new_session=os.name != "nt",
            )
            deadline = time.monotonic() + RUN_WEB_TIMEOUT
            timed_out = False
            cancelled = False
            while proc.poll() is None:
                if cancel_event.is_set():
                    cancelled = True
                    _terminate_process_tree(proc)
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    _terminate_process_tree(proc)
                    break
                time.sleep(0.05)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(proc)
        if cancelled:
            return ("[WEB_VALIDATION v1]\nstatus=INCONCLUSIVE level=runtime\nsummary=사용자 취소", None)
        if timed_out:
            return _timeout_report(path)
        if proc.returncode != 0:
            detail = error_path.read_text(encoding="utf-8", errors="replace")[-1200:]
            return (
                "[WEB_VALIDATION v1]\n"
                f"status=INCONCLUSIVE level=runtime entry={path}\n"
                f"summary=검증 작업자 오류: {detail or f'exit {proc.returncode}'}",
                None,
            )
        if output_path.stat().st_size > MAX_WORKER_OUTPUT_BYTES:
            return (
                "[WEB_VALIDATION v1]\n"
                f"status=INCONCLUSIVE level=runtime entry={path}\n"
                "summary=검증 결과가 안전한 출력 상한을 넘었습니다.",
                None,
            )
        try:
            result = json.loads(output_path.read_text(encoding="utf-8"))
            report = result.get("report")
            screenshot = result.get("screenshot")
            if not isinstance(report, str) or not (screenshot is None or isinstance(screenshot, str)):
                raise ValueError("invalid worker result")
            return report, screenshot
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return (
                "[WEB_VALIDATION v1]\n"
                f"status=INCONCLUSIVE level=runtime entry={path}\n"
                f"summary=검증 결과를 읽지 못했습니다: {type(error).__name__}",
                None,
            )


def _worker_main(input_path: str) -> int:
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    root = Path(str(payload.get("root") or "")).resolve()
    rel = str(payload.get("path") or "")
    target = _resolve(root, rel)
    if not target.is_file() or target.suffix.lower() not in (".html", ".htm"):
        raise ToolError("검증할 HTML 파일이 없습니다.")
    report, shot = _run_web_sync(
        target,
        rel,
        payload.get("actions"),
        payload.get("steps"),
        allowed_root=target.parent if payload.get("strict_local_assets") else root,
        strict_local_assets=bool(payload.get("strict_local_assets")),
    )
    sys.stdout.write(json.dumps({"report": report, "screenshot": shot}, ensure_ascii=False))
    return 0


async def run_web(
    root: Path,
    path: str,
    actions: list | None = None,
    steps: list | None = None,
    _strict_local_assets: bool = False,
    **_ignore,
) -> tuple[str, str | None]:
    """HTML을 격리된 작업자 Edge에서 실행하고 작업자 프로세스까지 정리한다."""
    target = _resolve(root, path)
    if not target.is_file():
        raise ToolError(f"파일이 없습니다: {path}")
    if target.suffix.lower() not in (".html", ".htm"):
        raise ToolError("HTML 파일만 실행·검사할 수 있습니다.")
    cancel_event = threading.Event()
    # 조건에 따라 원소 수가 달라지는 가변 인자 묶음이라 고정 길이 튜플로 굳으면 안 된다.
    process_args: tuple[Any, ...] = (root, path, actions, steps, cancel_event)
    if _strict_local_assets:
        process_args = (*process_args, True)
    task = asyncio.create_task(asyncio.to_thread(_run_web_process, *process_args))
    try:
        return await task
    except asyncio.CancelledError:
        cancel_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        raise


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(_worker_main(sys.argv[2]))
    raise SystemExit("webcheck.py is an internal worker module")
