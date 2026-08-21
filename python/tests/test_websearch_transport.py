"""web_search 전송 경로 — 브라우저 없이도 되고, 실패해도 조용히 넘어가지 않는다.

이 파일이 고정하는 것은 사용자가 겪은 실패다:
    [검색 불가] 헤드리스 브라우저 실행 실패: AttributeError:
    'PlaywrightContextManager' object has no attribute '_playwright'.
검색 한 번마다 브라우저를 띄웠고, 메모리가 빠듯하면 그 기동이 실패했으며,
그 실패가 런 전체를 멈췄다.
"""

from __future__ import annotations

import websearch
from websearch import (
    BLOCKED,
    EMPTY,
    OK,
    PARSE_FAILED,
    TRANSPORT_FAILED,
    SearchOutcome,
    parse_ddg_html,
    select_search_result,
)


# ── 마크업 파서 ─────────────────────────────────────────────────────────────

def _result_block(title: str, href: str, snippet: str) -> str:
    return f"""
      <div class="result results_links results_links_deep web-result ">
        <div class="links_main links_deep result__body">
          <h2 class="result__title">
            <a rel="nofollow" class="result__a" href="{href}">{title}</a>
          </h2>
          <a class="result__snippet" href="{href}">{snippet}</a>
        </div>
      </div>
    """


def test_parser_extracts_title_url_and_snippet() -> None:
    markup = "<html><body>" + _result_block(
        "How to Use Asyncio", "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa", "요약 문장"
    ) + "</body></html>"
    blocks, items = parse_ddg_html(markup)
    assert blocks == 1
    assert items[0]["title"] == "How to Use Asyncio"
    assert items[0]["snippet"] == "요약 문장"
    assert websearch._decode_ddg_url(items[0]["href"]) == "https://example.com/a"


def test_parser_counts_blocks_even_when_nothing_is_extractable() -> None:
    # 이 구분이 없으면 '마크업이 바뀌었다'와 '결과가 정말 0건이다'를 못 나눈다.
    markup = '<div class="result"><div class="links_main">링크 없는 블록</div></div>'
    blocks, items = parse_ddg_html(markup)
    assert blocks == 1
    assert items == []


def test_parser_survives_truncated_markup() -> None:
    markup = "<html><body>" + _result_block("잘린 문서", "https://example.com/a", "요약")
    blocks, items = parse_ddg_html(markup)
    assert blocks == 1 and len(items) == 1


def test_parser_does_not_double_decode_encoded_urls() -> None:
    # Wikipedia C%2B%2B → C++ 이중 디코드 회귀 방지.
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fen.wikipedia.org%2Fwiki%2FC%252B%252B"
    markup = _result_block("C++", href, "")
    _, items = parse_ddg_html(markup)
    assert websearch._decode_ddg_url(items[0]["href"]) == "https://en.wikipedia.org/wiki/C%2B%2B"


def test_status_classification_separates_the_three_failures() -> None:
    two = "<html>" + _result_block("A", "https://a.example/", "x") + "</html>"
    assert websearch._outcome_from_page("http", 200, two, 5).status == OK
    # 200이 아니면 차단 — DuckDuckGo는 안티봇 챌린지에서 202를 낸다.
    assert websearch._outcome_from_page("http", 202, "<html></html>", 5).status == BLOCKED
    # 200인데 블록이 하나도 없으면 마크업 변경이거나 200으로 위장한 차단이다.
    assert websearch._outcome_from_page("http", 200, "<html></html>", 5).status == PARSE_FAILED
    # 블록은 있는데 링크가 없으면 진짜 0건이다.
    empty_block = '<div class="result"><div>링크 없음</div></div>'
    assert websearch._outcome_from_page("http", 200, empty_block, 5).status == EMPTY


# ── 결과 선택 (순수 함수) ───────────────────────────────────────────────────

def _ok(transport: str = "http") -> SearchOutcome:
    return SearchOutcome(transport, OK, (("제목", "https://example.com/a", "요약"),), 1)


def test_a_successful_transport_wins_and_keeps_the_old_output_shape() -> None:
    out = select_search_result([_ok()], "질의")
    assert out.startswith("'질의' 검색 결과 1건 — 관련 있는 URL 여러 개를 web_fetch로")
    assert "1. 제목" in out and "   https://example.com/a" in out
    assert websearch.search_result_is_evidence(out)


def test_a_browser_fallback_after_an_http_block_still_returns_results() -> None:
    out = select_search_result(
        [SearchOutcome("http", BLOCKED, (), 0, "HTTP 202"), _ok("browser")], "질의"
    )
    assert "https://example.com/a" in out
    assert websearch.search_result_is_evidence(out)


def test_every_transport_failure_is_named_not_just_the_last_one() -> None:
    # 예전 실패 방식이 정확히 이것이었다 — 마지막 실패만 남아 어느 층이 무너졌는지 잃었다.
    out = select_search_result(
        [
            SearchOutcome("http", BLOCKED, (), 0, "HTTP 202"),
            SearchOutcome("browser", TRANSPORT_FAILED, (), 0, "브라우저 드라이버가 기동하지 못했습니다"),
        ],
        "질의",
    )
    assert "직접 요청" in out and "HTTP 202" in out
    assert "브라우저" in out and "드라이버" in out
    assert not websearch.search_result_is_evidence(out), "실패가 근거로 취급됐다"


def test_a_total_failure_tells_the_model_not_to_answer_from_memory() -> None:
    out = select_search_result([SearchOutcome("http", PARSE_FAILED, (), 0, "결과 블록 0개")], "질의")
    assert "아는 대로 답하지 말고" in out
    assert not websearch.search_result_is_evidence(out)


def test_a_genuine_zero_result_advises_changing_keywords() -> None:
    out = select_search_result([SearchOutcome("http", EMPTY, (), 7, "블록 7개, 링크 0개")], "질의")
    assert "[검색 결과 없음]" in out
    assert "키워드를 바꿔" in out


def test_no_outcome_at_all_is_still_reported_as_failure() -> None:
    out = select_search_result([], "질의")
    assert out.startswith("[검색 실패]")
    assert not websearch.search_result_is_evidence(out)


# ── 구동: 전송 순서와 재시도 정책 ───────────────────────────────────────────

def test_the_browser_is_never_launched_when_plain_http_works(monkeypatch) -> None:
    # 이것이 이 변경의 핵심이다. 검색은 서버가 렌더한 정적 HTML을 여는 일이라
    # 브라우저가 필요 없고, 브라우저를 안 띄우면 기동 실패도 없다.
    launched: list[str] = []
    monkeypatch.setattr(websearch, "_fetch_http", lambda q, c: _ok())
    monkeypatch.setattr(
        websearch, "_fetch_browser",
        lambda q, c: launched.append("launched") or _ok("browser"),  # type: ignore[func-returns-value]
    )
    websearch._search_sync("질의", 5, sleep=lambda _s: None)
    assert launched == [], "HTTP가 성공했는데도 브라우저를 띄웠다"


def test_the_browser_takes_over_when_http_is_blocked(monkeypatch) -> None:
    monkeypatch.setattr(
        websearch, "_fetch_http", lambda q, c: SearchOutcome("http", BLOCKED, (), 0, "HTTP 202")
    )
    monkeypatch.setattr(websearch, "_fetch_browser", lambda q, c: _ok("browser"))
    out = websearch._search_sync("질의", 5, sleep=lambda _s: None)
    assert "https://example.com/a" in out


def test_a_transport_failure_is_retried_once(monkeypatch) -> None:
    # 사용자가 본 드라이버 기동 실패는 간헐적이다. 한 번의 실패로 런 전체를 멈추면 안 된다.
    calls: list[int] = []

    def flaky(query: str, count: int) -> SearchOutcome:
        calls.append(1)
        if len(calls) == 1:
            return SearchOutcome("http", TRANSPORT_FAILED, (), 0, "ConnectError")
        return _ok()

    monkeypatch.setattr(websearch, "_fetch_http", flaky)
    monkeypatch.setattr(websearch, "_fetch_browser", lambda q, c: SearchOutcome("browser", PARSE_FAILED))
    out = websearch._search_sync("질의", 5, sleep=lambda _s: None)
    assert len(calls) == 2, "전송 실패를 재시도하지 않았다"
    assert "https://example.com/a" in out


def test_a_server_answer_is_not_retried(monkeypatch) -> None:
    # 차단·0건·마크업 변경은 서버가 준 답이다. 다시 물어도 같은 답이 온다 —
    # 재시도하면 실패한 검색만 몇 배 느려진다.
    calls: list[int] = []

    def blocked(query: str, count: int) -> SearchOutcome:
        calls.append(1)
        return SearchOutcome("http", BLOCKED, (), 0, "HTTP 202")

    monkeypatch.setattr(websearch, "_fetch_http", blocked)
    monkeypatch.setattr(websearch, "_fetch_browser", lambda q, c: SearchOutcome("browser", BLOCKED))
    websearch._search_sync("질의", 5, sleep=lambda _s: None)
    assert len(calls) == 1, "서버가 답한 실패를 재시도했다"


def test_a_driver_startup_failure_is_translated_into_something_a_person_can_read(monkeypatch) -> None:
    # Playwright 는 드라이버 기동 실패를 `'PlaywrightContextManager' object has no
    # attribute '_playwright'` 로 드러낸다. 그대로 내보내면 사용자에게 아무 의미가 없다.
    class _Boom:
        def __enter__(self):
            raise AttributeError("'PlaywrightContextManager' object has no attribute '_playwright'")

        def __exit__(self, *a):
            return False

    import sys
    import types

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _Boom()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    outcome = websearch._fetch_browser("질의", 5)
    assert outcome.status == TRANSPORT_FAILED
    assert "_playwright" not in outcome.detail
    assert "드라이버" in outcome.detail and "메모리" in outcome.detail


# ── 상한과 중복 제거 ────────────────────────────────────────────────────────

def test_duplicate_urls_are_collapsed_and_count_is_respected() -> None:
    raw = [
        {"title": "A", "href": "https://example.com/a", "snippet": ""},
        {"title": "A 사본", "href": "https://example.com/a", "snippet": ""},
        {"title": "B", "href": "https://example.com/b", "snippet": ""},
        {"title": "C", "href": "https://example.com/c", "snippet": ""},
    ]
    items = websearch._collect_items(raw, 2)
    assert [u for _, u, _ in items] == ["https://example.com/a", "https://example.com/b"]


def test_a_long_snippet_is_truncated_with_an_ellipsis() -> None:
    raw = [{"title": "T", "href": "https://example.com/a", "snippet": "가" * 400}]
    (_, _, snippet), = websearch._collect_items(raw, 1)
    assert len(snippet) == websearch.MAX_SNIPPET + 1 and snippet.endswith("…")


def test_a_non_http_scheme_is_dropped() -> None:
    raw = [{"title": "T", "href": "javascript:alert(1)", "snippet": ""}]
    assert websearch._collect_items(raw, 5) == ()


# ── web_fetch 도 같은 드라이버 실패를 겪는다 ────────────────────────────────

def test_web_fetch_retries_a_driver_startup_failure(monkeypatch) -> None:
    # 검색과 달리 web_fetch 는 브라우저가 진짜로 필요하다 — DNS 리바인딩 재검증과
    # 서브리소스 SSRF 차단이 브라우저 경로에 묶여 있다. 그래서 평문 HTTP 로 바꾸는 대신
    # 간헐 실패를 재시도한다.
    import webfetch

    calls: list[int] = []

    def flaky(url: str) -> str:
        calls.append(1)
        if len(calls) == 1:
            return f"{webfetch._BROWSER_START_FAILURE} 헤드리스 브라우저 실행 실패: 드라이버."
        return "[https://example.com] 본문"

    monkeypatch.setattr(webfetch, "_fetch_once", flaky)
    out = webfetch._fetch_sync("https://example.com", sleep=lambda _s: None)
    assert len(calls) == 2, "드라이버 기동 실패를 재시도하지 않았다"
    assert out.startswith("[https://example.com]")


def test_web_fetch_does_not_retry_a_page_level_failure(monkeypatch) -> None:
    import webfetch

    calls: list[int] = []

    def blocked(url: str) -> str:
        calls.append(1)
        return "[차단] 사설/내부 IP로 해석되어 차단되었습니다"

    monkeypatch.setattr(webfetch, "_fetch_once", blocked)
    webfetch._fetch_sync("https://example.com", sleep=lambda _s: None)
    assert len(calls) == 1, "서버/정책이 답한 실패를 재시도했다"


def test_web_fetch_translates_the_driver_attribute_error() -> None:
    import webfetch

    reason = webfetch._driver_failure_reason(
        AttributeError("'PlaywrightContextManager' object has no attribute '_playwright'")
    )
    assert "_playwright" not in reason
    assert "드라이버" in reason and "메모리" in reason
    # 다른 예외는 그대로 보존한다 — 번역이 정보를 지우면 안 된다.
    assert "ValueError" in webfetch._driver_failure_reason(ValueError("무언가"))
