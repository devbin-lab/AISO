from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import AsyncIterator

import pytest

import agent
import discordbot
import main
from agent_ledger import AgentExecutionLedger
from llm import LlmEvent, LlmFailureKind, LlmModelRuntime, LlmProviderError, LlmRequest

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

pytestmark = pytest.mark.skipif(TestClient is None, reason="TestClient(httpx) is unavailable")

CHANNEL_TOKEN = "gate6-main-channel-token"
ORDINARY_TOKEN = "gate6-renderer-token"
NIM_ENDPOINT = "https://nim.example.com/v1"
_nonce_counter = itertools.count(1)


def _headers(*, channel: bool = False) -> dict[str, str]:
    if channel:
        return {
            "X-Aiso-Credential-Token": CHANNEL_TOKEN,
            "X-Aiso-Credential-Nonce": f"gate6-nonce-{next(_nonce_counter):032d}",
        }
    return {"X-Aiso-Token": ORDINARY_TOKEN}


@pytest.fixture
def client(monkeypatch):
    auth_middleware = next(
        middleware for middleware in main.app.user_middleware
        if middleware.cls is main.TokenAuthMiddleware
    )
    monkeypatch.setattr(main, "CREDENTIAL_CHANNEL_TOKEN", CHANNEL_TOKEN)
    monkeypatch.setattr(main, "AUTH_TOKEN", ORDINARY_TOKEN)
    monkeypatch.setitem(auth_middleware.kwargs, "token", ORDINARY_TOKEN)
    main._credential_memory.clear_secret()
    main._credential_memory._used_nonces.clear()
    main._credential_memory._nonce_order.clear()
    main._nvidia_agent_grants.clear()
    main._nvidia_research_grants.clear()
    main._nvidia_discord_grants.clear()
    main.app.middleware_stack = None
    try:
        yield TestClient(main.app)
    finally:
        main._nvidia_agent_grants.clear()
        main._nvidia_research_grants.clear()
        main._nvidia_discord_grants.clear()
        main._credential_memory.clear_secret()
        main.app.middleware_stack = None


def _bind_nim(client) -> None:
    response = client.post(
        "/internal/credentials/bind",
        headers=_headers(channel=True),
        json={"deploymentMode": "nim", "endpoint": NIM_ENDPOINT},
    )
    assert response.status_code == 200


def _issue_target_grant(client, kind: str, *, ttl: float = 60) -> str:
    response = client.post(
        f"/internal/nvidia-{kind}/grant",
        headers=_headers(channel=True),
        json={
            "deploymentMode": "nim",
            "endpoint": NIM_ENDPOINT,
            "model": "model/a",
            "ttlSeconds": ttl,
        },
    )
    assert response.status_code == 200
    return response.json()["grantId"]


def test_research_and_discord_grants_are_main_only_one_use_exact_ttl_and_bounded(
    client, monkeypatch
):
    _bind_nim(client)
    for kind in ("research", "discord"):
        denied = client.post(
            f"/internal/nvidia-{kind}/grant", headers=_headers(), json={}
        )
        assert denied.status_code == 401

    now = 100.0
    monkeypatch.setattr(main.time, "monotonic", lambda: now)
    research = main._NvidiaResearchGrantStore()
    binding = {"deploymentMode": "nim", "endpoint": NIM_ENDPOINT}
    first = research.issue(binding, "model/a", 60)
    for _ in range(main.NVIDIA_GRANT_STORE_MAX_RECORDS):
        latest = research.issue(binding, "model/a", 60)
    assert len(research._grants) == main.NVIDIA_GRANT_STORE_MAX_RECORDS
    with pytest.raises(main.HTTPException, match="invalid or expired"):
        research.consume(first, binding, "model/a")
    research.consume(latest, binding, "model/a")
    with pytest.raises(main.HTTPException, match="invalid or expired"):
        research.consume(latest, binding, "model/a")

    discord = main._NvidiaDiscordGrantStore()
    token = discord.issue(binding, "model/a", 0.25)
    now = 100.251
    with pytest.raises(main.HTTPException, match="invalid or expired"):
        discord.consume(token, binding, "model/a")


def test_discord_nvidia_config_requires_exact_grant_and_never_retains_it(
    client, monkeypatch
):
    _bind_nim(client)
    grant = _issue_target_grant(client, "discord")
    applied: list[dict] = []

    async def fake_apply(config, *_callbacks):
        applied.append(config)

    monkeypatch.setattr(main.discordbot, "apply_config", fake_apply)
    monkeypatch.setattr(main.discordbot, "status", lambda: {"running": True})
    body = {
        "enabled": True,
        "token": "discord-token-canary",
        "provider": "nvidia",
        "deployment_mode": "nim",
        "endpoint": NIM_ENDPOINT,
        "model": "model/a",
        "ollama_host": "http://127.0.0.1:11434",
        "nvidia_runtime_grant": grant,
    }
    first = client.post("/discord/config", headers=_headers(), json=body)
    assert first.status_code == 200
    assert len(applied) == 1
    assert "nvidia_runtime_grant" not in applied[0]
    replay = client.post("/discord/config", headers=_headers(), json=body)
    assert replay.status_code == 409
    assert len(applied) == 1

    mismatched = _issue_target_grant(client, "discord")
    mismatch_body = {**body, "model": "model/b", "nvidia_runtime_grant": mismatched}
    mismatch = client.post("/discord/config", headers=_headers(), json=mismatch_body)
    assert mismatch.status_code == 409
    assert len(applied) == 1


def test_nvidia_research_chat_consumes_grant_before_egress_and_never_falls_back(
    client, monkeypatch
):
    _bind_nim(client)
    grant = _issue_target_grant(client, "research")
    runtime = object()
    runtime_calls: list[tuple] = []
    research_calls: list[dict] = []

    def fake_create_runtime(name, endpoint, **kwargs):
        runtime_calls.append((name, endpoint, kwargs))
        assert name == "nvidia"
        return runtime

    async def fake_research(**kwargs):
        research_calls.append(kwargs)
        yield {"type": "done"}

    monkeypatch.setattr(main, "create_runtime", fake_create_runtime)
    monkeypatch.setattr(main, "run_research_chat", fake_research)
    body = {
        "provider": "nvidia",
        "deployment_mode": "nim",
        "endpoint": NIM_ENDPOINT,
        "model": "model/a",
        "messages": [{"role": "user", "content": "research"}],
        "research": True,
        "nvidia_research_grant": grant,
        "ollama_host": "http://127.0.0.1:11434",
    }
    response = client.post("/chat", headers=_headers(), json=body)
    assert response.status_code == 200 and '"type": "done"' in response.text
    assert [call[0] for call in runtime_calls] == ["nvidia"]
    assert research_calls[0]["runtime"] is runtime
    assert research_calls[0]["strict_tool_protocol"] is True

    replay = client.post("/chat", headers=_headers(), json=body)
    assert replay.status_code == 200 and '"type": "error"' in replay.text
    assert len(runtime_calls) == 1 and len(research_calls) == 1

    failing_grant = _issue_target_grant(client, "research")

    async def failed_research(**_kwargs):
        raise LlmProviderError(
            503, "NVIDIA-UPSTREAM-CANARY", provider_name="NVIDIA",
            kind=LlmFailureKind.UNKNOWN,
        )
        yield  # pragma: no cover

    monkeypatch.setattr(main, "run_research_chat", failed_research)
    failed = client.post(
        "/chat", headers=_headers(), json={**body, "nvidia_research_grant": failing_grant}
    )
    assert '"type": "error"' in failed.text
    assert [call[0] for call in runtime_calls] == ["nvidia", "nvidia"]
    assert "Ollama" not in failed.text


class _StrictRuntime:
    def __init__(self, turns: list[list[LlmEvent]]):
        self.turns = list(turns)
        self.requests: list[LlmRequest] = []

    async def prepare_model(self, model: str) -> LlmModelRuntime:
        return LlmModelRuntime(model=model)

    def prepare_attempts(self, request, _effort, _runtime):
        return [request]

    async def chat_stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        self.requests.append(request)
        for event in self.turns.pop(0):
            yield event


def _strict_tool_turn(call_id: str, arguments: str) -> list[LlmEvent]:
    return [
        LlmEvent(kind="tool_call_delta", tool_calls=[{
            "index": 0,
            "id": call_id,
            "function": {"name": "web_fetch", "arguments": arguments},
        }]),
        LlmEvent(kind="done", done_reason="tool_calls"),
    ]


def _final_turn() -> list[LlmEvent]:
    return [LlmEvent(kind="content", text="done"), LlmEvent(kind="done", done_reason="stop")]


async def _collect(stream) -> list[dict]:
    return [event async for event in stream]


def test_nvidia_research_duplicate_provider_id_reuses_once_and_conflict_executes_zero(
    monkeypatch
):
    executions = 0

    async def fake_execute(*_args):
        nonlocal executions
        executions += 1
        return "result", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    same = _StrictRuntime([
        _strict_tool_turn("research-call-1", '{"url":"https://example.com/a"}'),
        _strict_tool_turn("research-call-1", '{"url":"https://example.com/a"}'),
        _final_turn(),
    ])
    events = asyncio.run(_collect(agent.run_research_chat(
        host="http://127.0.0.1:11434",
        model="model/a",
        messages=[{"role": "user", "content": "research"}],
        runtime=same,
        strict_tool_protocol=True,
    )))
    assert executions == 1
    assert sum(event.get("type") == "tool_result" for event in events) == 2
    assert same.requests[1].messages[-1]["tool_call_id"] == "research-call-1"

    executions = 0
    conflict = _StrictRuntime([
        _strict_tool_turn("research-call-2", '{"url":"https://example.com/a"}'),
        _strict_tool_turn("research-call-2", '{"url":"https://example.com/b"}'),
    ])
    conflict_events = asyncio.run(_collect(agent.run_research_chat(
        host="http://127.0.0.1:11434",
        model="model/a",
        messages=[{"role": "user", "content": "research"}],
        runtime=conflict,
        strict_tool_protocol=True,
    )))
    assert executions == 1
    assert any(event.get("type") == "error" for event in conflict_events)


def test_discord_duplicate_provider_id_reuses_result_and_conflict_is_zero_repeat(monkeypatch):
    monkeypatch.setattr(discordbot._S, "gen_lock", asyncio.Lock())
    executions = 0
    turns = iter([
        {"content": "", "tool_calls": [{
            "provider_tool_call_id": "discord-call-1",
            "canonical_arguments": "{}",
            "function": {"name": "discord_server_map", "arguments": {}},
        }]},
        {"content": "", "tool_calls": [{
            "provider_tool_call_id": "discord-call-1",
            "canonical_arguments": "{}",
            "function": {"name": "discord_server_map", "arguments": {}},
        }]},
        {"content": "done", "tool_calls": []},
    ])

    async def fake_step(_convo, _tools):
        return next(turns)

    async def fake_tool(*_args):
        nonlocal executions
        executions += 1
        return "map-result"

    monkeypatch.setattr(discordbot._S, "step", fake_step)
    monkeypatch.setattr(discordbot, "_run_bot_tool", fake_tool)
    assert asyncio.run(discordbot._tool_chat(None, "1", [{"role": "user", "content": "map"}])) == "done"
    assert executions == 1

    executions = 0
    conflict_turns = iter([
        {"content": "", "tool_calls": [{
            "provider_tool_call_id": "discord-call-2",
            "canonical_arguments": "{}",
            "function": {"name": "discord_server_map", "arguments": {}},
        }]},
        {"content": "", "tool_calls": [{
            "provider_tool_call_id": "discord-call-2",
            "canonical_arguments": '{"changed":true}',
            "function": {"name": "discord_server_map", "arguments": {"changed": True}},
        }]},
    ])

    async def conflict_step(_convo, _tools):
        return next(conflict_turns)

    monkeypatch.setattr(discordbot._S, "step", conflict_step)
    reply = asyncio.run(discordbot._tool_chat(None, "1", [{"role": "user", "content": "map"}]))
    assert executions == 1  # first call ran; conflicting reuse adds zero executions
    assert "ID" in reply


def test_discord_nvidia_tool_parse_error_is_not_retried_or_fallen_back(monkeypatch):
    main._credential_memory.bind({"deploymentMode": "nim", "endpoint": NIM_ENDPOINT})
    calls = 0

    class Runtime:
        async def prepare_model(self, model):
            return LlmModelRuntime(model=model)

        def prepare_attempts(self, request, *_args):
            return [request]

    def fake_create_runtime(name, *_args, **_kwargs):
        assert name == "nvidia"
        return Runtime()

    async def failed_turn(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise LlmProviderError(
            500, "parse canary", provider_name="NVIDIA", kind=LlmFailureKind.TOOL_PARSE
        )
        yield  # pragma: no cover

    monkeypatch.setattr(main, "create_runtime", fake_create_runtime)
    monkeypatch.setattr(main, "_chat_turn", failed_turn)
    result = asyncio.run(main._discord_step(
        [{"role": "user", "content": "map"}],
        model="model/a",
        context_length=4096,
        keep_alive="5m",
        host="http://127.0.0.1:11434",
        tools=[{"type": "function", "function": {"name": "discord_server_map"}}],
        provider="nvidia",
        deployment_mode="nim",
        endpoint=NIM_ENDPOINT,
    ))
    assert calls == 1
    assert result["tool_calls"] == []
    main._credential_memory.clear_secret()


def test_discord_nvidia_final_content_does_not_duplicate_streamed_fragments(monkeypatch):
    main._credential_memory.bind({"deploymentMode": "nim", "endpoint": NIM_ENDPOINT})

    class Runtime:
        async def prepare_model(self, model):
            return LlmModelRuntime(model=model)

        def prepare_attempts(self, request, *_args):
            return [request]

    async def completed_turn(*_args, **_kwargs):
        yield {"type": "content", "text": "hello"}
        yield {"_final": True, "content": "hello", "tool_calls": []}

    monkeypatch.setattr(main, "create_runtime", lambda name, *_args, **_kwargs: Runtime())
    monkeypatch.setattr(main, "_chat_turn", completed_turn)
    result = asyncio.run(main._discord_step(
        [{"role": "user", "content": "hello"}],
        model="model/a",
        context_length=4096,
        keep_alive="5m",
        host="http://127.0.0.1:11434",
        provider="nvidia",
        deployment_mode="nim",
        endpoint=NIM_ENDPOINT,
    ))
    assert result == {"content": "hello", "tool_calls": []}
    main._credential_memory.clear_secret()


def test_nvidia_discord_research_terminal_event_is_an_explicit_failure(monkeypatch):
    main._credential_memory.bind({"deploymentMode": "nim", "endpoint": NIM_ENDPOINT})
    monkeypatch.setattr(main, "create_runtime", lambda name, *_args, **_kwargs: object())

    async def cancelled_research(**kwargs):
        assert kwargs["strict_tool_protocol"] is True
        yield {"type": "content", "text": "partial must not become success"}
        yield {"type": "cancelled", "error": "cancelled"}

    monkeypatch.setattr(main, "run_research_chat", cancelled_research)
    result = asyncio.run(main._discord_research(
        [{"role": "user", "content": "brief"}],
        model="model/a",
        context_length=4096,
        keep_alive="5m",
        host="http://127.0.0.1:11434",
        provider="nvidia",
        deployment_mode="nim",
        endpoint=NIM_ENDPOINT,
    ))
    assert "partial must not become success" not in result
    assert "실패" in result
    main._credential_memory.clear_secret()


def test_gate6_rag_rejects_nonlocal_ollama_before_any_request():
    with pytest.raises(main.HTTPException, match="local Ollama"):
        main._require_local_ollama_host("https://ollama.example.com")
    with pytest.raises(main.HTTPException, match="local Ollama"):
        main._validate_nvidia_agent_execution_scope({
            "fingerprint": "f" * 64,
            "approvalMode": "read",
            "workspace": "C:/workspace",
            "ragEnabled": True,
            "ollamaHost": "https://ollama.example.com",
            "ragTopK": 5,
            "allowedTools": ["update_plan", "get_system_time", "search_docs"],
            "comfy": {
                "enabled": False,
                "baseUrl": "",
                "profiles": [],
                "selectionMode": "auto",
                "selectedProfileId": None,
            },
        })


def test_nvidia_comfy_failure_canary_never_reaches_provider_conversation_or_ledger(
    tmp_path, monkeypatch
):
    canary = "C:/PRIVATE/WORKFLOW-CANARY-98127.json"
    runtime = _StrictRuntime([
        [
            LlmEvent(kind="tool_call_delta", tool_calls=[{
                "index": 0,
                "id": "image-call-1",
                "function": {"name": "generate_image", "arguments": '{"prompt":"draw a cat"}'},
            }]),
            LlmEvent(kind="done", done_reason="tool_calls"),
        ],
        _final_turn(),
    ])

    async def fail_image(**_kwargs):
        raise agent.GenerationError(f"invalid workflow at {canary}", kind="input")

    monkeypatch.setattr(agent, "generate_image", fail_image)
    ledger_path = tmp_path / "ledger.sqlite3"
    with AgentExecutionLedger(ledger_path) as ledger:
        events = asyncio.run(_collect(agent.run_agent(
            host="http://127.0.0.1:11434",
            workspace="",
            model="model/a",
            messages=[{"role": "user", "content": "generate an image of a cat"}],
            session_id="session-gate6-comfy",
            provider="nvidia",
            runtime=runtime,
            approval_mode="auto",
            assistant_turn_id="assistant-gate6-comfy",
            execution_ledger=ledger,
            comfy_base_url="http://127.0.0.1:8188",
            comfy_profiles=[{
                "id": "private-profile",
                "name": "PRIVATE-MODEL-CANARY",
                "agentEnabled": True,
                "workflowTemplate": {"graph": {"path": canary}},
            }],
            nvidia_allowed_tools=["update_plan", "get_system_time", "generate_image"],
        )))

    local_result = next(event["output"] for event in events if event.get("type") == "tool_result")
    assert canary in local_result
    provider_payload = json.dumps(
        [{"messages": request.messages, "tools": request.tools} for request in runtime.requests],
        ensure_ascii=False,
        default=str,
    )
    assert canary not in provider_payload
    assert "PRIVATE-MODEL-CANARY" not in provider_payload
    image_schema = next(
        tool for tool in runtime.requests[0].tools or []
        if tool["function"]["name"] == "generate_image"
    )
    assert "model_hint" not in image_schema["function"]["parameters"]["properties"]
    assert canary.encode() not in ledger_path.read_bytes()
