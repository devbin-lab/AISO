from __future__ import annotations

import itertools

import pytest

import main
from llm import LlmEvent

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

pytestmark = pytest.mark.skipif(TestClient is None, reason="TestClient(httpx) is unavailable")

CHANNEL_TOKEN = "credential-channel-test-token"
ORDINARY_TOKEN = "ordinary-renderer-token"
_nonce_counter = itertools.count(1)


def headers(*, ordinary: bool = False, channel: bool = True, nonce: str | None = None):
    result = {"X-Aiso-Credential-Nonce": nonce or f"nonce-{next(_nonce_counter):032d}"}
    if ordinary:
        result["X-Aiso-Token"] = ORDINARY_TOKEN
    if channel:
        result["X-Aiso-Credential-Token"] = CHANNEL_TOKEN
    return result


@pytest.fixture
def client(monkeypatch):
    auth_middleware = next(
        middleware
        for middleware in main.app.user_middleware
        if middleware.cls is main.TokenAuthMiddleware
    )
    monkeypatch.setattr(main, "CREDENTIAL_CHANNEL_TOKEN", CHANNEL_TOKEN)
    monkeypatch.setattr(main, "AUTH_TOKEN", ORDINARY_TOKEN)
    monkeypatch.setitem(auth_middleware.kwargs, "token", ORDINARY_TOKEN)
    main._credential_memory.clear_secret()
    main._credential_memory._used_nonces.clear()
    main._credential_memory._nonce_order.clear()
    main.app.middleware_stack = None
    try:
        yield TestClient(main.app)
    finally:
        main._credential_memory.clear_secret()
        main.app.middleware_stack = None


def test_renderer_token_cannot_access_credential_status(client):
    response = client.post(
        "/internal/credentials/status",
        headers=headers(ordinary=True, channel=False),
        json={},
    )
    assert response.status_code == 401


def test_missing_or_wrong_channel_token_is_rejected(client):
    missing = client.post("/internal/credentials/status", headers=headers(channel=False), json={})
    wrong_headers = headers()
    wrong_headers["X-Aiso-Credential-Token"] = "wrong"
    wrong = client.post("/internal/credentials/status", headers=wrong_headers, json={})
    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_set_status_clear_is_write_only_and_zeroizes_memory(client):
    canary = "CANARY-SIDECAR-NVIDIA-KEY-91573"
    binding = {
        "deploymentMode": "build",
        "endpoint": main.NVIDIA_BUILD_BASE_URL,
        "apiKey": canary,
    }
    response = client.post("/internal/credentials/set", headers=headers(), json=binding)
    assert response.status_code == 200
    assert canary not in response.text

    status = client.post("/internal/credentials/status", headers=headers(), json={})
    assert status.status_code == 200
    assert status.json() == {
        "hasCredential": True,
        "binding": {
            "deploymentMode": "build",
            "endpoint": main.NVIDIA_BUILD_BASE_URL,
        },
    }
    assert canary not in status.text

    secret_reference = main._credential_memory._secret
    assert isinstance(secret_reference, bytearray)
    cleared = client.post("/internal/credentials/clear", headers=headers(), json={})
    assert cleared.status_code == 200
    assert main._credential_memory._secret is None
    assert secret_reference and all(byte == 0 for byte in secret_reference)


def test_nonce_replay_is_rejected(client):
    nonce = "replay-nonce-00000000000000000000000000000000"
    first = client.post("/internal/credentials/status", headers=headers(nonce=nonce), json={})
    replay = client.post("/internal/credentials/status", headers=headers(nonce=nonce), json={})
    assert first.status_code == 200
    assert replay.status_code == 409


def test_build_binding_cannot_be_redirected(client):
    canary = "CANARY-INVALID-BINDING-29384"
    response = client.post(
        "/internal/credentials/set",
        headers=headers(),
        json={"deploymentMode": "build", "endpoint": "https://evil.example/v1", "apiKey": canary},
    )
    assert response.status_code == 400
    assert canary not in response.text


def test_no_credential_retrieval_endpoint_exists(client):
    response = client.get("/internal/credentials/get", headers=headers())
    assert response.status_code == 404


def test_lookalike_127_hostname_is_not_treated_as_loopback(client):
    response = client.post(
        "/internal/credentials/set",
        headers=headers(),
        json={"deploymentMode": "nim", "endpoint": "http://127.evil.example/v1", "apiKey": "key"},
    )
    assert response.status_code == 400


def test_keyless_nim_bind_is_exact_and_build_bind_is_rejected(client):
    endpoint = "https://nim.example.com/v1"
    nim = client.post(
        "/internal/credentials/bind",
        headers=headers(),
        json={"deploymentMode": "nim", "endpoint": endpoint + "/"},
    )
    assert nim.status_code == 200
    assert main._credential_memory.status() == {
        "hasCredential": False,
        "binding": {"deploymentMode": "nim", "endpoint": endpoint},
    }
    build = client.post(
        "/internal/credentials/bind",
        headers=headers(),
        json={"deploymentMode": "build", "endpoint": main.NVIDIA_BUILD_BASE_URL},
    )
    assert build.status_code == 400


def test_nvidia_chat_uses_only_exact_sidecar_binding_and_never_ollama(client, monkeypatch):
    canary = "CANARY-SIDECAR-TRANSFER-68312"
    saved = client.post(
        "/internal/credentials/set",
        headers=headers(),
        json={
            "deploymentMode": "build",
            "endpoint": main.NVIDIA_BUILD_BASE_URL,
            "apiKey": canary,
        },
    )
    assert saved.status_code == 200
    calls = []

    class FakeNvidiaRuntime:
        async def chat_stream(self, _request):
            yield LlmEvent(kind="content", text="nvidia-only")
            yield LlmEvent(kind="done", done_reason="stop", output_tokens=2)

    def fake_create_runtime(name, endpoint, **kwargs):
        calls.append((name, endpoint, kwargs))
        assert name == "nvidia"
        return FakeNvidiaRuntime()

    monkeypatch.setattr(main, "create_runtime", fake_create_runtime)
    response = client.post(
        "/chat",
        headers=headers(ordinary=True, channel=False),
        json={
            "provider": "nvidia",
            "deployment_mode": "build",
            "endpoint": main.NVIDIA_BUILD_BASE_URL,
            "model": "meta/llama-test",
            "messages": [{"role": "user", "content": "hello"}],
            "research": False,
        },
    )
    assert response.status_code == 200
    assert "nvidia-only" in response.text
    assert '"type": "done"' in response.text
    assert canary not in response.text
    assert calls == [
        (
            "nvidia",
            main.NVIDIA_BUILD_BASE_URL,
            {"credential": canary, "deployment_mode": "build"},
        )
    ]


def test_nvidia_binding_mismatch_and_research_block_before_runtime_egress(client, monkeypatch):
    saved = client.post(
        "/internal/credentials/set",
        headers=headers(),
        json={
            "deploymentMode": "build",
            "endpoint": main.NVIDIA_BUILD_BASE_URL,
            "apiKey": "CANARY-NOT-FOR-NIM-88214",
        },
    )
    assert saved.status_code == 200
    calls = []
    monkeypatch.setattr(main, "create_runtime", lambda *args, **kwargs: calls.append((args, kwargs)))
    ordinary = headers(ordinary=True, channel=False)
    base = {
        "provider": "nvidia",
        "model": "meta/llama-test",
        "messages": [{"role": "user", "content": "hello"}],
    }
    mismatch = client.post(
        "/chat",
        headers=ordinary,
        json={
            **base,
            "deployment_mode": "nim",
            "endpoint": "https://nim.example.com/v1",
            "research": False,
        },
    )
    research = client.post(
        "/chat",
        headers=ordinary,
        json={
            **base,
            "deployment_mode": "build",
            "endpoint": main.NVIDIA_BUILD_BASE_URL,
            "research": True,
        },
    )
    assert '"type": "error"' in mismatch.text
    assert '"type": "error"' in research.text
    assert calls == []


def test_nvidia_agent_is_blocked_before_workspace_or_tool_execution(client, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "validate_workspace", lambda *_args: calls.append("workspace"))
    monkeypatch.setattr(main, "run_agent", lambda **_kwargs: calls.append("agent"))
    response = client.post(
        "/agent",
        headers=headers(ordinary=True, channel=False),
        json={
            "provider": "nvidia",
            "workspace": "C:/untrusted",
            "messages": [{"role": "user", "content": "run a tool"}],
        },
    )
    assert response.status_code == 200
    assert '"type": "error"' in response.text
    assert calls == []
