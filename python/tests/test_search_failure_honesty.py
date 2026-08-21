# -*- coding: utf-8 -*-
"""검색이 근거를 못 얻으면 성공으로 보고하지 않는다.

web_search 는 DuckDuckGo HTML을 CSS 클래스로 스크레이핑한다. 마크업이 바뀌거나
안티봇 챌린지(HTTP 202)에 걸리면 결과를 하나도 못 뽑는데, 예전에는 그 경우도
예외 없이 "[검색 결과 없음]" 문자열을 **정상 반환**했고 조사 루프가 그걸 성공으로
취급했다(`"ok": True` 고정).

그 결과가 실패보다 나쁘다:
  - 루프는 계속 돌면서 모델이 **기억으로** 답을 쓴다 (조용한 오답)
  - URL이 없는데 fetch 넛지가 "URL을 열어 확인하라"고 밀어붙여 모델이 URL을 지어낸다
  - 사용자는 "웹을 조사했다"고 믿는다

여기서 고정하는 계약:
  - 차단(HTTP≠200) / 파싱 실패(블록 0) / 진짜 0건을 구분해 보고한다
  - 열어볼 수 있는 URL이 없는 검색 결과는 ok=False 다
"""
from __future__ import annotations

import websearch
from websearch import search_result_is_evidence


# ── 근거 판정 ─────────────────────────────────────────────────────────

def test_result_with_urls_is_evidence():
    assert search_result_is_evidence("'x' 검색 결과 2건\n1. 제목 — https://example.com/a")


def test_blocked_parse_failed_and_empty_are_all_not_evidence():
    """세 실패 문구 모두 근거가 아니다 — 모델이 열어볼 URL이 없다."""
    for text in (
        "[검색 차단] 'x' — 검색 엔진이 HTTP 202로 응답했습니다",
        "[검색 파싱 실패] 'x' — 결과 블록을 찾지 못했습니다",
        "[검색 결과 없음] 'x' — 결과 블록 3개를 확인했으나 열어볼 수 있는 링크가 없습니다.",
        "[검색 불가] 헤드리스 브라우저 실행 실패: RuntimeError: nope.",
    ):
        assert not search_result_is_evidence(text), text


def test_empty_and_garbage_are_not_evidence():
    assert not search_result_is_evidence("")
    assert not search_result_is_evidence(None)  # type: ignore[arg-type]


# ── 추출 스크립트가 블록 수를 함께 돌려준다 ────────────────────────────

def test_extract_script_reports_block_count():
    """블록 수가 없으면 '마크업이 바뀌었다'와 '결과가 0건이다'를 구분할 수 없다."""
    assert "blocks: blocks.length" in websearch._EXTRACT_JS
    assert "items: res" in websearch._EXTRACT_JS


# ── 조사 루프가 근거 없는 검색을 성공으로 보고하지 않는다 ──────────────

NL = chr(10)


def test_research_loop_marks_evidence_free_search_as_failed():
    from agent_research import _tool_result_ok

    assert _tool_result_ok("web_search", "1. 예시 — https://example.com/a") is True
    assert _tool_result_ok("web_search", "[검색 파싱 실패] 'x' — 결과 블록을 찾지 못했습니다") is False
    # web_fetch 도 같은 판정을 받는다. 차단·추출 실패를 예외가 아니라 문자열로
    # 돌려주므로, "돌아왔으니 성공"으로 두면 대기 페이지를 읽고도 성공으로 보고된다.
    body = "본문 " * 80
    assert _tool_result_ok("web_fetch", "[https://e.com] . 제목" + NL + NL + body) is True
    assert _tool_result_ok("web_fetch", "[차단] 사설/내부 IP") is False
    assert _tool_result_ok(
        "web_fetch", "[https://e.com] 본문 텍스트를 추출하지 못했습니다 (JS 전용/차단 페이지일 수 있음)."
    ) is False
    # 도구가 아닌 이름은 이 판정을 적용하지 않는다
    assert _tool_result_ok("read_file", "무엇이든") is True
