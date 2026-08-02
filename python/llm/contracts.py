"""LLM 공용 계약. 이 모듈에는 공급자 URL·HTTP 헤더·와이어 형식이 없다."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Literal, Mapping, Protocol, Sequence


CapabilityState = Literal["supported", "unsupported", "unknown"]


class LlmFailureKind(str, Enum):
    """실행 경로가 공급자 이름 없이 처리할 수 있는 재시도/폴백 분류."""

    UNKNOWN = "unknown"
    LOAD_FAILURE = "load_failure"
    REASONING_UNSUPPORTED = "reasoning_unsupported"
    TOOLS_UNSUPPORTED = "tools_unsupported"
    TOOL_PARSE = "tool_parse"


class LlmProviderError(Exception):
    """공급자 통신/프로토콜 실패의 공용 오류.

    어댑터가 provider 이름과 실패 분류를 채우고, 실행 경로는 이 공개 타입만
    사용해 기존의 오류 문구·재시도 정책을 유지한다.
    """

    def __init__(
        self,
        status: int,
        body: str,
        *,
        provider_name: str = "LLM",
        kind: LlmFailureKind = LlmFailureKind.UNKNOWN,
    ):
        self.status = status
        self.body = body
        self.provider_name = provider_name
        self.kind = kind
        super().__init__(f"HTTP {status}: {body[:200]}")


@dataclass(frozen=True)
class LlmRequest:
    """공급자 중립 생성 입력.

    ``provider_options``는 기존 공급자의 고유 동작을 보존하는 불투명 확장 공간이다.
    공용 코어는 값을 읽거나 분기하지 않고 해당 어댑터만 해석한다.
    """

    model: str
    messages: Sequence[Mapping[str, Any]]
    temperature: float = 0.7
    max_output_tokens: int | None = None
    tools: Sequence[Mapping[str, Any]] | None = None
    provider_options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LlmEvent:
    """스트리밍 공급자 응답의 공용 이벤트."""

    kind: Literal[
        "content", "thinking", "tool_call_delta", "usage", "done", "cancelled", "incomplete", "error"
    ]
    text: str = ""
    tool_calls: Sequence[Mapping[str, Any]] | None = None
    output_tokens: int | None = None
    total_duration: int | None = None
    done_reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    chat: CapabilityState = "unknown"
    stream: CapabilityState = "unknown"
    tools: CapabilityState = "unknown"


@dataclass(frozen=True)
class LlmModelRuntime:
    """실행 시작 시 고정하는 공급자별 모델 준비 결과.

    ``state``는 런타임만 해석하는 불투명 값이다. 실행 코드는 URL·하드웨어·공급자
    프로토콜을 알 필요 없이 이 snapshot을 재시도 계획에 넘긴다.
    """

    model: str
    state: Mapping[str, Any] = field(default_factory=dict)


class LlmRuntime(Protocol):
    """모든 공급자가 노출하는 최소 공용 인터페이스."""

    def chat_stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]: ...

    async def list_models(self) -> list[str]: ...

    async def inspect_capabilities(self, model: str) -> ModelCapabilities: ...

    async def prepare_model(self, model: str) -> LlmModelRuntime: ...

    def prepare_attempts(
        self,
        request: LlmRequest,
        reasoning_effort: str,
        model_runtime: LlmModelRuntime,
    ) -> list[LlmRequest]: ...

    async def release_accelerator_memory(
        self,
        *,
        require_success: bool = False,
        timeout_seconds: float = 60,
    ) -> list[str]: ...


# Gate 1 호환 별칭. 이후 실행 코드는 LlmRuntime만 사용한다.
LlmProvider = LlmRuntime
