"""기존 Ollama 채팅 전송 어댑터.

Ollama NDJSON, ``think``/``num_ctx``/``keep_alive`` 및 VRAM 오프로드 사다리는
이 모듈에만 둔다. 공용 LLM 계약은 이 구현 세부 사항을 알지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, AsyncIterator, Mapping

import httpx

from llm.contracts import (
    LlmEvent,
    LlmFailureKind,
    LlmModelRuntime,
    LlmProviderError,
    LlmRequest,
    ModelCapabilities,
)


_CRASH_SIGNS = (
    "cuda error",
    "0xc0000409",
    "terminated",
    "shared object",
    "out of memory",
    "failed to allocate",
    "cudamalloc",
    "unable to allocate",
)
_layer_cache: dict[str, int | None] = {}


def _failure_kind(body: str) -> LlmFailureKind:
    lowered = body.lower()
    if any(sign in lowered for sign in _CRASH_SIGNS):
        return LlmFailureKind.LOAD_FAILURE
    if "does not support tools" in lowered or ("support" in lowered and "tools" in lowered):
        return LlmFailureKind.TOOLS_UNSUPPORTED
    if "parsing tool" in lowered or ("tool call" in lowered and ("error" in lowered or "parse" in lowered)):
        return LlmFailureKind.TOOL_PARSE
    if "think" in lowered:
        return LlmFailureKind.REASONING_UNSUPPORTED
    return LlmFailureKind.UNKNOWN


def _provider_error(status: int, body: str) -> LlmProviderError:
    return LlmProviderError(
        status,
        body,
        provider_name="Ollama",
        kind=_failure_kind(body),
    )


class OllamaAdapter:
    """Ollama의 NDJSON 채팅 프로토콜을 공용 계약으로 정규화한다."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint.rstrip("/")

    @staticmethod
    def request_from_legacy_payload(payload: Mapping[str, Any]) -> LlmRequest:
        """v0.3.1 요청 모양을 공용 요청으로 변환한다.

        이 호환 경계는 Gate 1의 무동작 변경을 위한 것이며, 호출자는 provider URL이나
        NDJSON 형식을 알 필요가 없다.
        """
        options = dict(payload.get("options") or {})
        provider_options: dict[str, Any] = {}
        if "keep_alive" in payload:
            provider_options["keep_alive"] = payload["keep_alive"]
        if "num_ctx" in options:
            provider_options["num_ctx"] = options["num_ctx"]
        if "num_gpu" in options:
            provider_options["num_gpu"] = options["num_gpu"]
        if "think" in payload:
            provider_options["think"] = payload["think"]
        return LlmRequest(
            model=str(payload.get("model") or ""),
            messages=list(payload.get("messages") or []),
            temperature=float(options.get("temperature", 0.7)),
            max_output_tokens=(int(options["num_predict"]) if "num_predict" in options else None),
            tools=payload.get("tools") if "tools" in payload else None,
            provider_options=provider_options,
        )

    @staticmethod
    def _serialize_messages(messages: Any) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for message in messages:
            copied = dict(message)
            if copied.get("role") == "assistant" and isinstance(copied.get("tool_calls"), list):
                ollama_calls: list[dict[str, Any]] = []
                for call in copied["tool_calls"]:
                    function = dict(call.get("function") or {}) if isinstance(call, dict) else {}
                    arguments = function.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            parsed = json.loads(arguments)
                            arguments = parsed if isinstance(parsed, dict) else {}
                        except json.JSONDecodeError:
                            arguments = {}
                    ollama_calls.append({
                        "function": {
                            "name": function.get("name", ""),
                            "arguments": arguments if isinstance(arguments, dict) else {},
                        }
                    })
                copied["tool_calls"] = ollama_calls
            # OpenAI 전용 필드는 Ollama payload에 내보내지 않는다. 기존 Ollama tool 메시지는
            # ID 없이 그대로 직렬화되어 v0.3.1 형식을 보존한다.
            if copied.get("role") == "tool":
                copied.pop("tool_call_id", None)
            serialized.append(copied)
        return serialized

    @classmethod
    def serialize_request(cls, request: LlmRequest) -> dict[str, Any]:
        extra = dict(request.provider_options)
        options: dict[str, Any] = {"temperature": request.temperature}
        if request.max_output_tokens is not None:
            options["num_predict"] = request.max_output_tokens
        if extra.get("num_ctx") is not None:
            options["num_ctx"] = extra["num_ctx"]
        if extra.get("num_gpu") is not None:
            options["num_gpu"] = extra["num_gpu"]
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": cls._serialize_messages(request.messages),
            "stream": True,
            "options": options,
        }
        if "keep_alive" in extra:
            payload["keep_alive"] = extra["keep_alive"]
        if request.tools is not None:
            payload["tools"] = list(request.tools)
        if "think" in extra:
            payload["think"] = extra["think"]
        return payload

    async def chat_stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        payload = self.serialize_request(request)
        timeout = httpx.Timeout(None, connect=5)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", f"{self.endpoint}/api/chat", json=payload) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="ignore")
                    raise _provider_error(response.status_code, body)
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("error"):
                        raise _provider_error(500, str(data["error"]))
                    message = data.get("message") or {}
                    thinking = message.get("thinking")
                    if thinking:
                        yield LlmEvent(kind="thinking", text=thinking)
                    content = message.get("content")
                    if content:
                        yield LlmEvent(kind="content", text=content)
                    calls = message.get("tool_calls")
                    if calls:
                        yield LlmEvent(kind="tool_call_delta", tool_calls=calls)
                    if data.get("done"):
                        yield LlmEvent(
                            kind="done",
                            output_tokens=data.get("eval_count"),
                            # prompt_eval_count = 서버가 실제로 처리한 프롬프트 토큰.
                            # 이게 없으면 compact_convo의 chars//3 예산이 num_ctx를
                            # 실제로 넘겼는지 확인할 방법이 없다(한국어 혼합에서 특히).
                            input_tokens=data.get("prompt_eval_count"),
                            total_duration=data.get("total_duration"),
                            done_reason=data.get("done_reason"),
                        )

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{self.endpoint}/api/tags")
            response.raise_for_status()
        return [model.get("name", "") for model in response.json().get("models", [])]

    async def inspect_capabilities(self, model: str) -> ModelCapabilities:
        # Gate 1에서는 모델 검사 호출을 새로 만들지 않는다. v0.3.1에서 확정된 범위만 표현한다.
        del model
        return ModelCapabilities(chat="supported", stream="supported", tools="unknown")

    async def _model_layers(self, model: str) -> int | None:
        """Ollama ``/api/show`` 기반 VRAM 오프로드 보조 정보(확정 결과만 캐시)."""
        if model in _layer_cache:
            return _layer_cache[model]
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.post(f"{self.endpoint}/api/show", json={"model": model})
                response.raise_for_status()
                info = response.json().get("model_info") or {}
        except Exception:  # noqa: BLE001 - 기존의 best-effort 동작 보존
            return None
        result: int | None = None
        for key, value in info.items():
            if key.endswith(".block_count"):
                result = int(value)
                break
        _layer_cache[model] = result
        return result

    async def prepare_model(self, model: str) -> LlmModelRuntime:
        return LlmModelRuntime(model=model, state={"layers": await self._model_layers(model)})

    @staticmethod
    def reset_cache() -> None:
        """테스트와 수명주기 관리용 Ollama 모델 메타데이터 캐시 초기화 경계."""
        _layer_cache.clear()

    @staticmethod
    def prepare_attempts(
        request: LlmRequest,
        reasoning_effort: str,
        model_runtime: LlmModelRuntime,
    ) -> list[LlmRequest]:
        """기존 Ollama think/CPU 오프로드 재시도 사다리를 요청 확장값으로 보존한다."""
        layers = model_runtime.state.get("layers")
        if not isinstance(layers, int):
            layers = None
        gpu_levels: list[int | None]
        if layers and layers > 6:
            gpu_levels = [None, max(1, int(layers * 0.55)), max(1, int(layers * 0.30))]
        else:
            gpu_levels = [None, 20, 10]
        attempts: list[LlmRequest] = []
        for gpu in gpu_levels:
            extra = dict(request.provider_options)
            if gpu is not None:
                extra["num_gpu"] = gpu
            else:
                extra.pop("num_gpu", None)
            if reasoning_effort:
                attempts.append(replace(request, provider_options={**extra, "think": reasoning_effort}))
            attempts.append(replace(request, provider_options=extra))
        return attempts

    async def release_accelerator_memory(
        self,
        *,
        require_success: bool = False,
        timeout_seconds: float = 60,
    ) -> list[str]:
        """현재 적재 모델을 best-effort로 해제한다. HTTP 세부사항은 어댑터에 한정한다."""
        unloaded: list[str] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=5)) as client:
            response = await client.get(f"{self.endpoint}/api/ps")
            models = (response.json().get("models") or []) if response.status_code == 200 else []
            for item in models:
                name = item.get("name") or item.get("model")
                if not isinstance(name, str) or not name:
                    continue
                try:
                    result = await client.post(
                        f"{self.endpoint}/api/generate",
                        json={"model": name, "prompt": "", "keep_alive": 0},
                    )
                    if require_success:
                        result.raise_for_status()
                    unloaded.append(name)
                except Exception:  # noqa: BLE001 - 한 모델 실패는 다른 모델 해제를 막지 않는다
                    continue
        return unloaded
