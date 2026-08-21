"""하네스 웹 검색 — DuckDuckGo에서 키워드로 검색해 여러 결과(제목·URL·요약)를 돌려준다.

에이전트가 '최상단 하나'만 보지 않고, 서로 다른 키워드로 여러 번 검색하고 관련 URL 여러 개를
web_fetch로 읽어 종합하도록 폭넓은 후보를 제공하는 게 목적이다.

## 전송 경로가 둘인 이유

예전에는 web_fetch와 같은 헤드리스 Edge로만 검색했다. 그런데 검색이 여는 것은
`html.duckduckgo.com/html/` — **서버가 렌더한 정적 HTML**이고 자바스크립트가 필요 없다.
브라우저를 띄울 이유가 없었던 셈인데, 그 대가는 컸다:

  - 검색 한 번마다 node.exe(Playwright 드라이버) + msedge를 새로 띄운다.
  - 메모리가 빠듯하면(12B 모델이 상주 중인 16GB 기기) 그 기동이 실패한다.
  - 실패는 `AttributeError: 'PlaywrightContextManager' object has no attribute
    '_playwright'` 로 나타났다. 이건 진짜 원인이 아니라 **껍데기**다 — 그 속성은
    드라이버가 접속했을 때 콜백에서만 대입되므로, 이 예외는 곧 '드라이버가 못 떴다'는 뜻이다.
  - 검색 실패는 런 전체를 멈춘다(근거 없이 답하지 않는 계약). 그래서 특정 모델처럼
    검색을 자주 부르는 경로에서는 거의 고정적인 실패로 보였다.

이제 평문 HTTP를 먼저 쓴다. 실측(같은 질의 3종): HTTP 200, CAPTCHA 없음,
**1.0~1.3초 — 브라우저 경로 2.5초의 절반 이하**. 브라우저를 아예 안 띄우므로
위 실패 부류가 통째로 사라진다. 서브리소스를 하나도 불러오지 않아 더 안전하기도 하다.

브라우저 경로는 **폴백으로 남긴다**. DDG가 평문 요청을 막기 시작하면(안티봇 챌린지)
진짜 브라우저처럼 보이는 쪽이 통과할 수 있기 때문이다. 두 경로가 모두 실패하면
**각각의 실패 사유를 함께** 보고한다 — 마지막 것만 보고하면 어느 층이 무너졌는지 잃는다.

검색 자체는 고정된 DuckDuckGo 주소만 열므로 SSRF 위험이 없고, 결과 URL은 텍스트로만
돌려준다(실제 방문은 에이전트가 web_fetch로 하며 거기서 사설/내부 주소가 차단된다).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, urlparse

from tools import ToolError
from webfetch import _guard_subresource, outbound_text_block_reason

SEARCH_TIMEOUT = 20000     # 브라우저 폴백의 페이지 로드 상한(ms)
HTTP_TIMEOUT = 12.0        # 평문 HTTP 상한(초) — 실측 1.0~1.3초라 넉넉하다
HTTP_RETRIES = 1           # 전송 실패(네트워크 끊김 등)에만 재시도한다
RETRY_BACKOFF = 0.6        # 초
DEFAULT_COUNT = 8
MAX_COUNT = 15
MAX_SNIPPET = 320          # 결과 요약 문자 상한
MAX_QUERY_CHARS = 512

SEARCH_HOST = "html.duckduckgo.com"
SEARCH_URL = f"https://{SEARCH_HOST}/html/?q="
# 리다이렉트는 DuckDuckGo 안에서만 허용한다. 검색 엔드포인트가 임의의 외부 호스트로
# 우리를 보내는 것은 정상이 아니고, 그 응답을 파싱하면 신뢰 경계가 흐려진다.
_ALLOWED_REDIRECT_HOSTS = ("duckduckgo.com",)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

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

# 브라우저 폴백에서 쓰는 추출기. 아래 HTMLParser 와 **같은 의미**로 유지해야 한다
# (같은 블록 선택자, 같은 제목·링크·요약 위치). 두 경로가 다른 결과를 내면
# 폴백이 일어난 검색만 조용히 달라진다.
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
_EVIDENCE_URL_RE = re.compile(r"https?://[^\s\])}>]+", re.IGNORECASE)


def search_result_is_evidence(result: str) -> bool:
    """검색 결과가 모델이 실제로 열어볼 수 있는 URL을 담고 있는가.

    차단·파싱 실패·0건 안내 문자열은 모델에게 유용한 설명이지만 근거가 아니다.
    이걸 성공으로 보고하면 모델은 '검색했다'고 여기고 기억으로 답을 쓴다.
    """
    return bool(_EVIDENCE_URL_RE.search(str(result or "")))


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


# ── 결과 표현 ───────────────────────────────────────────────────────────────

OK = "ok"
BLOCKED = "blocked"              # HTTP 상태가 200이 아니다 — 안티봇 챌린지 등
PARSE_FAILED = "parse_failed"    # 결과 블록을 하나도 못 찾았다 — 마크업 변경 또는 200 위장 차단
EMPTY = "empty"                  # 블록은 있는데 열어볼 URL이 없다 — 진짜 0건
TRANSPORT_FAILED = "transport_failed"  # 네트워크/브라우저 자체가 실패 — 재시도 대상


@dataclass(frozen=True)
class SearchOutcome:
    """전송 경로 하나의 결과. 실패 사유를 버리지 않는다."""

    transport: str                                   # "http" | "browser"
    status: str
    items: tuple[tuple[str, str, str], ...] = ()     # (title, url, snippet)
    blocks: int = 0
    detail: str = ""

    @property
    def retryable(self) -> bool:
        # 서버가 답을 준 경우(차단·0건·마크업 변경)는 재시도해도 같은 답이 온다.
        return self.status == TRANSPORT_FAILED


class _DdgResultParser(HTMLParser):
    """_EXTRACT_JS 와 같은 의미의 추출기 — div.result 안의 a.result__a 와 .result__snippet.

    표준 라이브러리만 쓴다. 검색 결과 파싱 하나 때문에 HTML 라이브러리 의존을 늘리지 않는다.
    """

    _BLOCK_CLASSES = ("result", "web-result", "results_links", "result--web")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks = 0
        self.items: list[dict[str, str]] = []
        self._depth = 0   # 결과 블록 안의 태그 깊이. 0이면 블록 밖.
        self._current: dict[str, str] | None = None
        self._sink: str | None = None
        self._buffer: list[str] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        return set((dict(attrs).get("class") or "").split())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = self._classes(attrs)
        if self._depth == 0:
            if tag == "div" and classes.intersection(self._BLOCK_CLASSES):
                self.blocks += 1
                self._depth = 1
                self._current = {"title": "", "href": "", "snippet": ""}
            return
        self._depth += 1
        if self._current is None:
            return
        if tag == "a" and "result__a" in classes and not self._current["href"]:
            self._current["href"] = dict(attrs).get("href") or ""
            self._sink, self._buffer = "title", []
        elif "result__snippet" in classes and not self._current["snippet"]:
            self._sink, self._buffer = "snippet", []

    def handle_endtag(self, tag: str) -> None:
        if self._depth == 0:
            return
        if self._sink is not None and self._current is not None:
            text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
            if not self._current[self._sink]:
                self._current[self._sink] = text
            self._sink, self._buffer = None, []
        self._depth -= 1
        if self._depth == 0 and self._current is not None:
            if self._current["href"]:
                self.items.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._sink is not None:
            self._buffer.append(data)


def parse_ddg_html(markup: str) -> tuple[int, list[dict[str, str]]]:
    """검색 결과 HTML에서 (블록 수, 항목들)을 뽑는다.

    블록 수를 따로 돌려주는 이유: 이게 없으면 '마크업이 바뀌어 하나도 못 뽑았다'와
    '검색어에 결과가 정말 0건이다'를 구분할 수 없다. 예전에는 둘 다 "[검색 결과 없음]"
    으로 나갔고, 상위 루프가 그걸 성공으로 취급해 모델이 웹을 못 읽은 채 기억으로
    답을 썼다 — 조용한 오답이 가장 나쁜 실패다.
    """
    parser = _DdgResultParser()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # noqa: BLE001 — 깨진 마크업이어도 그때까지 모은 것은 쓴다
        pass
    return parser.blocks, parser.items


def _collect_items(
    raw_items: list[dict[str, str]], count: int
) -> tuple[tuple[str, str, str], ...]:
    seen: set[str] = set()
    items: list[tuple[str, str, str]] = []
    for entry in raw_items:
        url = _decode_ddg_url((entry or {}).get("href", ""))
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        title = ((entry or {}).get("title") or "").strip() or url
        snippet = ((entry or {}).get("snippet") or "").strip()
        if len(snippet) > MAX_SNIPPET:
            snippet = snippet[:MAX_SNIPPET] + "…"
        items.append((title, url, snippet))
        if len(items) >= count:
            break
    return tuple(items)


def _outcome_from_page(
    transport: str, status_code: int | None, markup: str, count: int
) -> SearchOutcome:
    """상태 코드와 마크업 하나를 세 실패 상태 중 하나로 판정한다 — 순수 함수."""
    blocks, raw = parse_ddg_html(markup)
    items = _collect_items(raw, count)
    if items:
        return SearchOutcome(transport, OK, items, blocks)
    if status_code is not None and status_code != 200:
        return SearchOutcome(transport, BLOCKED, (), blocks, f"HTTP {status_code}")
    if blocks == 0:
        return SearchOutcome(transport, PARSE_FAILED, (), 0, "결과 블록 0개")
    return SearchOutcome(transport, EMPTY, (), blocks, f"블록 {blocks}개, 링크 0개")


# ── 전송 경로 1: 평문 HTTP (브라우저 없음) ──────────────────────────────────

def _fetch_http(query: str, count: int) -> SearchOutcome:
    import httpx

    try:
        with httpx.Client(
            headers={"User-Agent": _UA, "Accept-Language": "ko,en;q=0.9"},
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
        ) as client:
            response = client.get(SEARCH_URL + quote(query))
    except Exception as e:  # noqa: BLE001 — 네트워크 실패는 재시도 대상이다
        return SearchOutcome("http", TRANSPORT_FAILED, (), 0, f"{type(e).__name__}: {e}")

    # 리다이렉트가 DuckDuckGo 밖으로 나갔다면 파싱하지 않는다.
    final_host = (urlparse(str(response.url)).hostname or "").lower()
    if not any(final_host == h or final_host.endswith("." + h) for h in _ALLOWED_REDIRECT_HOSTS):
        return SearchOutcome("http", BLOCKED, (), 0, f"예상치 못한 호스트로 리다이렉트: {final_host}")

    return _outcome_from_page("http", response.status_code, response.text, count)


# ── 전송 경로 2: 헤드리스 브라우저 (폴백) ───────────────────────────────────

def _fetch_browser(query: str, count: int) -> SearchOutcome:
    from playwright.sync_api import sync_playwright

    url = SEARCH_URL + quote(query)
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
                    return SearchOutcome(
                        "browser", TRANSPORT_FAILED, (), 0,
                        f"페이지를 열 수 없음: {type(e).__name__}: {e}",
                    )
                status = getattr(response, "status", None) if response is not None else None
                page.wait_for_timeout(700)  # 결과 렌더 시간
                try:
                    raw = page.evaluate(_EXTRACT_JS)
                except Exception as e:  # noqa: BLE001 — 추출 실패는 브라우저 기동 실패가 아니다
                    return SearchOutcome(
                        "browser", PARSE_FAILED, (), 0,
                        f"추출 실패: {type(e).__name__}: {e}",
                    )
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 — 브라우저 자체가 안 뜨는 경우
        # AttributeError: ... has no attribute '_playwright' 는 Playwright 내부가
        # 드라이버 기동 실패를 드러내는 방식이다. 그대로 내보내면 사용자에게
        # 아무 의미가 없으므로 무슨 일이 일어났는지로 바꿔 적는다.
        reason = f"{type(e).__name__}: {e}"
        if isinstance(e, AttributeError) and "_playwright" in str(e):
            reason = "브라우저 드라이버가 기동하지 못했습니다(메모리 부족일 수 있음)"
        return SearchOutcome("browser", TRANSPORT_FAILED, (), 0, reason)

    if isinstance(raw, dict):
        blocks = int(raw.get("blocks") or 0)
        extracted = raw.get("items")
    else:  # 예전 배열 형태(방어적)
        blocks, extracted = (len(raw) if isinstance(raw, list) else 0), raw
    items = _collect_items(extracted if isinstance(extracted, list) else [], count)
    if items:
        return SearchOutcome("browser", OK, items, blocks)
    if status is not None and status != 200:
        return SearchOutcome("browser", BLOCKED, (), blocks, f"HTTP {status}")
    if blocks == 0:
        return SearchOutcome("browser", PARSE_FAILED, (), 0, "결과 블록 0개")
    return SearchOutcome("browser", EMPTY, (), blocks, f"블록 {blocks}개, 링크 0개")


# ── 결과 선택과 보고 (순수 함수) ────────────────────────────────────────────

_TRANSPORT_LABEL = {"http": "직접 요청", "browser": "브라우저"}
_STATUS_LABEL = {
    BLOCKED: "차단",
    PARSE_FAILED: "파싱 실패",
    EMPTY: "결과 없음",
    TRANSPORT_FAILED: "연결 실패",
}


def format_results(query: str, items: tuple[tuple[str, str, str], ...]) -> str:
    lines = [
        f"'{query}' 검색 결과 {len(items)}건 — 관련 있는 URL 여러 개를 web_fetch로 열어 본문을 읽고 "
        "출처와 함께 종합하세요 (최상단 하나만 보지 말 것).",
        "",
    ]
    for i, (title, url, snippet) in enumerate(items, 1):
        lines.append(f"{i}. {title}")
        lines.append(f"   {url}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


def select_search_result(outcomes: list[SearchOutcome], query: str) -> str:
    """여러 전송 경로의 결과 중 하나를 골라 사용자/모델에게 줄 문자열을 만든다.

    성공한 경로가 있으면 그 결과만 낸다 — 출력 형식은 예전과 바이트 동일하다.
    전부 실패하면 **각 경로의 사유를 함께** 보고한다. 마지막 실패만 보고하면
    어느 층이 무너졌는지 알 수 없고, 그게 이 도구의 예전 실패 방식이었다.
    """
    if not outcomes:
        return f"[검색 실패] '{query}' — 검색을 시도하지 못했습니다."

    for outcome in outcomes:
        if outcome.status == OK:
            return format_results(query, outcome.items)

    reasons = " · ".join(
        f"{_TRANSPORT_LABEL.get(o.transport, o.transport)}: "
        f"{_STATUS_LABEL.get(o.status, o.status)}"
        + (f"({o.detail})" if o.detail else "")
        for o in outcomes
    )
    # 진짜 0건(블록은 있는데 링크가 없음)만 '키워드를 바꿔 보라'가 유효한 조언이다.
    if all(o.status == EMPTY for o in outcomes):
        return (
            f"[검색 결과 없음] '{query}' — 결과 블록은 확인했으나 열어볼 수 있는 링크가 "
            f"없습니다 ({reasons}). 키워드를 바꿔 다시 시도하세요."
        )
    return (
        f"[검색 실패] '{query}' — {reasons}. "
        "웹 근거를 얻지 못했으므로 아는 대로 답하지 말고, 사용자에게 검색이 실패했다고 "
        "알리거나 잠시 뒤 다시 시도하세요."
    )


# ── 구동 ────────────────────────────────────────────────────────────────────

def _search_sync(query: str, count: int, *, sleep=time.sleep) -> str:
    """평문 HTTP를 먼저, 실패하면 브라우저로. 전송 실패만 재시도한다.

    재시도 대상을 좁히는 것이 중요하다 — 차단·0건·마크업 변경은 서버가 준 답이라
    다시 물어도 같은 답이 온다. 그런 것까지 재시도하면 실패한 검색이 몇 배 느려질 뿐이다.
    """
    outcomes: list[SearchOutcome] = []
    for fetch in (_fetch_http, _fetch_browser):
        outcome = fetch(query, count)
        for _ in range(HTTP_RETRIES):
            if not outcome.retryable:
                break
            sleep(RETRY_BACKOFF)
            outcome = fetch(query, count)
        outcomes.append(outcome)
        if outcome.status == OK:
            break
    return select_search_result(outcomes, query)


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
