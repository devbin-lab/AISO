"""하네스 웹 검색 — DuckDuckGo에서 키워드로 검색해 여러 결과(제목·URL·요약)를 돌려준다.

에이전트가 '최상단 하나'만 보지 않고, 서로 다른 키워드로 여러 번 검색하고 관련 URL 여러 개를
web_fetch로 읽어 종합하도록 폭넓은 후보를 제공하는 게 목적이다. web_fetch와 같은 헤드리스
Edge 엔진을 동기 API로 별도 스레드에서 실행한다(Windows+uvicorn에서 asyncio 서브프로세스 회피).

검색 자체는 고정된 DuckDuckGo 주소만 열므로 SSRF 위험이 없고, 결과 URL은 텍스트로만 돌려준다
(실제 방문은 에이전트가 web_fetch로 하며 거기서 사설/내부 주소가 차단된다).
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, quote, urlparse

from tools import ToolError

SEARCH_TIMEOUT = 20000     # 검색 페이지 로드 상한(ms)
DEFAULT_COUNT = 8
MAX_COUNT = 15
MAX_SNIPPET = 320          # 결과 요약 문자 상한

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
_EXTRACT_JS = """() => {
  const res = [];
  document.querySelectorAll('div.result, div.web-result, div.results_links, div.result--web').forEach((b) => {
    const a = b.querySelector('a.result__a') || b.querySelector('h2 a') || b.querySelector('a.result__url');
    if (!a) return;
    const sn = b.querySelector('.result__snippet');
    res.push({
      title: (a.textContent || '').trim(),
      href: a.getAttribute('href') || '',
      snippet: sn ? (sn.textContent || '').trim() : '',
    });
  });
  return res;
}"""

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
            browser = p.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page(viewport={"width": 1024, "height": 900}, user_agent=_UA)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=SEARCH_TIMEOUT)
                except Exception as e:  # noqa: BLE001
                    return f"[검색 실패] 검색 페이지를 열 수 없습니다: {type(e).__name__}: {e}"
                page.wait_for_timeout(700)  # 결과 렌더 시간
                raw = page.evaluate(_EXTRACT_JS)
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 — 브라우저 자체가 안 뜨는 경우
        return f"[검색 불가] 헤드리스 브라우저 실행 실패: {type(e).__name__}: {e}."

    seen: set[str] = set()
    items: list[tuple[str, str, str]] = []
    for r in raw if isinstance(raw, list) else []:
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
        return (
            f"[검색 결과 없음] '{query}' — 검색이 일시적으로 차단됐거나 결과가 없습니다. "
            "키워드를 바꿔 다시 시도하세요."
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
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = DEFAULT_COUNT
    n = max(1, min(MAX_COUNT, n))
    return await asyncio.to_thread(_search_sync, query.strip(), n)
