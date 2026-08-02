"""Gate 1 실제 실행 경로의 Ollama wire/event 특성화와 아키텍처 경계 검사."""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import main  # noqa: E402
from llm.providers import ollama as ollama_provider  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
except Exception:  # pragma: no cover
    TestClient = None


class _Response:
    status_code = 200

    def __init__(
        self,
        lines: list[str] | None = None,
        info: dict | None = None,
        transport=None,
    ):
        self._lines = lines or []
        self._info = info or {}
        self._transport = transport

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        if self._transport is not None:
            self._transport.chat_response_exit += 1
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""

    def raise_for_status(self):
        return None

    def json(self):
        return {"model_info": self._info}


class _Transport:
    """show + chat만 허용하는 실제 adapter 전용 HTTP 대역."""

    def __init__(self, lines: list[str]):
        self.lines = lines
        self.payloads: list[dict] = []
        self.chat_client_exit = 0
        self.chat_response_exit = 0

    def client(self):
        transport = self

        class Client:
            is_chat_stream = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                if self.is_chat_stream:
                    transport.chat_client_exit += 1
                return False

            async def post(self, url, json=None):
                assert url.endswith("/api/show")
                assert json == {"model": "m"}
                return _Response(info={})

            def stream(self, method, url, json=None):
                assert method == "POST" and url.endswith("/api/chat")
                transport.payloads.append(json)
                self.is_chat_stream = True
                return _Response(lines=transport.lines, transport=transport)

        return Client()


def _install(monkeypatch, lines: list[str]) -> _Transport:
    ollama_provider.OllamaAdapter.reset_cache()
    transport = _Transport(lines)
    monkeypatch.setattr(ollama_provider.httpx, "AsyncClient", lambda *args, **kwargs: transport.client())
    return transport


def _lines() -> list[str]:
    return [
        json.dumps({"message": {"thinking": "reason"}}),
        json.dumps({"message": {"content": "answer"}}),
        json.dumps({"done": True, "done_reason": "stop", "eval_count": 7, "total_duration": 9}),
    ]


def _auth_headers() -> dict[str, str]:
    return {"X-Aiso-Token": main.AUTH_TOKEN} if main.AUTH_TOKEN else {}


@pytest.mark.skipif(TestClient is None, reason="TestClient(httpx) 미설치")
def test_chat_endpoint_wire_payload_and_normalized_event_order(monkeypatch):
    transport = _install(monkeypatch, _lines())
    client = TestClient(main.app)
    response = client.post(
        "/chat",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "model": "m",
            "reasoning_effort": "high",
            "temperature": 0.2,
            "context_length": 4096,
            "keep_alive": "5m",
            "ollama_host": "http://ollama.test",
        },
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    assert [json.loads(line)["type"] for line in response.text.splitlines()] == ["thinking", "content", "done"]
    assert transport.payloads == [{
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "keep_alive": "5m",
        "think": "high",
        "options": {"temperature": 0.2, "num_predict": agent.MAX_GEN_TOKENS, "num_ctx": 4096},
    }]


def test_agent_execution_wire_payload_and_event_order(monkeypatch):
    transport = _install(monkeypatch, _lines())
    monkeypatch.setattr(agent, "list_skills", lambda: [])
    monkeypatch.setattr(agent.discordops, "available", lambda: False)

    async def collect():
        return [event async for event in agent.run_agent(
            host="http://ollama.test",
            workspace="",
            model="m",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.2,
            context_length=4096,
            keep_alive="5m",
            rag_enabled=False,
        )]

    events = asyncio.run(collect())
    assert [event["type"] for event in events] == ["thinking", "content", "usage", "done"]
    payload = transport.payloads
    assert len(payload) == 1
    assert payload[0]["model"] == "m"
    assert payload[0]["stream"] is True and payload[0]["keep_alive"] == "5m"
    assert payload[0]["think"] == "medium"
    assert payload[0]["options"] == {"temperature": 0.2, "num_predict": agent.MAX_GEN_TOKENS, "num_ctx": 4096}
    assert payload[0]["messages"][-1] == {"role": "user", "content": "hello"}
    assert [tool["function"]["name"] for tool in payload[0]["tools"]] == [
        "update_plan", "web_fetch", "web_search", "create_skill", "run_skill",
    ]


def test_research_execution_wire_payload_and_event_order(monkeypatch):
    transport = _install(monkeypatch, _lines())

    async def collect():
        return [event async for event in agent.run_research_chat(
            host="http://ollama.test",
            model="m",
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.2,
            context_length=4096,
            keep_alive="5m",
        )]

    events = asyncio.run(collect())
    assert [event["type"] for event in events] == ["thinking", "content", "usage", "done"]
    assert transport.payloads == [{
        "model": "m",
        "messages": [
            {"role": "system", "content": agent.RESEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": "hello"},
        ],
        "stream": True,
        "keep_alive": "5m",
        "think": "medium",
        "options": {"temperature": 0.2, "num_predict": agent.MAX_GEN_TOKENS, "num_ctx": 4096},
        "tools": [
            agent.REGISTRY["web_search"].schema,
            agent.REGISTRY["web_fetch"].schema,
        ],
    }]


def test_discord_execution_wire_payload_and_normalized_content(monkeypatch):
    transport = _install(monkeypatch, _lines())
    result = asyncio.run(main._discord_step(
        [{"role": "user", "content": "hello"}],
        model="m",
        context_length=4096,
        keep_alive="5m",
        host="http://ollama.test",
    ))
    assert result == {"content": "answer", "tool_calls": []}
    assert transport.payloads == [{
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "keep_alive": "5m",
        "think": "medium",
        "options": {"temperature": 0.7, "num_predict": agent.MAX_GEN_TOKENS, "num_ctx": 4096},
    }]


def _content_then_tool_call_lines() -> list[str]:
    return [
        json.dumps({"message": {"content": "first"}}),
        json.dumps({
            "message": {
                "tool_calls": [{
                    "function": {
                        "name": "web_search",
                        "arguments": json.dumps({"query": "must not run"}),
                    }
                }]
            }
        }),
        json.dumps({"done": True, "done_reason": "stop"}),
    ]


def test_chat_endpoint_close_propagates_to_adapter_http_stream(monkeypatch):
    """사용자 /chat 스트림을 닫으면 adapter HTTP context도 정확히 한 번 닫힌다."""
    transport = _install(monkeypatch, _content_then_tool_call_lines())

    async def close_after_first_chunk():
        response = await main.chat(main.ChatRequest(
            messages=[main.ChatMessage(role="user", content="hello")],
            model="m",
            ollama_host="http://ollama.test",
        ))
        body = response.body_iterator
        first = await anext(body)
        await body.aclose()
        return json.loads(first)

    assert asyncio.run(close_after_first_chunk()) == {"type": "content", "text": "first"}
    assert (transport.chat_client_exit, transport.chat_response_exit) == (1, 1)


def test_public_run_agent_close_stops_future_tool_and_closes_adapter_stream(monkeypatch):
    transport = _install(monkeypatch, _content_then_tool_call_lines())
    executed: list[str] = []
    monkeypatch.setattr(agent, "list_skills", lambda: [])
    monkeypatch.setattr(agent.discordops, "available", lambda: False)

    async def unexpected_execute(*args, **kwargs):
        executed.append(args[0] if args else kwargs.get("name", "unknown"))
        return "unexpected"

    monkeypatch.setattr(agent, "execute", unexpected_execute)

    async def close_after_first_event():
        stream = agent.run_agent(
            host="http://ollama.test",
            workspace="",
            model="m",
            messages=[{"role": "user", "content": "hello"}],
            rag_enabled=False,
        )
        events = [await anext(stream)]
        await stream.aclose()
        return events

    events = asyncio.run(close_after_first_event())
    assert events == [{"type": "content", "text": "first"}]
    assert not executed
    assert all(event["type"] != "done" for event in events)
    assert (transport.chat_client_exit, transport.chat_response_exit) == (1, 1)


def test_public_research_close_stops_future_tool_and_closes_adapter_stream(monkeypatch):
    transport = _install(monkeypatch, _content_then_tool_call_lines())
    executed: list[str] = []

    async def unexpected_execute(*args, **kwargs):
        executed.append(args[0] if args else kwargs.get("name", "unknown"))
        return "unexpected"

    monkeypatch.setattr(agent, "execute", unexpected_execute)

    async def close_after_first_event():
        stream = agent.run_research_chat(
            host="http://ollama.test",
            model="m",
            messages=[{"role": "user", "content": "hello"}],
        )
        events = [await anext(stream)]
        await stream.aclose()
        return events

    events = asyncio.run(close_after_first_event())
    assert events == [{"type": "content", "text": "first"}]
    assert not executed
    assert all(event["type"] != "done" for event in events)
    assert (transport.chat_client_exit, transport.chat_response_exit) == (1, 1)


def test_execution_modules_depend_only_on_public_llm_runtime_boundary():
    """AST 검사로 concrete provider import/reference와 직접 chat transport를 막는다."""
    root = Path(__file__).resolve().parents[1]
    modules = [root / "main.py", root / "agent.py", root / "discordbot.py"]
    concrete_names = {"OllamaAdapter", "OllamaHTTPError"}
    chat_literals: dict[Path, list[str]] = {}
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("llm.providers"), path
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("llm.providers") for alias in node.names), path
            if isinstance(node, ast.Name):
                assert node.id not in concrete_names, path
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "/api/chat" in node.value:
                chat_literals.setdefault(path, []).append(node.value)
    assert chat_literals == {}

    source_files = [
        p for p in root.rglob("*.py")
        if "tests" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts
    ]
    chat_locations = []
    chat_transport_calls = []
    for path in source_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Constant) and isinstance(node.value, str) and "/api/chat" in node.value
            for node in ast.walk(tree)
        ):
            chat_locations.append(path.relative_to(root).as_posix())
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "stream"
            ):
                continue
            if any(
                isinstance(descendant, ast.Constant)
                and isinstance(descendant.value, str)
                and "/api/chat" in descendant.value
                for arg in node.args
                for descendant in ast.walk(arg)
            ):
                chat_transport_calls.append(path.relative_to(root).as_posix())
    assert chat_locations == ["llm/providers/ollama.py"]
    assert chat_transport_calls == ["llm/providers/ollama.py"]
