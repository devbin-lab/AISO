"""공급자에 중립적인 Aiso LLM 경계.

실행 경로는 이 모듈의 계약과 factory만 알며, HTTP 프로토콜 세부 사항은 provider
어댑터에 둔다. Gate 1에서는 기존 Ollama 구현만 등록한다.
"""

from .contracts import (
    LlmEvent,
    LlmFailureKind,
    LlmModelRuntime,
    LlmProvider,
    LlmProviderError,
    LlmRequest,
    LlmRuntime,
    ModelCapabilities,
)
from .factory import create_provider, create_runtime

__all__ = [
    "LlmEvent",
    "LlmFailureKind",
    "LlmModelRuntime",
    "LlmProvider",
    "LlmProviderError",
    "LlmRequest",
    "LlmRuntime",
    "ModelCapabilities",
    "create_provider",
    "create_runtime",
]
