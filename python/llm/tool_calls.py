"""Strict provider-neutral assembly for streamed function tool calls.

The assembler is deliberately pure: it never executes a tool and it only
returns calls after the provider stream has ended with an explicit, compatible
finish reason.  This keeps partial/cancelled streams outside the execution
boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Sequence


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_PROVIDER_ID = 512
_MAX_ARGUMENTS = 64 * 1024


class ToolCallProtocolError(ValueError):
    """The provider emitted an ambiguous or malformed tool-call protocol."""


@dataclass(frozen=True)
class AssembledToolCall:
    index: int
    provider_tool_call_id: str
    name: str
    arguments: dict[str, Any]
    canonical_arguments: str


@dataclass
class _Fragments:
    call_type: str = ""
    provider_id: str = ""
    name: str = ""
    arguments: str = ""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ToolCallProtocolError("도구 인자 JSON에 중복 키가 있습니다.")
        result[key] = value
    return result


def canonicalize_tool_arguments(arguments: Mapping[str, Any]) -> str:
    """Return the stable JSON representation used by the execution ledger."""
    try:
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ToolCallProtocolError("도구 인자를 안전하게 직렬화할 수 없습니다.") from exc


class ToolCallAssembler:
    def __init__(self) -> None:
        self._calls: dict[int, _Fragments] = {}

    def add(self, deltas: Sequence[Mapping[str, Any]]) -> None:
        for delta in deltas:
            if not isinstance(delta, Mapping):
                raise ToolCallProtocolError("도구 호출 조각 형식이 올바르지 않습니다.")
            index = delta.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index > 255:
                raise ToolCallProtocolError("도구 호출 index가 올바르지 않습니다.")
            function = delta.get("function")
            if not isinstance(function, Mapping):
                raise ToolCallProtocolError("도구 호출 함수 조각이 없습니다.")

            call_type = delta.get("type", "")
            provider_id = delta.get("id", "")
            name = function.get("name", "")
            arguments = function.get("arguments", "")
            if not all(isinstance(value, str) for value in (call_type, provider_id, name, arguments)):
                raise ToolCallProtocolError("도구 호출 문자열 조각 형식이 올바르지 않습니다.")
            if call_type and call_type != "function":
                raise ToolCallProtocolError("함수형이 아닌 도구 호출은 지원하지 않습니다.")

            current = self._calls.setdefault(index, _Fragments())
            # A second complete-looking start for an occupied index is not a
            # continuation.  Treat it as a collision instead of guessing which
            # provider call should own the index.
            if (
                current.provider_id
                and current.name
                and current.arguments
                and provider_id
                and name
                and arguments.lstrip().startswith(("{", "["))
            ):
                raise ToolCallProtocolError("같은 index에 서로 다른 도구 호출이 충돌했습니다.")
            if call_type:
                current.call_type = call_type
            current.provider_id += provider_id
            current.name += name
            current.arguments += arguments
            if len(current.provider_id) > _MAX_PROVIDER_ID:
                raise ToolCallProtocolError("provider 도구 호출 ID가 너무 깁니다.")
            if len(current.name) > 128 or len(current.arguments) > _MAX_ARGUMENTS:
                raise ToolCallProtocolError("도구 호출 조각이 허용 범위를 초과했습니다.")

    def finalize(self, *, saw_done: bool, finish_reason: str | None) -> list[AssembledToolCall]:
        if not self._calls:
            if not saw_done:
                raise ToolCallProtocolError("응답 스트림이 완료되지 않았습니다.")
            return []
        if not saw_done or finish_reason != "tool_calls":
            raise ToolCallProtocolError("도구 호출 응답이 올바른 완료 표식 없이 종료되었습니다.")
        indexes = sorted(self._calls)
        if indexes != list(range(len(indexes))):
            raise ToolCallProtocolError("도구 호출 index가 연속적이지 않습니다.")

        result: list[AssembledToolCall] = []
        seen_provider_ids: set[str] = set()
        for index in indexes:
            fragments = self._calls[index]
            if fragments.call_type not in ("", "function"):
                raise ToolCallProtocolError("도구 호출 형식이 올바르지 않습니다.")
            if not fragments.provider_id or fragments.provider_id in seen_provider_ids:
                raise ToolCallProtocolError("provider 도구 호출 ID가 없거나 중복되었습니다.")
            if not _NAME_RE.fullmatch(fragments.name):
                raise ToolCallProtocolError("도구 함수명이 올바르지 않습니다.")
            try:
                parsed = json.loads(
                    fragments.arguments,
                    object_pairs_hook=_object_without_duplicate_keys,
                )
            except ToolCallProtocolError:
                raise
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ToolCallProtocolError("도구 인자 JSON이 올바르지 않습니다.") from exc
            if not isinstance(parsed, dict):
                raise ToolCallProtocolError("도구 인자는 JSON 객체여야 합니다.")
            canonical = canonicalize_tool_arguments(parsed)
            seen_provider_ids.add(fragments.provider_id)
            result.append(
                AssembledToolCall(
                    index=index,
                    provider_tool_call_id=fragments.provider_id,
                    name=fragments.name,
                    arguments=parsed,
                    canonical_arguments=canonical,
                )
            )
        return result
