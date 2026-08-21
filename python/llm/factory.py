"""등록된 LLM 공급자 생성. 공용 factory는 전송 세부 사항을 알지 않는다."""

from __future__ import annotations

from typing import Callable

from .contracts import LlmRuntime
from .providers.ollama import OllamaAdapter
from .providers.nvidia import NvidiaAdapter


_RUNTIMES: dict[str, Callable[[str], LlmRuntime]] = {
    "ollama": OllamaAdapter,
}


def create_runtime(
    name: str,
    endpoint: str,
    *,
    credential: str | None = None,
    deployment_mode: str | None = None,
) -> LlmRuntime:
    """명시적으로 선택된 공급자 어댑터를 만든다.

    Gate 1은 기존 Ollama만 등록한다. 자동 전환이나 네트워크 호출은 하지 않는다.
    """
    if name == "nvidia":
        return NvidiaAdapter(
            endpoint,
            deployment_mode=deployment_mode or "build",
            api_key=credential,
        )
    try:
        return _RUNTIMES[name](endpoint)
    except KeyError as exc:
        raise ValueError(f"지원하지 않는 LLM 공급자: {name}") from exc
