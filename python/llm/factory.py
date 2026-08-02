"""등록된 LLM 공급자 생성. 공용 factory는 전송 세부 사항을 알지 않는다."""

from __future__ import annotations

from typing import Callable

from .contracts import LlmRuntime
from .providers.ollama import OllamaAdapter


_RUNTIMES: dict[str, Callable[[str], LlmRuntime]] = {
    "ollama": OllamaAdapter,
}


def create_runtime(name: str, endpoint: str) -> LlmRuntime:
    """명시적으로 선택된 공급자 어댑터를 만든다.

    Gate 1은 기존 Ollama만 등록한다. 자동 전환이나 네트워크 호출은 하지 않는다.
    """
    try:
        return _RUNTIMES[name](endpoint)
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 LLM 공급자: {name}") from exc


def create_provider(name: str, endpoint: str) -> LlmRuntime:
    """Gate 1 호환 별칭. 새 실행 경로는 ``create_runtime``을 사용한다."""
    return create_runtime(name, endpoint)
