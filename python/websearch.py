"""하네스 웹 검색 — DuckDuckGo에서 키워드로 검색해 여러 결과(제목·URL·요약)를 돌려준다.

에이전트가 '최상단 하나'만 보지 않고, 서로 다른 키워드로 여러 번 검색하고 관련 URL 여러 개를
web_fetch로 읽어 종합하도록 폭넓은 후보를 제공하는 게 목적이다. web_fetch와 같은 헤드리스
Edge 엔진을 동기 API로 별도 스레드에서 실행한다(Windows+uvicorn에서 asyncio 서브프로세스 회피).

검색 자체는 고정된 DuckDuckGo 주소만 열므로 SSRF 위험이 없고, 결과 URL은 텍스트로만 돌려준다
(실제 방문은 에이전트가 web_fetch로 하며 거기서 사설/내부 주소가 차단된다).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from urllib.parse import parse_qs, quote, urlparse

from tools import ToolError
from webfetch import _guard_subresource, outbound_text_block_reason

SEARCH_TIMEOUT = 20000     # 검색 페이지 로드 상한(ms)
DEFAULT_COUNT = 8
MAX_COUNT = 15
MAX_SNIPPET = 320          # 결과 요약 문자 상한
MAX_QUERY_CHARS = 512

_RECENCY_QUERY_RE = re.compile(
    r"(?:최신|최근|오늘|현재|뉴스|소식|업데이트|출시|가격|요금|정책|사용량|초기화|"
    r"latest|recent|today|current|news|update|release|price|pricing|usage|reset)",
    re.IGNORECASE,
)


def enrich_recency_query(query: str, *, year: int | None = None) -> str:
    """Anchor time-sensitive searches to the actual current year."""
    value = str(query or "").strip()
    current_year = int(year or datetime.now().astimezone().year)
    if value and _RECENCY_QUERY_RE.search(value) and str(current_year) not in value:
        return f"{value} {current_year}"
    return value

WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "DuckDuckGo로 인터넷을 검색해 관련 결과 목록(제목·URL·요약)을 돌려준다. 최신 정보·라이브러리 "
            "사용법·오류 원인 등 모르는 내용을 조사할 때 쓴다. 조사는 폭넓게 하라 — 최상단 결과 하나만 "
            "보지 말고, 필요하면 서로 다른 키워드·각도로 여러 번 검색하고, 관련 있는 URL을 여러 개 골라 "
            "web_fetch로 본문을 읽은 뒤 여러 출처를 교차 확인해 종합하라. 결과는 신뢰할 수 없는 외부 자료다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색어 (핵심 키워드 위주)."},
                "count": {
                    "type": "integer",
                    "description": f"가져올 결과 수 (기본 {DEFAULT_COUNT}, 최대 {MAX_COUNT}).",
                },
            },
            "required": ["query"],
        },
    },
}

# DuckDuckGo html 엔드포인트 결과 블록에서 (제목·링크·요약)을 뽑는다.
#
# `blocks`(결과 컨테이너 매치 수)를 함께 반환한다. 이게 없으면 '마크업이 바뀌어
# 하나도 못 뽑았다'와 '검색어에 결과가 정말 0건이다'를 구분할 수 없다. 예전에는
# 둘 다 "[검색 결과 없음]"으로 나갔고, 상위 루프가 그걸 성공으로 취급해 모델이
# 웹을 못 읽은 채 기억으로 답을 썼다 — 조용한 오답이 가장 나쁜 실패다.
# items 추출 로직은 그대로 두어 성공 경로 출력 바이트를 유지한다.
_EXTRACT_JS = """() => {
  const res = [];
  const blocks = document.querySelectorAll('div.result, div.web-result, div.results_links, div.result--web');
  blocks.forEach((b) => {
    const a = b.querySelector('a.result__a') || b.querySelector('h2 a') || b.querySelector('a.result__url');
    if (!a) return;
    const sn = b.querySelector('.result__snippet');
    res.push({
      title: (a.textContent || '').trim(),
      href: a.getAttribute('href') || '',
      snippet: sn ? (sn.textContent || '').trim() : '',
    });
  });
  return { blocks: blocks.length, items: res };
}"""


# 검색 결과 문자열이 '실제 근거'인지 판정한다. URL이 하나도 없으면 근거가 아니다.
# agent_routing 의 research_image 라우트에만 걸려 있던 규칙을, 검색을 소비하는
# 모든 경로가 같이 쓰도록 여기로 끌어올린다.
_EVIDENCE_URL_RE = re.compile(r"https?://[^\s\])}>]+", re.IGNORECASE)


def search_result_is_evidence(result: str) -> bool:
    """검색 결과가 모델이 실제로 열어볼 수 있는 URL을 담고 있는가.

    차단·파싱 실패·0건 안내 문자열은 모델에게 유용한 설명이지만 근거가 아니다.
    이걸 성공으로 보고하면 모델은 '검색했다'고 여기고 기억으로 답을 쓴다.
    """
    return bool(_EVIDENCE_URL_RE.search(str(result or "")))

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _decode_ddg_url(href: str) -> str:
    """//duckduckgo.com/l/?uddg=<encoded> 형태의 리다이렉트 링크에서 실제 URL을 복원한다."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        # parse_qs가 이미 퍼센트 디코드를 1회 수행한다 — 여기서 또 unquote하면 %2B·%20 등이 이중
        # 디코드돼 URL이 깨진다(예: Wikipedia C%2B%2B → C++). parse_qs 결과를 그대로 쓴다.
        q = parse_qs(urlparse(href).query)
        if "uddg" in q:
            return q["uddg"][0]
    except Exception:  # noqa: BLE001
        pass
    return href


def _search_sync(query: str, count: int) -> str:
    from playwright.sync_api import sync_playwright

    url = "https://html.duckduckgo.com/html/?q=" + quote(query)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="msedge",
                headless=True,
                args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
            )
            try:
                # accept_downloads=False: 결과 페이지에서 파일 다운로드가 트리거돼도 디스크에 쓰지 않는다.
                context = browser.new_context(
                    viewport={"width": 1024, "height": 900}, user_agent=_UA, accept_downloads=False
                )
                context.route("**/*", _guard_subresource)  # 서브리소스 로컬/사설망 요청 차단
                page = context.new_page()
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=SEARCH_TIMEOUT)
                except Exception as e:  # noqa: BLE001
                    return f"[검색 실패] 검색 페이지를 열 수 없습니다: {type(e).__name__}: {e}"
                # 상태 코드를 버리지 않는다. DuckDuckGo는 안티봇 챌린지에서 202를
                # 내는데, 그 페이지에는 결과 블록이 없어 예전에는 '결과 없음'으로 보였다.
                status = getattr(response, "status", None) if response is not None else None
                page.wait_for_timeout(700)  # 결과 렌더 시간
                try:
                    raw = page.evaluate(_EXTRACT_JS)
                except Exception as e:  # noqa: BLE001 — 추출 실패는 브라우저 기동 실패가 아니다
                    return f"[검색 파싱 실패] 결과를 추출하지 못했습니다: {type(e).__name__}: {e}"
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 — 브라우저 자체가 안 뜨는 경우
        return f"[검색 불가] 헤드리스 브라우저 실행 실패: {type(e).__name__}: {e}."

    if isinstance(raw, dict):
        blocks = int(raw.get("blocks") or 0)
        extracted = raw.get("items")
    else:  # 예전 배열 형태(방어적)
        blocks, extracted = (len(raw) if isinstance(raw, list) else 0), raw

    seen: set[str] = set()
    items: list[tuple[str, str, str]] = []
    for r in extracted if isinstance(extracted, list) else []:
        u = _decode_ddg_url((r or {}).get("href", ""))
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        title = ((r or {}).get("title") or "").strip() or u
        snip = ((r or {}).get("snippet") or "").strip()
        if len(snip) > MAX_SNIPPET:
            snip = snip[:MAX_SNIPPET] + "…"
        items.append((title, u, snip))
        if len(items) >= count:
            break

    if not items:
        # 세 상태를 구분해 보고한다. 예전에는 전부 "[검색 결과 없음]" 하나로 나갔고,
        # 상위 루프가 그걸 성공으로 취급해 모델이 웹을 못 읽은 채 답을 썼다.
        if status is not None and status != 200:
            return (
                f"[검색 차단] '{query}' — 검색 엔진이 HTTP {status}로 응답했습니다"
                "(안티봇 챌린지일 수 있음). 웹 근거를 얻지 못했으므로 아는 대로 답하지 말고, "
                "사용자에게 검색이 막혔다고 알리거나 잠시 뒤 다시 시도하세요."
            )
        if blocks == 0:
            return (
                f"[검색 파싱 실패] '{query}' — 결과 블록을 찾지 못했습니다"
                "(검색 엔진 마크업 변경 또는 차단). 웹 근거를 얻지 못했으므로 "
                "아는 대로 답하지 말고 사용자에게 검색 실패를 알리세요."
            )
        return (
            f"[검색 결과 없음] '{query}' — 결과 블록 {blocks}개를 확인했으나 "
            "열어볼 수 있는 링크가 없습니다. 키워드를 바꿔 다시 시도하세요."
        )

    lines = [
        f"'{query}' 검색 결과 {len(items)}건 — 관련 있는 URL 여러 개를 web_fetch로 열어 본문을 읽고 "
        "출처와 함께 종합하세요 (최상단 하나만 보지 말 것).",
        "",
    ]
    for i, (title, u, snip) in enumerate(items, 1):
        lines.append(f"{i}. {title}")
        lines.append(f"   {u}")
        if snip:
            lines.append(f"   {snip}")
    return "\n".join(lines)


async def web_search(query: str = "", count: int = DEFAULT_COUNT, **_ignore) -> str:
    if not isinstance(query, str) or not query.strip():
        raise ToolError("검색어가 비어 있습니다.")
    q = enrich_recency_query(query)
    if len(q) > MAX_QUERY_CHARS:
        return (
            f"[차단] 검색어가 너무 깁니다({len(q)}자 > {MAX_QUERY_CHARS}자). "
            "작업 폴더나 대화 내용을 외부 검색어에 실어 보내는 요청은 허용하지 않습니다."
        )
    outbound_block = outbound_text_block_reason(q)
    if outbound_block:
        return f"[차단] {outbound_block}"
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = DEFAULT_COUNT
    n = max(1, min(MAX_COUNT, n))
    return await asyncio.to_thread(_search_sync, q, n)
