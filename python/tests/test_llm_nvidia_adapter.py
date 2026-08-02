from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from llm import LlmFailureKind, LlmProviderError, LlmRequest
from llm.providers.nvidia import (
    NVIDIA_BUILD_BASE_URL,
    NvidiaAdapter,
    NvidiaSseParser,
    canonicalize_nvidia_endpoint,
)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def sse(data: object | str, *, crlf: bool = False) -> bytes:
    value = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    newline = "\r\n" if crlf else "\n"
    return f"data: {value}{newline}{newline}".encode()


def response_for(body: bytes, *, chunks: list[bytes] | None = None, status: int = 200, headers=None):
    stream = ChunkStream(chunks if chunks is not None else [body])
    return httpx.Response(status, headers=headers, stream=stream), stream


async def collect(adapter: NvidiaAdapter):
    request = LlmRequest(model="nvidia/test-model", messages=[{"role": "user", "content": "안녕"}])
    return [event async for event in adapter.chat_stream(request)]


def build_adapter(handler, *, sleep=None):
    return NvidiaAdapter(
        NVIDIA_BUILD_BASE_URL,
        deployment_mode="build",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        sleep=sleep or asyncio.sleep,
    )


def test_incremental_parser_handles_crlf_comments_blank_and_multiple_data_lines():
    parser = NvidiaSseParser()
    raw = (
        b": keepalive\r\n\r\n"
        b"event: message\r\n"
        b"data: {\"choices\":\r\n"
        b"data: []}\r\n\r\n"
        b"\r\n"
        b"data: [DONE]\n\n"
    )
    frames: list[str] = []
    for byte in raw:
        frames.extend(parser.feed(bytes([byte])))
    frames.extend(parser.finish())
    assert frames == ['{"choices":\n[]}', "[DONE]"]


def test_arbitrary_byte_and_utf8_splits_normalize_all_gate3_events():
    frames = [
        b": ping\r\n\r\n",
        sse({"choices": [{"delta": {"reasoning_content": "생각🙂"}, "finish_reason": None}]}, crlf=True),
        sse({"choices": [{"delta": {"content": "답변한글"}, "finish_reason": None}]}),
        sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_", "function": {"name": "we", "arguments": "{\"q\":"}}]}, "finish_reason": None}]}),
        sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "b", "arguments": "\"x\"}"}}]}, "finish_reason": "tool_calls"}]}),
        sse({"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}}),
        sse("[DONE]"),
    ]
    body = b"".join(frames)
    chunks = [body[index:index + 1] for index in range(len(body))]

    async def run():
        adapter = build_adapter(lambda _request: response_for(body, chunks=chunks)[0])
        return await collect(adapter)

    events = asyncio.run(run())
    assert [event.kind for event in events] == [
        "thinking", "content", "tool_call_delta", "tool_call_delta", "usage", "done"
    ]
    assert events[0].text == "생각🙂"
    assert events[1].text == "답변한글"
    assert events[2].tool_calls[0]["function"]["arguments"] == '{"q":'
    assert events[3].tool_calls[0]["function"]["arguments"] == '"x"}'
    assert events[4].total_tokens == 8
    assert events[5].output_tokens == 5
    assert events[5].done_reason == "tool_calls"


@pytest.mark.parametrize(
    "body",
    [
        sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}),
        b"data: {not-json}\n\n",
        b"data: \xff\n\n",
        b"data: [DONE]\n",
    ],
)
def test_malformed_or_truncated_stream_never_emits_fake_done(body):
    async def run():
        adapter = build_adapter(lambda _request: response_for(body)[0])
        return await collect(adapter)

    events = asyncio.run(run())
    assert events[-1].kind == "incomplete"
    assert all(event.kind != "done" for event in events)


def test_retry_once_for_status_before_first_byte_and_honor_bounded_retry_after():
    calls = 0
    slept: list[float] = []
    complete = sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}) + sse("[DONE]")

    def handler(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "99"}, content=b"CANARY-UPSTREAM-BODY")
        return response_for(complete)[0]

    async def fake_sleep(seconds: float):
        slept.append(seconds)

    events = asyncio.run(collect(build_adapter(handler, sleep=fake_sleep)))
    assert calls == 2
    assert slept == [2.0]
    assert events[-1].kind == "done"
    assert "CANARY" not in " ".join((event.error or event.text) for event in events)


def test_connect_failure_retries_once_before_any_response_byte():
    calls = 0
    complete = sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}) + sse("[DONE]")

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("CANARY-CONNECT", request=request)
        return response_for(complete)[0]

    async def no_sleep(_seconds: float):
        return None

    events = asyncio.run(collect(build_adapter(handler, sleep=no_sleep)))
    assert calls == 2
    assert events[-1].kind == "done"


def test_no_retry_after_any_response_byte_even_without_content_delta():
    calls = 0
    body = b": first-byte\n\n" + sse({"choices": []})

    def handler(_request):
        nonlocal calls
        calls += 1
        return response_for(body)[0]

    events = asyncio.run(collect(build_adapter(handler)))
    assert calls == 1
    assert events[-1].kind == "incomplete"
    assert all(event.kind != "done" for event in events)


@pytest.mark.parametrize(
    ("status", "kind", "expected_calls"),
    [
        (401, LlmFailureKind.AUTH, 1),
        (403, LlmFailureKind.AUTH, 1),
        (402, LlmFailureKind.PAYMENT, 1),
        (404, LlmFailureKind.NOT_FOUND, 1),
        (422, LlmFailureKind.INVALID_REQUEST, 1),
        (307, LlmFailureKind.INVALID_REQUEST, 1),
        (500, LlmFailureKind.UPSTREAM, 2),
    ],
)
def test_http_errors_are_sanitized_and_redirects_not_followed(status, kind, expected_calls):
    calls = 0
    canary = "CANARY-RAW-AUTHORIZATION-UPSTREAM"

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, content=canary.encode(), headers={"Location": "https://evil.example"})

    async def no_sleep(_seconds: float):
        return None

    with pytest.raises(LlmProviderError) as exc:
        asyncio.run(collect(build_adapter(handler, sleep=no_sleep)))
    assert exc.value.kind is kind
    assert canary not in exc.value.body
    assert canary not in str(exc.value)
    assert calls == expected_calls


def test_api_key_is_only_in_authorization_header_not_url_or_payload():
    canary = "CANARY-NVIDIA-KEY-72619"
    complete = sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}) + sse("[DONE]")

    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == f"Bearer {canary}"
        assert canary not in str(request.url)
        assert canary not in request.content.decode()
        assert request.url == f"{NVIDIA_BUILD_BASE_URL}/chat/completions"
        return response_for(complete)[0]

    adapter = NvidiaAdapter(
        NVIDIA_BUILD_BASE_URL,
        deployment_mode="build",
        api_key=canary,
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(collect(adapter))[-1].kind == "done"


def test_user_nim_may_omit_bearer_key_but_stays_on_canonical_endpoint():
    complete = sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}) + sse("[DONE]")

    def handler(request: httpx.Request):
        assert "Authorization" not in request.headers
        assert request.url == "https://nim.example.test/v1/chat/completions"
        return response_for(complete)[0]

    adapter = NvidiaAdapter(
        "https://NIM.EXAMPLE.TEST:443/v1/",
        deployment_mode="nim",
        api_key=None,
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(collect(adapter))[-1].kind == "done"


@pytest.mark.parametrize(
    "mode,endpoint",
    [
        ("build", "https://evil.example/v1"),
        ("nim", "http://nim.example/v1"),
        ("nim", "https://u:p@nim.example/v1"),
        ("nim", "https://nim.example/v1?q=secret"),
        ("nim", "https://nim.example/v1#frag"),
        ("nim", "https://nim.example:99999/v1"),
    ],
)
def test_endpoint_policy_rejects_redirectable_or_unsafe_targets(mode, endpoint):
    with pytest.raises(ValueError):
        canonicalize_nvidia_endpoint(mode, endpoint)


def test_cancellation_closes_http_stream_context_immediately():
    first = sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]})
    stream = ChunkStream([first, sse("[DONE]")])

    async def run():
        adapter = build_adapter(lambda _request: httpx.Response(200, stream=stream))
        generator = adapter.chat_stream(LlmRequest(model="m", messages=[]))
        event = await generator.__anext__()
        assert event.kind == "content"
        await generator.aclose()

    asyncio.run(run())
    assert stream.closed is True
