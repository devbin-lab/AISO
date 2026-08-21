"""Gate 1: Ollama 호환 계층의 golden 계약 및 직접 전송 경계."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import agent
from llm import LlmProviderError, LlmRequest, create_runtime
from llm.providers import ollama as ollama_provider


FIXTURE = Path(__file__).parent / "fixtures" / "ollama_chat_contract.json"


def _event_dict(event) -> dict:
    out = {"kind": event.kind}
    if event.text:
        out["text"] = event.text
    if event.tool_calls is not None:
        out["tool_calls"] = list(event.tool_calls)
    if event.output_tokens is not None:
        out["output_tokens"] = event.output_tokens
    # prompt_eval_count → input_tokens. 골든 픽스처가 이 매핑을 고정한다.
    if event.input_tokens is not None:
        out["input_tokens"] = event.input_tokens
    if event.total_duration is not None:
        out["total_duration"] = event.total_duration
    if event.done_reason is not None:
        out["done_reason"] = event.done_reason
    return out


def test_ollama_payload_and_ndjson_events_match_golden_fixture(monkeypatch):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    seen: dict = {}

    class FakeResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            for line in fixture["ndjson"]:
                yield line

        async def aread(self):
            return b""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, method, url, json=None):
            seen.update({"method": method, "url": url, "payload": json})
            return FakeResponse()

    monkeypatch.setattr(ollama_provider.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    request = ollama_provider.OllamaAdapter.request_from_legacy_payload(fixture["legacy_payload"])
    assert ollama_provider.OllamaAdapter.serialize_request(request) == fixture["serialized_payload"]

    async def collect():
        runtime = create_runtime("ollama", "http://ollama.test/")
        return [_event_dict(event) async for event in runtime.chat_stream(request)]

    assert asyncio.run(collect()) == fixture["events"]
    assert seen == {
        "method": "POST",
        "url": "http://ollama.test/api/chat",
        "payload": fixture["serialized_payload"],
    }


def test_execution_paths_have_no_direct_ollama_chat_transport():
    root = Path(__file__).resolve().parents[1]
    for relative in ("agent.py", "main.py", "discordbot.py"):
        assert "/api/chat" not in (root / relative).read_text(encoding="utf-8")
    assert "/api/chat" in (root / "llm" / "providers" / "ollama.py").read_text(encoding="utf-8")


def test_common_contract_and_factory_have_no_provider_transport_details():
    root = Path(__file__).resolve().parents[1] / "llm"
    for relative in ("contracts.py", "factory.py"):
        source = (root / relative).read_text(encoding="utf-8").lower()
        assert "http://" not in source and "https://" not in source
        assert "authorization" not in source and "headers" not in source


def test_serializer_preserves_explicit_zero_and_empty_values():
    """Gate 1 serializer가 기존의 명시적 0/빈 값 필드를 truthy 검사로 잃지 않는다."""
    legacy = {
        "model": "m",
        "messages": [],
        "stream": True,
        "keep_alive": "",
        "think": "",
        "tools": [],
        "options": {"temperature": 0.0, "num_predict": 0, "num_ctx": 0, "num_gpu": 0},
    }
    request = ollama_provider.OllamaAdapter.request_from_legacy_payload(legacy)
    assert ollama_provider.OllamaAdapter.serialize_request(request) == legacy

    false_value = {**legacy, "keep_alive": False}
    false_request = ollama_provider.OllamaAdapter.request_from_legacy_payload(false_value)
    assert ollama_provider.OllamaAdapter.serialize_request(false_request)["keep_alive"] is False


def _install_recording_chat_transport(monkeypatch, lines: list[str]):
    recorder = {"client_exit": 0, "response_exit": 0}

    class Response:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            recorder["response_exit"] += 1
            return False

        async def aiter_lines(self):
            for line in lines:
                yield line

        async def aread(self):
            return b""

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            recorder["client_exit"] += 1
            return False

        def stream(self, method, url, json=None):
            assert method == "POST" and url.endswith("/api/chat")
            return Response()

    monkeypatch.setattr(ollama_provider.httpx, "AsyncClient", lambda *args, **kwargs: Client())
    return recorder


def test_agent_repeat_break_closes_provider_http_contexts(monkeypatch):
    block = "반복되는 생성 문자열입니다. " * 1000
    lines = [json.dumps({"message": {"content": block}}, ensure_ascii=False)] * 20
    recorder = _install_recording_chat_transport(monkeypatch, lines)

    async def collect():
        return [event async for event in agent._chat_turn("http://ollama.test", LlmRequest(model="m", messages=[]))]

    events = asyncio.run(collect())
    assert events[-1]["_final"] is True and events[-1]["done_reason"] == "repetition"
    assert recorder == {"client_exit": 1, "response_exit": 1}


def test_agent_cancel_and_stream_error_close_provider_http_contexts(monkeypatch):
    # 소비자가 중간 취소한 경우
    recorder = _install_recording_chat_transport(
        monkeypatch,
        [json.dumps({"message": {"content": "첫 토큰"}}, ensure_ascii=False)],
    )

    async def cancel_after_first_event():
        turn = agent._chat_turn("http://ollama.test", LlmRequest(model="m", messages=[]))
        first = await anext(turn)
        await turn.aclose()
        return first

    assert asyncio.run(cancel_after_first_event()) == {"type": "content", "text": "첫 토큰"}
    assert recorder == {"client_exit": 1, "response_exit": 1}

    # 공급자가 스트리밍 도중 오류를 낸 경우
    recorder = _install_recording_chat_transport(
        monkeypatch,
        [json.dumps({"error": "stream failed"}, ensure_ascii=False)],
    )

    async def collect_error():
        try:
            async for _event in agent._chat_turn("http://ollama.test", LlmRequest(model="m", messages=[])):
                pass
        except LlmProviderError:
            return True
        return False

    assert asyncio.run(collect_error()) is True
    assert recorder == {"client_exit": 1, "response_exit": 1}
