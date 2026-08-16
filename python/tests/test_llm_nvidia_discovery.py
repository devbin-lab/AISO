from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from llm import LlmFailureKind, LlmProviderError, ModelCapabilities
from llm.providers import nvidia as nvidia_provider
from llm.providers.nvidia import NVIDIA_BUILD_BASE_URL, NvidiaAdapter


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def sse(data: object | str) -> bytes:
    value = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    return f"data: {value}\n\n".encode()


async def no_sleep(_seconds: float) -> None:
    return None


def adapter(handler, *, mode: str = "build", endpoint: str = NVIDIA_BUILD_BASE_URL):
    return NvidiaAdapter(
        endpoint,
        deployment_mode=mode,
        api_key="CANARY-DISCOVERY-KEY-73192" if mode == "build" else None,
        transport=httpx.MockTransport(handler),
        sleep=no_sleep,
    )


def test_model_list_is_explicit_strict_deduplicated_and_uses_only_bound_endpoint():
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        assert request.method == "GET"
        assert request.url == f"{NVIDIA_BUILD_BASE_URL}/models"
        assert request.headers["Authorization"] == "Bearer CANARY-DISCOVERY-KEY-73192"
        assert "CANARY" not in str(request.url)
        return httpx.Response(200, json={"data": [
            {"id": "model/a"}, {"id": "model/b"}, {"id": "model/a"}
        ]})

    runtime = adapter(handler)
    assert calls == 0
    assert asyncio.run(runtime.list_models()) == ["model/a", "model/b"]
    assert calls == 1


def test_empty_model_list_is_valid_and_does_not_imply_any_capability():
    runtime = adapter(lambda _request: httpx.Response(200, json={"data": []}))
    assert asyncio.run(runtime.list_models()) == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"models": []}),
        httpx.Response(200, json={"data": [{"name": "missing-id"}]}),
        httpx.Response(200, json={"data": [{"id": ""}]}),
    ],
)
def test_malformed_model_list_is_sanitized(response):
    runtime = adapter(lambda _request: response)
    with pytest.raises(LlmProviderError) as exc:
        asyncio.run(runtime.list_models())
    assert exc.value.kind is LlmFailureKind.MALFORMED
    assert "not-json" not in str(exc.value)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, LlmFailureKind.AUTH),
        (403, LlmFailureKind.AUTH),
        (307, LlmFailureKind.INVALID_REQUEST),
        (404, LlmFailureKind.NOT_FOUND),
    ],
)
def test_model_list_auth_not_found_and_redirect_errors_are_sanitized(status, kind):
    calls = 0
    canary = "CANARY-RAW-MODEL-LIST-BODY-81924"

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            content=canary.encode(),
            headers={"Location": "https://attacker.example/models"},
        )

    with pytest.raises(LlmProviderError) as exc:
        asyncio.run(adapter(handler).list_models())
    assert calls == 1
    assert exc.value.kind is kind
    assert canary not in str(exc.value)


def test_user_nim_model_list_uses_its_exact_canonical_endpoint_without_bearer():
    def handler(request: httpx.Request):
        assert request.url == "https://nim.example.test/v1/models"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"data": [{"id": "local/model"}]})

    runtime = adapter(handler, mode="nim", endpoint="https://NIM.EXAMPLE.TEST:443/v1/")
    assert asyncio.run(runtime.list_models()) == ["local/model"]


def supported_probe_body() -> bytes:
    return b"".join([
        sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "probe_",
            "type": "function",
            "function": {"name": "aiso_capability_", "arguments": "{\"value\":"},
        }]}, "finish_reason": None}]}),
        sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call",
            "function": {"name": "probe", "arguments": "1}"},
        }]}, "finish_reason": "tool_calls"}]}),
        sse("[DONE]"),
    ])


def basic_probe_body() -> bytes:
    return sse({"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}) + sse("[DONE]")


def test_forced_tool_probe_reconstructs_split_delta_but_executes_no_aiso_tool():
    requests = []
    executed = 0
    body = supported_probe_body()

    def handler(request: httpx.Request):
        nonlocal executed
        requests.append(request)
        assert request.extensions["timeout"]["read"] is None
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["temperature"] == 0
        if len(requests) == 1:
            assert payload["max_tokens"] == 32
            assert "tools" not in payload
            assert "tool_choice" not in payload
            return httpx.Response(200, stream=ChunkStream([basic_probe_body()]))
        assert payload["max_tokens"] == 256
        assert payload["tool_choice"] == {
            "type": "function", "function": {"name": "aiso_capability_probe"}
        }
        assert payload["tools"][0]["function"]["name"] == "aiso_capability_probe"
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert executed == 0
        return httpx.Response(200, stream=ChunkStream([body[index:index + 1] for index in range(len(body))]))

    result = asyncio.run(adapter(handler).inspect_capabilities("model/probe"))
    assert result.chat == "supported"
    assert result.stream == "supported"
    assert result.tools == "supported"
    assert executed == 0
    assert len(requests) == 2


@pytest.mark.parametrize("status", [400, 422])
def test_explicit_tool_probe_rejection_is_unsupported(status):
    calls = 0

    def handler(_request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, stream=ChunkStream([basic_probe_body()]))
        return httpx.Response(status)

    result = asyncio.run(adapter(handler).inspect_capabilities("m"))
    assert result.chat == "supported"
    assert result.stream == "supported"
    assert result.tools == "unsupported"
    assert calls == 3


def test_tool_probe_retries_portable_shape_after_thinking_control_is_rejected():
    payloads: list[dict] = []

    def handler(request: httpx.Request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if len(payloads) == 1:
            return httpx.Response(200, stream=ChunkStream([basic_probe_body()]))
        if len(payloads) == 2:
            assert payload["chat_template_kwargs"] == {"enable_thinking": False}
            return httpx.Response(422)
        assert "chat_template_kwargs" not in payload
        return httpx.Response(200, stream=ChunkStream([supported_probe_body()]))

    result = asyncio.run(adapter(handler).inspect_capabilities("model/probe"))
    assert result.chat == "supported"
    assert result.stream == "supported"
    assert result.tools == "supported"
    assert len(payloads) == 3


def test_tool_probe_failure_keeps_independently_confirmed_chat_and_stream():
    calls = 0
    ignored_tool = sse({"choices": [{"delta": {"content": "no function"}, "finish_reason": "stop"}]}) + sse("[DONE]")

    def handler(_request: httpx.Request):
        nonlocal calls
        calls += 1
        body = basic_probe_body() if calls == 1 else ignored_tool
        return httpx.Response(200, stream=ChunkStream([body]))

    result = asyncio.run(adapter(handler).inspect_capabilities("m"))
    assert result.chat == "supported"
    assert result.stream == "supported"
    assert result.tools == "unknown"
    assert calls == 2


def test_capability_probe_uses_one_total_timeout_and_keeps_completed_basic_evidence(monkeypatch):
    runtime = adapter(lambda _request: pytest.fail("the mocked probe helpers own this test"))

    async def basic(_model: str):
        return ModelCapabilities(chat="supported", stream="supported")

    async def wait_for_tool(_model: str, *, disable_thinking: bool):
        assert disable_thinking is True
        await asyncio.Event().wait()
        return ModelCapabilities()  # pragma: no cover

    monkeypatch.setattr(runtime, "_inspect_basic_capabilities", basic)
    monkeypatch.setattr(runtime, "_inspect_tool_capabilities", wait_for_tool)
    monkeypatch.setattr(nvidia_provider, "_CAPABILITY_PROBE_TOTAL_TIMEOUT", 0.01)

    result = asyncio.run(runtime.inspect_capabilities("m"))
    assert result.chat == "supported"
    assert result.stream == "supported"
    assert result.tools == "unknown"


def test_single_tool_call_without_index_is_normalized_but_parallel_missing_indexes_are_not():
    single = b"".join([
        sse({"choices": [{"delta": {"tool_calls": [{
            "id": "probe",
            "type": "function",
            "function": {"name": "aiso_capability_probe", "arguments": "{\"value\":1}"},
        }]}, "finish_reason": "tool_calls"}]}),
        sse("[DONE]"),
    ])
    calls = 0

    def single_handler(_request: httpx.Request):
        nonlocal calls
        calls += 1
        body = basic_probe_body() if calls == 1 else single
        return httpx.Response(200, stream=ChunkStream([body]))

    result = asyncio.run(adapter(single_handler).inspect_capabilities("m"))
    assert result.tools == "supported"

    ambiguous = sse({"choices": [{"delta": {"tool_calls": [
        {"id": "one", "function": {"name": "aiso_capability_probe", "arguments": "{\"value\":1}"}},
        {"id": "two", "function": {"name": "aiso_capability_probe", "arguments": "{\"value\":1}"}},
    ]}, "finish_reason": "tool_calls"}]}) + sse("[DONE]")
    calls = 0

    def ambiguous_handler(_request: httpx.Request):
        nonlocal calls
        calls += 1
        body = basic_probe_body() if calls == 1 else ambiguous
        return httpx.Response(200, stream=ChunkStream([body]))

    result = asyncio.run(adapter(ambiguous_handler).inspect_capabilities("m"))
    assert result.chat == "supported"
    assert result.stream == "supported"
    assert result.tools == "unknown"


@pytest.mark.parametrize(
    "body",
    [
        sse({"choices": [{"delta": {"content": "forced call ignored"}, "finish_reason": "stop"}]}) + sse("[DONE]"),
        b"data: {not-json}\n\n",
        sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "probe",
            "function": {"name": "aiso_capability_probe", "arguments": "{\"value\":2}"},
        }]}, "finish_reason": "tool_calls"}]}) + sse("[DONE]"),
        sse({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "probe",
            "function": {"name": "aiso_capability_probe", "arguments": "{\"value\":1}"},
        }]}, "finish_reason": "tool_calls"}]}),
    ],
)
def test_ambiguous_malformed_or_truncated_probe_is_unknown(body):
    result = asyncio.run(
        adapter(lambda _request: httpx.Response(200, stream=ChunkStream([body]))).inspect_capabilities("m")
    )
    if body.endswith(sse("[DONE]")) and b"forced call ignored" in body:
        assert result.chat == "supported"
        assert result.stream == "supported"
    else:
        assert result.chat == "unknown" or result.chat == "supported"
    assert result.tools == "unknown"


def test_network_probe_failure_is_unknown_without_a_second_quota_request():
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("CANARY-CONNECT-DETAIL", request=request)

    result = asyncio.run(adapter(handler).inspect_capabilities("m"))
    assert calls == 1
    assert result.chat == "unknown"
    assert result.stream == "unknown"
    assert result.tools == "unknown"
    assert "CANARY" not in repr(result)


@pytest.mark.parametrize(
    ("status", "kind"),
    [(401, LlmFailureKind.AUTH), (402, LlmFailureKind.PAYMENT), (404, LlmFailureKind.NOT_FOUND)],
)
def test_probe_auth_payment_and_invalid_model_remain_sanitized_user_errors(status, kind):
    canary = "CANARY-RAW-PROBE-BODY-51762"
    with pytest.raises(LlmProviderError) as exc:
        asyncio.run(adapter(lambda _request: httpx.Response(status, content=canary.encode())).inspect_capabilities("bad"))
    assert exc.value.kind is kind
    assert canary not in str(exc.value)
