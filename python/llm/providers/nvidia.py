"""OpenAI-compatible NVIDIA Build / user NIM streaming adapter."""

from __future__ import annotations

import asyncio
import codecs
import email.utils
import ipaddress
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from llm.contracts import (
    LlmEvent,
    LlmFailureKind,
    LlmModelRuntime,
    LlmProviderError,
    LlmRequest,
    ModelCapabilities,
)


NVIDIA_BUILD_BASE_URL = "https://integrate.api.nvidia.com/v1"
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
_MAX_RETRY_AFTER_SECONDS = 2.0
_CAPABILITY_PROBE_NAME = "aiso_capability_probe"
_CAPABILITY_PROBE_ARGUMENT = 1


def canonicalize_nvidia_endpoint(deployment_mode: str, endpoint: str) -> str:
    candidate = str(endpoint or "").strip()
    if deployment_mode == "build":
        if candidate and candidate.rstrip("/") != NVIDIA_BUILD_BASE_URL:
            raise ValueError("NVIDIA Build 주소는 변경할 수 없습니다.")
        return NVIDIA_BUILD_BASE_URL
    if deployment_mode != "nim":
        raise ValueError("지원하지 않는 NVIDIA 배포 방식입니다.")
    if not candidate:
        raise ValueError("사용자 NIM 주소가 필요합니다.")
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("사용자 NIM 주소 형식이 올바르지 않습니다.") from exc
    host = (parsed.hostname or "").lower()
    loopback = host == "localhost" or host.endswith(".localhost")
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if (
        parsed.scheme not in ("http", "https")
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and not loopback)
    ):
        raise ValueError("사용자 NIM 주소가 보안 정책에 맞지 않습니다.")
    hostname = f"[{host}]" if ":" in host else host
    port = parsed.port
    if port is not None and not (
        (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)
    ):
        hostname = f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), hostname, path, "", ""))


class NvidiaSseError(Exception):
    pass


class NvidiaSseParser:
    """Incremental strict UTF-8 SSE parser returning complete data frames."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._text = ""
        self._data_lines: list[str] = []

    def _line(self, line: str) -> list[str]:
        if line.endswith("\r"):
            line = line[:-1]
        if line == "":
            if not self._data_lines:
                return []
            data = "\n".join(self._data_lines)
            self._data_lines.clear()
            return [data]
        if line.startswith(":"):
            return []
        field, separator, value = line.partition(":")
        if field != "data":
            return []
        if separator and value.startswith(" "):
            value = value[1:]
        self._data_lines.append(value)
        return []

    def feed(self, chunk: bytes) -> list[str]:
        try:
            self._text += self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            raise NvidiaSseError("NVIDIA 응답의 UTF-8 인코딩이 올바르지 않습니다.") from exc
        frames: list[str] = []
        while "\n" in self._text:
            line, self._text = self._text.split("\n", 1)
            frames.extend(self._line(line))
        return frames

    def finish(self) -> list[str]:
        try:
            self._text += self._decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise NvidiaSseError("NVIDIA 응답의 UTF-8 인코딩이 올바르지 않습니다.") from exc
        # A final non-newline line may be parsed, but a pending data frame without
        # a blank delimiter is incomplete and must never become a fake completion.
        if self._text:
            self._line(self._text)
            self._text = ""
        if self._data_lines:
            raise NvidiaSseError("NVIDIA 응답 스트림이 프레임 도중 종료되었습니다.")
        return []


@dataclass
class _AttemptFailure(Exception):
    public: LlmProviderError
    retryable: bool
    received_bytes: bool
    retry_after: float = 0.0


def _safe_error(status: int, kind: LlmFailureKind, message: str) -> LlmProviderError:
    return LlmProviderError(status, message, provider_name="NVIDIA", kind=kind)


def _status_error(status: int) -> LlmProviderError:
    if status in (401, 403):
        return _safe_error(status, LlmFailureKind.AUTH, "NVIDIA 인증에 실패했습니다. API 키와 배포 대상을 확인하세요.")
    if status == 402:
        return _safe_error(status, LlmFailureKind.PAYMENT, "NVIDIA API 사용 한도 또는 결제 상태를 확인하세요.")
    if status == 429:
        return _safe_error(status, LlmFailureKind.RATE_LIMIT, "NVIDIA 요청 한도에 도달했습니다. 잠시 뒤 다시 시도하세요.")
    if status == 404:
        return _safe_error(status, LlmFailureKind.NOT_FOUND, "NVIDIA 모델 또는 API 경로를 찾을 수 없습니다.")
    if status in (400, 422):
        return _safe_error(status, LlmFailureKind.INVALID_REQUEST, "NVIDIA가 요청 형식을 거부했습니다. 모델 설정을 확인하세요.")
    if 300 <= status < 400:
        return _safe_error(status, LlmFailureKind.INVALID_REQUEST, "NVIDIA 요청 리디렉션을 보안상 거부했습니다.")
    if status >= 500:
        return _safe_error(status, LlmFailureKind.UPSTREAM, "NVIDIA 서버가 일시적인 오류를 반환했습니다.")
    return _safe_error(status, LlmFailureKind.UNKNOWN, f"NVIDIA 요청이 HTTP {status}로 실패했습니다.")


def _retry_after(value: str | None) -> float:
    if not value:
        return 0.1
    try:
        seconds = float(value)
    except ValueError:
        try:
            target = email.utils.parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0.1
    return min(_MAX_RETRY_AFTER_SECONDS, max(0.0, seconds))


class NvidiaAdapter:
    def __init__(
        self,
        endpoint: str,
        *,
        deployment_mode: str,
        api_key: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.endpoint = canonicalize_nvidia_endpoint(deployment_mode, endpoint)
        self.deployment_mode = deployment_mode
        if deployment_mode == "build" and not api_key:
            raise ValueError("NVIDIA Build API 키가 준비되지 않았습니다.")
        self._api_key = api_key
        self._transport = transport
        self._sleep = sleep

    @staticmethod
    def serialize_request(request: LlmRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [dict(message) for message in request.messages],
            "stream": True,
            "temperature": request.temperature,
            "stream_options": {"include_usage": True},
        }
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.tools is not None:
            payload["tools"] = list(request.tools)
        tool_choice = request.provider_options.get("tool_choice")
        if isinstance(tool_choice, (str, dict)):
            payload["tool_choice"] = tool_choice
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _stream_once(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        parser = NvidiaSseParser()
        received_bytes = False
        done = False
        output_tokens: int | None = None
        input_tokens: int | None = None
        total_tokens: int | None = None
        finish_reason: str | None = None
        timeout = httpx.Timeout(60.0, connect=10.0, read=60.0, write=10.0, pool=10.0)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self.endpoint}/chat/completions",
                    headers=self._headers(),
                    json=self.serialize_request(request),
                ) as response:
                    if response.status_code != 200:
                        public = _status_error(response.status_code)
                        raise _AttemptFailure(
                            public,
                            response.status_code in _RETRYABLE_STATUS,
                            False,
                            _retry_after(response.headers.get("Retry-After")),
                        )
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        received_bytes = True
                        try:
                            frames = parser.feed(chunk)
                        except NvidiaSseError as exc:
                            raise _AttemptFailure(
                                _safe_error(502, LlmFailureKind.MALFORMED, str(exc)), False, True
                            ) from exc
                        for frame in frames:
                            if frame.strip() == "[DONE]":
                                done = True
                                yield LlmEvent(
                                    kind="done",
                                    output_tokens=output_tokens,
                                    input_tokens=input_tokens,
                                    total_tokens=total_tokens,
                                    done_reason=finish_reason,
                                )
                                return
                            try:
                                data = json.loads(frame)
                            except (json.JSONDecodeError, TypeError) as exc:
                                raise _AttemptFailure(
                                    _safe_error(502, LlmFailureKind.MALFORMED, "NVIDIA SSE JSON 형식이 올바르지 않습니다."),
                                    False,
                                    True,
                                ) from exc
                            if not isinstance(data, dict) or data.get("error") is not None:
                                raise _AttemptFailure(
                                    _safe_error(502, LlmFailureKind.UPSTREAM, "NVIDIA 스트림이 오류로 종료되었습니다."),
                                    False,
                                    True,
                                )
                            usage = data.get("usage")
                            if isinstance(usage, dict):
                                input_tokens = usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else input_tokens
                                output_tokens = usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else output_tokens
                                total_tokens = usage.get("total_tokens") if isinstance(usage.get("total_tokens"), int) else total_tokens
                                yield LlmEvent(
                                    kind="usage",
                                    input_tokens=input_tokens,
                                    output_tokens=output_tokens,
                                    total_tokens=total_tokens,
                                )
                            choices = data.get("choices")
                            if choices is None:
                                continue
                            if not isinstance(choices, list):
                                raise _AttemptFailure(
                                    _safe_error(502, LlmFailureKind.MALFORMED, "NVIDIA choices 형식이 올바르지 않습니다."),
                                    False,
                                    True,
                                )
                            for choice in choices:
                                if not isinstance(choice, dict):
                                    raise _AttemptFailure(
                                        _safe_error(502, LlmFailureKind.MALFORMED, "NVIDIA choice 형식이 올바르지 않습니다."),
                                        False,
                                        True,
                                    )
                                reason = choice.get("finish_reason")
                                if isinstance(reason, str):
                                    finish_reason = reason
                                delta = choice.get("delta") or {}
                                if not isinstance(delta, dict):
                                    raise _AttemptFailure(
                                        _safe_error(502, LlmFailureKind.MALFORMED, "NVIDIA delta 형식이 올바르지 않습니다."),
                                        False,
                                        True,
                                    )
                                thinking = delta.get("reasoning_content")
                                if not isinstance(thinking, str):
                                    thinking = delta.get("reasoning") if isinstance(delta.get("reasoning"), str) else delta.get("thinking")
                                if isinstance(thinking, str) and thinking:
                                    yield LlmEvent(kind="thinking", text=thinking)
                                content = delta.get("content")
                                if isinstance(content, str) and content:
                                    yield LlmEvent(kind="content", text=content)
                                calls = delta.get("tool_calls")
                                if calls is not None:
                                    if not isinstance(calls, list):
                                        raise _AttemptFailure(
                                            _safe_error(502, LlmFailureKind.MALFORMED, "NVIDIA tool_calls 형식이 올바르지 않습니다."),
                                            False,
                                            True,
                                        )
                                    normalized: list[dict[str, Any]] = []
                                    for call in calls:
                                        if not isinstance(call, dict) or not isinstance(call.get("index"), int):
                                            raise _AttemptFailure(
                                                _safe_error(502, LlmFailureKind.MALFORMED, "NVIDIA 도구 호출 조각이 올바르지 않습니다."),
                                                False,
                                                True,
                                            )
                                        function = call.get("function") or {}
                                        if not isinstance(function, dict):
                                            raise _AttemptFailure(
                                                _safe_error(502, LlmFailureKind.MALFORMED, "NVIDIA 함수 호출 조각이 올바르지 않습니다."),
                                                False,
                                                True,
                                            )
                                        normalized.append({
                                            "index": call["index"],
                                            "id": call.get("id") if isinstance(call.get("id"), str) else "",
                                            "type": call.get("type") if isinstance(call.get("type"), str) else "function",
                                            "function": {
                                                "name": function.get("name") if isinstance(function.get("name"), str) else "",
                                                "arguments": function.get("arguments") if isinstance(function.get("arguments"), str) else "",
                                            },
                                        })
                                    if normalized:
                                        yield LlmEvent(kind="tool_call_delta", tool_calls=normalized)
                    try:
                        parser.finish()
                    except NvidiaSseError as exc:
                        raise _AttemptFailure(
                            _safe_error(502, LlmFailureKind.TRUNCATED, str(exc)), False, received_bytes
                        ) from exc
        except _AttemptFailure:
            raise
        except httpx.TimeoutException as exc:
            raise _AttemptFailure(
                _safe_error(504, LlmFailureKind.TIMEOUT, "NVIDIA 응답 시간이 초과되었습니다."),
                not received_bytes,
                received_bytes,
            ) from exc
        except httpx.RequestError as exc:
            raise _AttemptFailure(
                _safe_error(503, LlmFailureKind.CONNECT, "NVIDIA 서버에 연결할 수 없습니다."),
                not received_bytes,
                received_bytes,
            ) from exc
        if not done:
            raise _AttemptFailure(
                _safe_error(502, LlmFailureKind.TRUNCATED, "NVIDIA 응답이 완료 표식 없이 종료되었습니다."),
                False,
                received_bytes,
            )

    async def chat_stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        attempt = 0
        while True:
            try:
                stream = self._stream_once(request)
                completed = False
                try:
                    async for event in stream:
                        yield event
                    completed = True
                    return
                finally:
                    if not completed:
                        await stream.aclose()
            except _AttemptFailure as failure:
                if attempt == 0 and failure.retryable and not failure.received_bytes:
                    attempt = 1
                    await self._sleep(failure.retry_after)
                    continue
                if failure.received_bytes:
                    yield LlmEvent(kind="incomplete", error=failure.public.body)
                    return
                raise failure.public from None

    async def list_models(self) -> list[str]:
        timeout = httpx.Timeout(15.0, connect=10.0, read=15.0, write=10.0, pool=10.0)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    f"{self.endpoint}/models",
                    headers={**self._headers(), "Accept": "application/json"},
                )
            if response.status_code != 200:
                raise _status_error(response.status_code)
            try:
                payload = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                raise _safe_error(
                    502,
                    LlmFailureKind.MALFORMED,
                    "NVIDIA 모델 목록 응답 형식이 올바르지 않습니다.",
                ) from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise _safe_error(
                    502,
                    LlmFailureKind.MALFORMED,
                    "NVIDIA 모델 목록 응답 형식이 올바르지 않습니다.",
                )
            models: list[str] = []
            seen: set[str] = set()
            for item in payload["data"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
                    raise _safe_error(
                        502,
                        LlmFailureKind.MALFORMED,
                        "NVIDIA 모델 목록 항목 형식이 올바르지 않습니다.",
                    )
                model = item["id"].strip()
                if model not in seen:
                    seen.add(model)
                    models.append(model)
            return models
        except LlmProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise _safe_error(
                504,
                LlmFailureKind.TIMEOUT,
                "NVIDIA 모델 목록 응답 시간이 초과되었습니다.",
            ) from exc
        except httpx.RequestError as exc:
            raise _safe_error(
                503,
                LlmFailureKind.CONNECT,
                "NVIDIA 모델 목록 서버에 연결할 수 없습니다.",
            ) from exc

    async def inspect_capabilities(self, model: str) -> ModelCapabilities:
        target_model = model.strip()
        if not target_model:
            raise ValueError("NVIDIA 모델명이 필요합니다.")
        probe = LlmRequest(
            model=target_model,
            messages=[
                {
                    "role": "user",
                    "content": "Call the capability probe function exactly once with value 1.",
                }
            ],
            temperature=0,
            max_output_tokens=32,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": _CAPABILITY_PROBE_NAME,
                        "description": "Confirms function-call protocol support without executing a tool.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "value": {
                                    "type": "integer",
                                    "enum": [_CAPABILITY_PROBE_ARGUMENT],
                                }
                            },
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            provider_options={
                "tool_choice": {
                    "type": "function",
                    "function": {"name": _CAPABILITY_PROBE_NAME},
                }
            },
        )
        fragments: dict[int, dict[str, str]] = {}
        completed = False
        stream = self._stream_once(probe)
        stream_consumed = False
        try:
            async for event in stream:
                if event.kind == "tool_call_delta":
                    for call in event.tool_calls or []:
                        index = call.get("index")
                        if not isinstance(index, int) or index < 0:
                            return ModelCapabilities()
                        current = fragments.setdefault(
                            index,
                            {"id": "", "name": "", "arguments": ""},
                        )
                        call_id = call.get("id")
                        function = call.get("function")
                        if isinstance(call_id, str):
                            current["id"] += call_id
                        if isinstance(function, Mapping):
                            name = function.get("name")
                            arguments = function.get("arguments")
                            if isinstance(name, str):
                                current["name"] += name
                            if isinstance(arguments, str):
                                current["arguments"] += arguments
                elif event.kind == "done":
                    completed = True
                elif event.kind in ("error", "incomplete", "cancelled"):
                    return ModelCapabilities()
            stream_consumed = True
        except _AttemptFailure as failure:
            exc = failure.public
            if exc.status in (400, 422) and exc.kind is LlmFailureKind.INVALID_REQUEST:
                return ModelCapabilities(tools="unsupported")
            if exc.kind in (
                LlmFailureKind.CONNECT,
                LlmFailureKind.TIMEOUT,
                LlmFailureKind.UPSTREAM,
                LlmFailureKind.MALFORMED,
                LlmFailureKind.TRUNCATED,
            ):
                return ModelCapabilities()
            raise exc from None
        finally:
            if not stream_consumed:
                await stream.aclose()
        if not completed:
            return ModelCapabilities()
        capabilities = ModelCapabilities(chat="supported", stream="supported", tools="unknown")
        if len(fragments) != 1 or 0 not in fragments:
            return capabilities
        call = fragments[0]
        if not call["id"] or call["name"] != _CAPABILITY_PROBE_NAME:
            return capabilities
        try:
            arguments = json.loads(call["arguments"])
        except (json.JSONDecodeError, TypeError):
            return capabilities
        if arguments == {"value": _CAPABILITY_PROBE_ARGUMENT}:
            return ModelCapabilities(chat="supported", stream="supported", tools="supported")
        return capabilities

    async def prepare_model(self, model: str) -> LlmModelRuntime:
        return LlmModelRuntime(model=model)

    def prepare_attempts(
        self,
        request: LlmRequest,
        reasoning_effort: str,
        model_runtime: LlmModelRuntime,
    ) -> list[LlmRequest]:
        del reasoning_effort, model_runtime
        return [request]

    async def release_accelerator_memory(
        self,
        *,
        require_success: bool = False,
        timeout_seconds: float = 60,
    ) -> list[str]:
        del require_success, timeout_seconds
        return []
