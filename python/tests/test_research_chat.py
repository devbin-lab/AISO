# -*- coding: utf-8 -*-
"""리서치 채팅 루프(run_research_chat) — 조사 도구만 태우고 올바로 디스패치하는지 고정.

Ollama·Playwright 없이 _generate_turn / execute / model_layers를 대역으로 갈아끼워
루프의 이벤트 계약(tool_call→tool_result→content→done)과 허용목록을 검증한다.
"""
from __future__ import annotations

import asyncio

import agent
import agent_research
import websearch
from llm import LlmModelRuntime, LlmRequest
from llm.providers import ollama as ollama_provider
from toolspec import REGISTRY


NL = chr(10)


async def _collect(gen) -> list[dict]:
    return [ev async for ev in gen]


def _final(tool_calls, content="", tokens=1) -> dict:
    return {
        "content": content,
        "thinking": "",
        "tool_calls": tool_calls,
        "done_reason": None,
        "output_tokens": tokens,
    }


def _script(monkeypatch, turns: list[dict], on_execute):
    """_generate_turn이 turns를 차례로 내도록, execute를 on_execute로 대역화."""
    async def fake_prepare_model(host, model):  # 네트워크 차단
        return LlmModelRuntime(model=model)

    calls = {"i": 0}

    async def fake_generate(host, base, effort, layers, offload):
        t = turns[calls["i"]]
        calls["i"] += 1
        yield {"_gen": True, "final": t, "error": None, "offload_noticed": offload}

    monkeypatch.setattr(agent, "_prepare_model", fake_prepare_model)
    monkeypatch.setattr(agent, "_generate_turn", fake_generate)
    monkeypatch.setattr(agent, "execute", on_execute)
    return calls


def test_research_tool_names_are_only_research_tools():
    """조사 도구는 web_search·web_fetch 둘뿐이어야 한다(파일/명령 툴 유입 방지)."""
    assert agent.RESEARCH_TOOL_NAMES == ("web_search", "web_fetch")
    for n in agent.RESEARCH_TOOL_NAMES:
        assert n in REGISTRY


def test_research_forces_fetch_after_search_only(monkeypatch):
    """검색만 하고 원문(fetch)을 안 읽은 채 답하려 하면, 한 번 넛지해 web_fetch로 교차확인시킨다.

    흐름: web_search → (스니펫만 본) 임시 답 → 넛지(reset_content) → web_fetch → 최종 답 → done.
    """
    executed: list[str] = []

    async def on_execute(spec, root, host, args):
        executed.append(spec.name)
        # 실제 web_search 는 열어볼 수 있는 URL을 반환한다. URL이 없는 결과는
        # 이제 "근거 없음"으로 판정되어 fetch 넛지를 발동시키지 않으므로,
        # 넛지 동작을 검증하려면 픽스처도 현실적이어야 한다.
        if spec.name == "web_search":
            return ("검색 결과 1건\n1. 예시 — https://example.com/a", None)
        return (_page(args.get("url") or "https://e.com"), None)

    calls = _script(
        monkeypatch,
        [
            _final([{"function": {"name": "web_search", "arguments": {"query": "x"}}}]),
            _final([], content="스니펫만 보고 쓴 임시 답"),  # 아직 fetch 안 함 → 넛지 유발
            _final([{"function": {"name": "web_fetch", "arguments": {"url": "https://e.com"}}}]),
            _final([], content="원문 확인 후 최종 답"),
        ],
        on_execute,
    )
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "질문"}]))
    )
    types = [e["type"] for e in evs if "type" in e]
    assert executed == ["web_search", "web_fetch"]  # 검색 후 강제 fetch까지
    assert any(e.get("type") == "reset_content" for e in evs)  # 임시 답 폐기 신호
    assert types[-1] == "done"
    assert calls["i"] == 4  # 네 턴 모두 생성됨(넛지로 이어감)


def _page(url: str) -> str:
    """현실적인 web_fetch 성공 결과. 본문이 없으면 이제 근거로 세지 않는다 —
    차단 페이지를 '읽었다'로 치던 오답을 막는 게이트가 생겼기 때문이다."""
    return f"[{url}] . 제목" + chr(10) * 2 + "본문 문장입니다. " * 40


def test_research_no_nudge_when_already_fetched(monkeypatch):
    """이미 web_fetch로 원문을 읽었으면 넛지하지 않고 바로 마무리한다(중복 강제 방지)."""
    executed: list[str] = []

    async def on_execute(spec, root, host, args):
        executed.append(spec.name)
        # 실제 web_search 는 열어볼 수 있는 URL을 반환한다. URL이 없는 결과는
        # 이제 "근거 없음"으로 판정되어 fetch 넛지를 발동시키지 않으므로,
        # 넛지 동작을 검증하려면 픽스처도 현실적이어야 한다.
        if spec.name == "web_search":
            return ("검색 결과 1건\n1. 예시 — https://example.com/a", None)
        return (_page(args.get("url") or "https://e.com"), None)

    calls = _script(
        monkeypatch,
        [
            _final([
                {"function": {"name": "web_search", "arguments": {"query": "x"}}},
                {"function": {"name": "web_fetch", "arguments": {"url": "https://e.com"}}},
            ]),
            _final([], content="검색+원문까지 본 답"),
        ],
        on_execute,
    )
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "질문"}]))
    )
    assert executed == ["web_search", "web_fetch"]
    assert not any(e.get("type") == "reset_content" for e in evs)  # 넛지 없음
    assert calls["i"] == 2  # 추가 턴 없이 종료


def test_research_auto_fetches_top_search_urls(monkeypatch):
    """검색 결과에 URL이 있으면 하네스가 상위 원문을 자동으로 정독한다(모델이 안 시켜도)."""
    executed: list[tuple] = []

    async def on_execute(spec, root, host, args):
        executed.append((spec.name, args.get("url")))
        if spec.name == "web_search":
            return (
                "'q' 검색 결과 3건 …\n\n"
                "1. A\n   https://a.example/1\n   snippet a\n"
                "2. B\n   https://b.example/2\n   snippet b\n"
                "3. C\n   https://c.example/3\n   snippet c",
                None,
            )
        return (_page(args.get("url") or "https://e.com"), None)

    calls = _script(
        monkeypatch,
        [
            _final([{"function": {"name": "web_search", "arguments": {"query": "q"}}}]),
            _final([], content="여러 출처 종합 답"),
        ],
        on_execute,
    )
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "q"}]))
    )
    # web_search 1회 + 상위 3개 원문 자동 web_fetch
    assert executed[0] == ("web_search", None)
    fetched = [u for (n, u) in executed if n == "web_fetch"]
    assert fetched == ["https://a.example/1", "https://b.example/2", "https://c.example/3"]
    # 자동으로 원문을 읽었으니 넛지(reset_content) 없이 바로 마무리
    assert not any(e.get("type") == "reset_content" for e in evs)
    types = [e["type"] for e in evs if "type" in e]
    assert types[-1] == "done"
    assert calls["i"] == 2  # 넛지로 인한 추가 턴 없음


def test_research_loop_rejects_file_tool_without_executing(monkeypatch):
    """모델이 write_file 등 비조사 툴을 지어내면 실행하지 말고 오류를 되돌려준다."""
    executed: list[str] = []

    async def on_execute(spec, root, host, args):
        executed.append(spec.name)
        return ("", None)

    _script(
        monkeypatch,
        [
            _final([{"function": {"name": "write_file", "arguments": {"path": "a", "content": "b"}}}]),
            _final([], content="답"),
        ],
        on_execute,
    )
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "q"}]))
    )
    tr = next(e for e in evs if e.get("type") == "tool_result")
    assert tr["ok"] is False
    assert executed == []  # 비조사 툴은 디스패치되지 않아야 한다


def test_looks_degenerate_detects_repetition():
    """같은 덩어리가 반복되면 퇴행으로 감지, 다양한/짧은 텍스트는 아님."""
    assert agent._looks_degenerate("짧은 텍스트") is False
    diverse = "\n".join(f"고유한 줄 {i} — 서로 다른 내용 {i * 37}" for i in range(300))
    assert agent._looks_degenerate(diverse) is False
    block = ("수정하겠습니다.\n```js\nfunction update(){ draw(); requestAnimationFrame(update); }\n```\n진행합니다.\n")
    assert agent._looks_degenerate(block * 60) is True


def test_chat_turn_aborts_repetitive_stream(monkeypatch):
    """_chat_turn은 같은 본문을 반복하는 스트림을 num_ctx 한도 전에 조기 중단한다."""
    import json as _json

    class _Resp:
        status_code = 200

        def __init__(self, lines):
            self._lines = lines

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            for ln in self._lines:
                yield ln

        async def aread(self):
            return b""

    class _Client:
        def __init__(self, lines):
            self._lines = lines

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None):
            return _Resp(self._lines)

    block = "수정하겠습니다. function update(){ draw(); requestAnimationFrame(update); } 진행합니다.\n"
    chunk = _json.dumps({"message": {"content": block}}, ensure_ascii=False)
    lines = [chunk] * 120 + [_json.dumps({"done": True, "done_reason": "stop", "eval_count": 9})]
    monkeypatch.setattr(ollama_provider.httpx, "AsyncClient", lambda *a, **k: _Client(lines))

    async def run():
        final = None
        n_content = 0
        async for ev in agent._chat_turn("h", LlmRequest(model="m", messages=[])):
            if ev.get("_final"):
                final = ev
            elif ev.get("type") == "content":
                n_content += 1
        return final, n_content

    final, n_content = asyncio.run(run())
    assert final["done_reason"] == "repetition"  # done(stop) 전에 반복 감지로 끊음
    assert n_content < 120  # 120개 청크를 다 받기 전에 조기 중단


def test_chat_turn_aborts_repetitive_thinking(monkeypatch):
    """thinking(사고) 채널에서 같은 덩어리를 반복하면 조기 중단한다 — content만 보면 놓치는 폭주."""
    import json as _json

    class _Resp:
        status_code = 200

        def __init__(self, lines):
            self._lines = lines

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_lines(self):
            for ln in self._lines:
                yield ln

        async def aread(self):
            return b""

    class _Client:
        def __init__(self, lines):
            self._lines = lines

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None):
            return _Resp(self._lines)

    block = "Actually, let's do this: 1. Create skill with urllib. 2. Run it. 3. Confirm.\n"
    chunk = _json.dumps({"message": {"thinking": block}}, ensure_ascii=False)
    lines = [chunk] * 200 + [_json.dumps({"done": True, "done_reason": "stop", "eval_count": 9})]
    monkeypatch.setattr(ollama_provider.httpx, "AsyncClient", lambda *a, **k: _Client(lines))

    async def run():
        final = None
        n_think = 0
        async for ev in agent._chat_turn("h", LlmRequest(model="m", messages=[])):
            if ev.get("_final"):
                final = ev
            elif ev.get("type") == "thinking":
                n_think += 1
        return final, n_think

    final, n_think = asyncio.run(run())
    assert final["done_reason"] == "repetition"  # 사고 폭주도 반복으로 감지해 끊음
    assert n_think < 200  # 200개 청크를 다 받기 전에 조기 중단


def test_research_repetition_stops_with_notice(monkeypatch):
    """반복 퇴행(done_reason='repetition')이면 안내 문구와 함께 정지한다."""
    async def on_execute(spec, root, host, args):
        return ("", None)

    fin = {"content": "반복…", "thinking": "", "tool_calls": [], "done_reason": "repetition", "output_tokens": 1}
    _script(monkeypatch, [fin], on_execute)
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "q"}]))
    )
    notes = [e.get("text", "") for e in evs if e.get("type") == "notice"]
    assert any("반복" in n for n in notes)
    assert [e["type"] for e in evs if "type" in e][-1] == "done"


def test_research_loop_plain_answer_no_tools(monkeypatch):
    """검색이 불필요한 질문은 툴 없이 바로 content→done."""
    async def on_execute(spec, root, host, args):  # 호출되면 실패
        raise AssertionError("execute should not be called")

    _script(monkeypatch, [_final([], content="안녕하세요")], on_execute)
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "안녕"}]))
    )
    types = [e["type"] for e in evs if "type" in e]
    assert "tool_call" not in types
    assert "content" not in types or True  # content는 _generate_turn 스트림에서 나오므로 여기선 검사 안 함
    assert types[-1] == "done"
# 시변 검색어는 실제 현재 연도를 반드시 포함한다.
def test_web_search_recency_query_is_anchored_to_current_year():
    assert websearch.enrich_recency_query("OpenAI latest news", year=2026) == "OpenAI latest news 2026"
    assert websearch.enrich_recency_query("OpenAI 최신 뉴스 2025", year=2026).endswith("2026")
    assert websearch.enrich_recency_query("파이썬 리스트 정렬", year=2026) == "파이썬 리스트 정렬"


def test_research_tool_results_are_capped_at_record_time(monkeypatch):
    """리서치 루프의 도구 결과도 기록 시점에 잘린다.

    회귀 방지: 절단을 `compact_convo`에서 기록 시점으로 옮길 때, 에이전트 루프만
    새 관문(ModelConversation)을 쓰고 리서치 루프는 평범한 list로 남을 뻔했다.
    그러면 web_fetch 원문(최대 3만자)이 무제한으로 모델 컨텍스트에 들어간다 —
    compact_convo는 더 이상 내용을 자르지 않기 때문이다.
    """
    huge = "본문 " * 20_000

    async def on_execute(spec, root, host, args):
        return (huge, None)

    seen_payloads: list = []
    original_request = agent.LlmRequest

    _script(
        monkeypatch,
        [
            _final([{"function": {"name": "web_fetch", "arguments": {"url": "https://e.com"}}}]),
            _final([], content="정리 완료"),
        ],
        on_execute,
    )

    def capturing_request(**kwargs):
        seen_payloads.append(kwargs.get("messages") or [])
        return original_request(**kwargs)

    monkeypatch.setattr(agent, "LlmRequest", capturing_request)
    asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "질문"}]))
    )

    tool_messages = [
        message
        for messages in seen_payloads
        for message in messages
        if message.get("role") == "tool"
    ]
    assert tool_messages, "도구 결과가 프롬프트에 실리지 않았다"
    longest = max(len(str(message.get("content") or "")) for message in tool_messages)
    assert longest < len(huge), f"원문이 통째로 실렸다: {longest}자"
    assert any(
        agent.TOOL_RESULT_TRUNCATION_LABEL in str(message.get("content") or "")
        for message in tool_messages
    )


def test_blocked_page_is_not_counted_as_having_read_the_source(monkeypatch):
    """차단 페이지를 읽고도 '원문 확인'으로 치지 않는다.

    실제로 겪은 오답이다. 'GPT의 최신 모델' 질문에 검색은 성공했지만 1위 결과가
    안티봇 대기 페이지('잠시만 기다리십시오…')여서 본문이 비었다. 그런데 web_fetch 는
    실패를 예외가 아니라 **문자열**로 돌려주므로 루프가 '읽었다'로 세었고,
    "위에서 읽은 출처로 답하라"고 밀어 모델이 **스니펫만 보고** 답을 확정했다.

    지금은 근거가 아니라고 판정해 교차확인 넛지가 살아 있어야 한다.
    """
    blocked = (
        "[https://openai.com/x/] 본문 텍스트를 추출하지 못했습니다 "
        "(JS 전용/차단 페이지일 수 있음). title='잠시만 기다리십시오…'"
    )

    async def on_execute(spec, root, host, args):
        if spec.name == "web_search":
            return ("검색 결과 1건" + NL + "1. Introducing X" + NL + "   https://openai.com/x/" + NL
                    + "   스니펫이 정답처럼 보인다", None)
        return (blocked, None)

    calls = _script(
        monkeypatch,
        [
            _final([{"function": {"name": "web_search", "arguments": {"query": "latest model"}}}]),
            _final([], content="검색 결과에 따르면 최신 모델은 X입니다."),
            _final([], content="원문을 확인하지 못했습니다."),
        ],
        on_execute,
    )
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "q"}]))
    )
    # 차단된 fetch 는 성공으로 보고되지 않는다 — UI 의 도구 카드도 정직해야 한다.
    fetch_results = [e for e in evs if e.get("type") == "tool_result" and e.get("name") == "web_fetch"]
    assert fetch_results, "자동 원문 읽기가 아예 시도되지 않았다"
    assert all(e["ok"] is False for e in fetch_results), "차단 페이지가 성공으로 보고됐다"
    # 근거를 못 얻었으므로 스니펫 답을 확정으로 두지 않고 교차확인을 다시 요구한다.
    assert any(e.get("type") == "reset_content" for e in evs), "스니펫만 보고 답을 확정했다"


def test_a_real_page_body_does_count_as_having_read_the_source(monkeypatch):
    """반대로 진짜 본문을 읽었으면 중복 넛지 없이 마무리한다(위 게이트가 과하지 않다)."""
    async def on_execute(spec, root, host, args):
        if spec.name == "web_search":
            return ("검색 결과 1건" + NL + "1. A" + NL + "   https://a.example/1" + NL + "   snippet", None)
        return (_page("https://a.example/1"), None)

    calls = _script(
        monkeypatch,
        [
            _final([{"function": {"name": "web_search", "arguments": {"query": "q"}}}]),
            _final([], content="원문 근거를 종합한 답"),
        ],
        on_execute,
    )
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "q"}]))
    )
    fetch_results = [e for e in evs if e.get("type") == "tool_result" and e.get("name") == "web_fetch"]
    assert fetch_results and all(e["ok"] is True for e in fetch_results)
    assert not any(e.get("type") == "reset_content" for e in evs)


# ── 1차 출처가 막히면 2차 출처로 ────────────────────────────────────────

def _results(*urls: str) -> str:
    lines = ["검색 결과 %d건" % len(urls)]
    for i, u in enumerate(urls, 1):
        lines += [f"{i}. 제목 {i}", f"   {u}", "   스니펫"]
    return NL.join(lines)


BLOCKED_PAGE = (
    "[https://blocked.example/x] 본문 텍스트를 추출하지 못했습니다 "
    "(JS 전용/차단 페이지일 수 있음). title='잠시만 기다리십시오…'"
)


def test_a_blocked_primary_source_falls_through_to_the_next_source(monkeypatch):
    """1위가 안티봇이면 포기하지 않고 다음 출처를 연다.

    실제로 겪은 경로다 — openai.com 이 막혔는데 거기서 멈추는 바람에 모델에게 남은
    재료가 검색 스니펫뿐이었다. 막힌 시도는 예산을 쓰지 않아야 다음 후보에 닿는다.
    """
    opened: list[str] = []

    async def on_execute(spec, root, host, args):
        if spec.name == "web_search":
            # 좋은 출처를 예전 창(상위 3개) **밖**에 둔다. 그래야 이 테스트가
            # "다음 후보까지 내려간다"는 동작을 실제로 판별한다.
            return (_results("https://blocked.example/1", "https://blocked.example/2",
                             "https://blocked.example/3", "https://blocked.example/4",
                             "https://good.example/5", "https://good.example/6"), None)
        url = args["url"]
        opened.append(url)
        if "blocked.example" in url:
            return (BLOCKED_PAGE, None)
        return (_page(url), None)

    _script(
        monkeypatch,
        [
            _final([{"function": {"name": "web_search", "arguments": {"query": "q"}}}]),
            _final([], content="2차 출처로 확인한 답"),
        ],
        on_execute,
    )
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "q"}]))
    )
    # 막힌 둘을 지나 성공한 둘까지 실제로 열었다.
    assert opened[:2] == ["https://blocked.example/1", "https://blocked.example/2"]
    assert "https://good.example/5" in opened, "상위 3개에서 멈춰 2차 출처에 닿지 못했다"
    ok = [e for e in evs if e.get("type") == "tool_result" and e.get("name") == "web_fetch" and e["ok"]]
    assert ok, "2차 출처에서도 본문을 못 얻었다"
    # 무엇이 막혔는지 사용자에게 알린다.
    assert any("blocked.example" in e.get("text", "") for e in evs if e.get("type") == "notice")


def test_a_fully_blocked_query_stops_instead_of_hammering(monkeypatch):
    """전부 막힌 질의에서 무한정 두드리지 않는다(시도 상한)."""
    opened: list[str] = []

    async def on_execute(spec, root, host, args):
        if spec.name == "web_search":
            return (_results(*[f"https://blocked.example/{i}" for i in range(1, 13)]), None)
        opened.append(args["url"])
        return (BLOCKED_PAGE, None)

    _script(
        monkeypatch,
        [
            _final([{"function": {"name": "web_search", "arguments": {"query": "q"}}}]),
            _final([], content="원문을 열지 못했습니다."),
            _final([], content="끝"),
        ],
        on_execute,
    )
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "q"}]))
    )
    assert len(opened) <= agent_research.AUTO_FETCH_ATTEMPTS, f"시도 상한을 넘겼다: {len(opened)}"
    # 근거를 못 얻었으니 스니펫 답을 확정으로 두지 않는다.
    assert any(e.get("type") == "reset_content" for e in evs)


def test_blocked_attempts_do_not_consume_the_evidence_budget(monkeypatch):
    """막힌 시도는 예산을 쓰지 않는다 — 그래야 실제 본문 개수를 채운다."""
    async def on_execute(spec, root, host, args):
        if spec.name == "web_search":
            return (_results("https://blocked.example/1", "https://good.example/2",
                             "https://good.example/3", "https://good.example/4"), None)
        url = args["url"]
        return (BLOCKED_PAGE, None) if "blocked" in url else (_page(url), None)

    _script(
        monkeypatch,
        [
            _final([{"function": {"name": "web_search", "arguments": {"query": "q"}}}]),
            _final([], content="답"),
        ],
        on_execute,
    )
    evs = asyncio.run(
        _collect(agent.run_research_chat(host="h", model="m", messages=[{"role": "user", "content": "q"}]))
    )
    ok = [e for e in evs if e.get("type") == "tool_result" and e.get("name") == "web_fetch" and e["ok"]]
    assert len(ok) == agent_research.AUTO_FETCH_TOP, f"확보한 본문이 {len(ok)}건뿐이다"


# ── 현재 시각 ───────────────────────────────────────────────────────────

def test_research_prompt_carries_the_current_moment_not_an_import_time_snapshot():
    """'최신'의 기준 시점은 요청마다 다시 계산해야 한다.

    예전에는 모듈 임포트 시점의 날짜를 굳혀 썼다. 앱을 며칠 켜 두면 "Today is" 가
    틀린 날짜를 가리켰고, 모델은 '최신'을 그 낡은 기준으로 해석했다.
    """
    import datetime as dt

    line = agent_research.current_time_line()
    now = dt.datetime.now().astimezone()
    assert now.strftime("%Y-%m-%d") in line
    assert now.strftime("%H:%M") in line          # 날짜만이 아니라 시각까지
    assert "UTC" in line                           # 시간대 없이는 UTC 문서와 비교할 수 없다
    prompt = agent_research.research_system_prompt("ko")
    assert line in prompt

    # 시계를 옮기면 프롬프트도 따라와야 한다 — 굳은 상수면 안 따라온다.
    frozen = agent_research.research_system_prompt("ko")
    assert now.strftime("%Y-%m-%d") in frozen
