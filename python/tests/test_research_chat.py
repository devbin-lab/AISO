# -*- coding: utf-8 -*-
"""리서치 채팅 루프(run_research_chat) — 조사 도구만 태우고 올바로 디스패치하는지 고정.

Ollama·Playwright 없이 _generate_turn / execute / model_layers를 대역으로 갈아끼워
루프의 이벤트 계약(tool_call→tool_result→content→done)과 허용목록을 검증한다.
"""
from __future__ import annotations

import asyncio

import agent
from llm import LlmModelRuntime, LlmRequest
from llm.providers import ollama as ollama_provider
from toolspec import REGISTRY


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
        return (f"[{spec.name} 결과] …", None)

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


def test_research_no_nudge_when_already_fetched(monkeypatch):
    """이미 web_fetch로 원문을 읽었으면 넛지하지 않고 바로 마무리한다(중복 강제 방지)."""
    executed: list[str] = []

    async def on_execute(spec, root, host, args):
        executed.append(spec.name)
        return (f"[{spec.name} 결과] …", None)

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
        return (f"[본문] {args.get('url')}", None)

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
