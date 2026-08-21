"""LLM-turn and tool-call execution primitives for Aiso.

This module contains provider-neutral stream collection, tool-call normalisation,
and runtime preparation. The public agent facade re-exports these functions so
existing integration seams remain stable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, AsyncGenerator, Callable

from llm import (
    LlmFailureKind,
    LlmModelRuntime,
    LlmProviderError,
    LlmRequest,
    LlmRuntime,
    create_runtime,
)
from llm.tool_calls import ToolCallAssembler, ToolCallProtocolError, canonicalize_tool_arguments

MAX_PARSE_RETRIES = 2
REP_MIN_LEN = 4000
REP_CHECK_EVERY = 2000


def requires_approval(
    *,
    approval_mode: str,
    approval_name: str,
    needs_approval_for_tool: Callable[[str, str], bool],
    workspace_context_exposed: bool,
    is_network_egress: bool,
) -> bool:
    """Apply the single Agent approval contract at one runtime boundary.

    Auto is intentionally unconditional: every enabled/exposed Agent tool runs
    without an approval card. Read retains ordinary tool confirmation and adds
    one confirmation before sending workspace/RAG-derived context to the web.
    """
    if approval_mode == "auto":
        return False
    return needs_approval_for_tool(approval_name, approval_mode) or (
        approval_mode == "read" and workspace_context_exposed and is_network_egress
    )


def _looks_degenerate(text: str) -> bool:
    """생성이 같은 덩어리를 반복하는 퇴행(무한 반복) 상태인지 감지한다.

    최근 텍스트 중간의 짧은 조각이 그대로 여러 번 나타나면 반복으로 본다. 정상적으로
    다양한 출력은 임의 조각이 반복되지 않으므로 오탐이 낮다(코드·표의 자연스러운 반복은
    보통 3회 미만이거나 조각이 완전 일치하지 않는다).
    """
    if len(text) < REP_MIN_LEN:
        return False
    tail = text[-3000:]
    probe = tail[1200:1400]  # 중간 200자 표본
    return bool(probe.strip()) and tail.count(probe) >= 3

async def _release_llm_for_image(host: str) -> list[str]:
    """ComfyUI에 VRAM을 넘기기 위해 runtime의 적재 모델을 best-effort로 해제한다."""
    try:
        return await create_runtime("ollama", host).release_accelerator_memory(
            require_success=True,
            timeout_seconds=30,
        )
    except Exception:  # noqa: BLE001 — 언로드 조회 실패만으로 생성 요청을 막지는 않음
        return []

async def _chat_turn(
    host: str,
    request: LlmRequest,
    runtime: LlmRuntime | None = None,
    *,
    strict_tool_protocol: bool = False,
) -> AsyncGenerator[dict, None]:
    """공용 LLM 이벤트 한 턴을 기존 Agent 최종 결과로 모은다."""
    content = ""
    thinking = ""
    # 조립기를 쓰지 않는 공급자에서는 LlmEvent.tool_calls(Sequence[Mapping[str, Any]])가
    # 그대로 쌓인다. 여기서 dict 로 복사하면 불필요한 사본이 생기므로, 누적 리스트의
    # 실제 계약인 Mapping 으로 적는다. 아래 조립기 경로가 채우는 dict 리터럴도
    # Mapping 이므로 두 경로 모두 이 타입을 만족한다.
    tool_calls: list[Mapping[str, Any]] = []
    done_reason = None
    output_tokens = 0  # eval_count — 이 턴에 '생성'된 토큰
    # prompt_eval_count — 서버가 실제로 처리한 프롬프트 토큰. 사용량 집계가 아니라
    # 컨텍스트 예산 관측용이다: compact_convo의 chars//3 추정이 num_ctx를 실제로
    # 넘겼는지 확인할 유일한 피드백. 공급자가 안 주면 None으로 남는다.
    input_tokens: int | None = None
    rep_next = REP_MIN_LEN        # content가 이 길이를 넘으면 반복 퇴행 검사
    rep_next_think = REP_MIN_LEN  # thinking도 동일하게 검사 (사고 채널에서 폭주하는 경우)
    runtime = runtime or create_runtime("ollama", host)
    assembler = ToolCallAssembler() if strict_tool_protocol else None
    saw_done = False
    stream = runtime.chat_stream(request)
    stream_completed = False
    try:
        async for event in stream:
            if event.kind == "thinking":
                thinking += event.text
                yield {"type": "thinking", "text": event.text}
                # 사고(thinking) 채널에서 같은 덩어리를 무한 반복하는 퇴행도 끊는다.
                # (content만 보면 놓친다 — 실제 폭주는 종종 thinking에서 먼저 터진다.)
                if len(thinking) >= rep_next_think:
                    rep_next_think = len(thinking) + REP_CHECK_EVERY
                    if _looks_degenerate(thinking):
                        done_reason = "repetition"
                        break
            elif event.kind == "content":
                content += event.text
                yield {"type": "content", "text": event.text}
                # 같은 덩어리를 무한 반복하는 퇴행이면 스트림을 끊는다(num_predict보다 훨씬 일찍).
                if len(content) >= rep_next:
                    rep_next = len(content) + REP_CHECK_EVERY
                    if _looks_degenerate(content):
                        done_reason = "repetition"
                        break
            elif event.kind == "tool_call_delta":
                if assembler is not None:
                    assembler.add(event.tool_calls or [])
                else:
                    tool_calls.extend(event.tool_calls or [])
            elif event.kind == "done":
                if saw_done:
                    raise ToolCallProtocolError("LLM 완료 이벤트가 중복되었습니다.")
                saw_done = True
                done_reason = event.done_reason
                output_tokens = event.output_tokens or 0
                input_tokens = event.input_tokens
            elif event.kind in ("cancelled", "incomplete", "error"):
                raise ToolCallProtocolError(event.error or "LLM 응답 스트림이 완전하게 종료되지 않았습니다.")
        stream_completed = True
    finally:
        # 중단·취소·공급자 오류일 때만 HTTP 스트림을 즉시 정리한다.
        # 정상 소비 뒤에는 이미 소진된 자식 제너레이터를 다시 닫지 않는다.
        if not stream_completed:
            await stream.aclose()
    if assembler is not None:
        assembled = assembler.finalize(saw_done=saw_done, finish_reason=done_reason)
        tool_calls = [
            {
                "index": call.index,
                "provider_tool_call_id": call.provider_tool_call_id,
                "function": {
                    "name": call.name,
                    "arguments": call.arguments,
                },
                "canonical_arguments": call.canonical_arguments,
            }
            for call in assembled
        ]
    yield {
        "_final": True,
        "content": content,
        "thinking": thinking,
        "tool_calls": tool_calls,
        "done_reason": done_reason,
        "output_tokens": output_tokens,
        "input_tokens": input_tokens,
    }

def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Decode a JSON object without accepting a later duplicate-key override."""
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise ToolCallProtocolError(f"도구 인자에 중복 키가 있습니다: {key}")
        parsed[key] = value
    return parsed


def _parse_args(raw: Any) -> dict[str, Any]:
    """Accept only one unambiguous JSON object at the execution boundary.

    A malformed call must never silently become ``{}``, because many tools
    have defaults that can turn that placeholder into a real filesystem or
    network operation.  Provider-specific parsers may already have checked a
    call, but this final normalisation step is deliberately provider-neutral.
    """
    parsed: Any
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
        except (json.JSONDecodeError, ToolCallProtocolError) as error:
            raise ToolCallProtocolError(f"도구 인자가 유효한 JSON 객체가 아닙니다: {error}") from error
    else:
        raise ToolCallProtocolError("도구 인자는 JSON 객체여야 합니다.")
    if not isinstance(parsed, dict):
        raise ToolCallProtocolError("도구 인자는 JSON 객체여야 합니다.")
    if not all(isinstance(key, str) for key in parsed):
        raise ToolCallProtocolError("도구 인자 키는 문자열이어야 합니다.")
    return parsed

def _normalize_tool_calls(raw_calls: Any, assistant_turn_id: str) -> list[dict[str, Any]]:
    if not isinstance(raw_calls, list):
        raise ToolCallProtocolError("도구 호출 목록 형식이 올바르지 않습니다.")
    normalized: list[dict[str, Any]] = []
    provider_ids: set[str] = set()
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
            raise ToolCallProtocolError("도구 호출 형식이 올바르지 않습니다.")
        function = raw["function"]
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise ToolCallProtocolError("도구 함수명이 없습니다.")
        arguments = function.get("arguments")
        parsed = _parse_args(arguments)
        provider_id = raw.get("provider_tool_call_id") or raw.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            provider_id = f"ollama-{assistant_turn_id}-{index}"
        if provider_id in provider_ids:
            raise ToolCallProtocolError("provider 도구 호출 ID가 중복되었습니다.")
        provider_ids.add(provider_id)
        # The provider's canonical representation is metadata, not authority.
        # Recompute it from the validated object so the execution ledger and
        # repeat guards always describe what will actually run.
        canonical = canonicalize_tool_arguments(parsed)
        normalized.append(
            {
                "index": index,
                "provider_tool_call_id": provider_id,
                "function": {"name": name, "arguments": parsed},
                "canonical_arguments": canonical,
            }
        )
    return normalized


def _schema_error(tool_name: str, path: str, message: str) -> ToolCallProtocolError:
    return ToolCallProtocolError(f"{tool_name} 인자 {path}: {message}")


def _validate_schema_value(
    tool_name: str,
    value: Any,
    schema: Mapping[str, Any],
    path: str,
) -> None:
    """Validate the safe JSON-schema subset used by Aiso function tools.

    Tool schemas are the executable contract between the model and the local
    handler.  Validation here does not replace handler-side validation; it
    prevents malformed or surplus model arguments from reaching defaults,
    filesystem paths, or external calls in the first place.
    """
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            raise _schema_error(tool_name, path, "JSON object is required")
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        required = schema.get("required")
        required = required if isinstance(required, list) else []
        for required_key in required:
            if isinstance(required_key, str) and required_key not in value:
                raise _schema_error(tool_name, path, f"missing required key '{required_key}'")
        additional = schema.get("additionalProperties")
        # Existing Aiso schemas follow normal JSON Schema semantics: omitted
        # additionalProperties means a handler may ignore forward-compatible
        # hints (for example legacy image technical settings).  Explicit false
        # remains a hard execution boundary.  Required/type/enum validation is
        # still enforced for every declared input.
        strict_properties = additional is False
        for key, child in value.items():
            if key not in properties:
                if strict_properties or additional is False:
                    raise _schema_error(tool_name, f"{path}.{key}", "unknown key")
                if isinstance(additional, Mapping):
                    _validate_schema_value(tool_name, child, additional, f"{path}.{key}")
                continue
            child_schema = properties[key]
            if isinstance(child_schema, Mapping):
                _validate_schema_value(tool_name, child, child_schema, f"{path}.{key}")
    elif expected_type == "array":
        if not isinstance(value, list):
            raise _schema_error(tool_name, path, "JSON array is required")
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise _schema_error(tool_name, path, f"requires at least {min_items} item(s)")
        if isinstance(max_items, int) and len(value) > max_items:
            raise _schema_error(tool_name, path, f"allows at most {max_items} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                _validate_schema_value(tool_name, item, item_schema, f"{path}[{index}]")
    elif expected_type == "string":
        if not isinstance(value, str):
            raise _schema_error(tool_name, path, "string is required")
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise _schema_error(tool_name, path, f"minimum length is {min_length}")
        if isinstance(max_length, int) and len(value) > max_length:
            raise _schema_error(tool_name, path, f"maximum length is {max_length}")
    elif expected_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _schema_error(tool_name, path, "integer is required")
    elif expected_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _schema_error(tool_name, path, "number is required")
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            raise _schema_error(tool_name, path, "boolean is required")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise _schema_error(tool_name, path, f"must be one of {enum!r}")
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(minimum, (int, float)) and value < minimum:
            raise _schema_error(tool_name, path, f"must be at least {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise _schema_error(tool_name, path, f"must be at most {maximum}")


def validate_tool_arguments(tool_name: str, arguments: dict[str, Any], schema: Mapping[str, Any]) -> None:
    """Validate one normalised tool call against its exposed function schema."""
    function = schema.get("function")
    if not isinstance(function, Mapping):
        raise ToolCallProtocolError(f"{tool_name} 도구 스키마가 올바르지 않습니다.")
    parameters = function.get("parameters")
    if not isinstance(parameters, Mapping):
        raise ToolCallProtocolError(f"{tool_name} 도구 인자 스키마가 없습니다.")
    _validate_schema_value(tool_name, arguments, parameters, "$")

async def _generate_turn(
    host: str, base: LlmRequest, reasoning_effort: str, model_runtime: LlmModelRuntime,
    offload_noticed: bool, runtime: LlmRuntime | None = None, *, strict_tool_protocol: bool = False,
    chat_turn: Callable[..., AsyncGenerator[dict, None]] | None = None,
) -> AsyncGenerator[dict, None]:
    """한 턴 생성 — 오프로드 사다리 + gpt-oss 파싱오류 재생성 + 스트리밍을 캡슐화한다.

    스트림/알림 이벤트(thinking·content·notice)는 그대로 yield하고, 마지막에 딱 하나의
    종료 마커를 yield하고 끝난다:
        {"_gen": True, "final": <dict|None>, "error": <str|None>, "offload_noticed": bool}
    - final 있음 → 성공(툴콜/컨텐츠를 담은 _chat_turn 최종 이벤트).
    - error 있음 → 치명적 종료(호출자가 그대로 error 이벤트로 내보내고 런 종료).
    offload_noticed는 '런 1회만 알림' 정책을 유지하려 들어오고 갱신되어 나간다.
    """
    parse_retries = 0
    while True:
        final = None
        yielded_any = False  # 이 시도에서 이미 토큰을 흘렸는지 (중복 렌더 방지)
        parse_failed = False
        turn_runtime = runtime or create_runtime("ollama", host)
        attempts = turn_runtime.prepare_attempts(base, reasoning_effort, model_runtime)
        for i, attempt in enumerate(attempts):
            try:
                turn = chat_turn or _chat_turn
                turn_stream = (
                    turn(host, attempt)
                    if runtime is None
                    else turn(
                        host,
                        attempt,
                        turn_runtime,
                        strict_tool_protocol=strict_tool_protocol,
                    )
                )
                turn_completed = False
                try:
                    async for ev in turn_stream:
                        if ev.get("_final"):
                            final = ev
                        else:
                            yielded_any = True
                            yield ev
                    turn_completed = True
                finally:
                    if not turn_completed:
                        await turn_stream.aclose()
                break
            except LlmProviderError as e:
                # 스트리밍 전에 난 파싱 오류(내용 미출력)면 재생성으로 회복 가능
                if e.kind is LlmFailureKind.TOOL_PARSE and not yielded_any:
                    parse_failed = True
                    final = None
                    break
                last = i == len(attempts) - 1
                load_failure = e.kind is LlmFailureKind.LOAD_FAILURE
                if not last and (load_failure or e.kind is LlmFailureKind.REASONING_UNSUPPORTED):
                    if load_failure and not offload_noticed:
                        offload_noticed = True
                        yield {
                            "type": "notice",
                            "text": "VRAM 부족 — CPU 오프로드로 실행합니다 (느려질 수 있어요)",
                        }
                    continue
                yield {"_gen": True, "final": None,
                       "error": f"{e.provider_name} 오류 ({e.status}): {e.body[:300]}",
                       "error_kind": e.kind,
                       "offload_noticed": offload_noticed}
                return
            except Exception as e:  # noqa: BLE001
                yield {"_gen": True, "final": None, "error": f"연결 실패: {e}",
                       "offload_noticed": offload_noticed}
                return

        if parse_failed and parse_retries < MAX_PARSE_RETRIES:
            parse_retries += 1
            if parse_retries == 1:
                yield {"type": "notice", "text": "모델 출력 형식 오류(도구 호출 파싱) — 다시 생성합니다…"}
            continue  # 같은 요청으로 재생성 (temperature 편차로 대개 회복)
        break

    if final is None:
        err = (
            "모델이 올바른 형식의 응답을 만들지 못했습니다(도구 호출 파싱 반복 실패). "
            "추론 강도를 낮추거나 다시 시도해보세요."
            if parse_failed else "빈 응답"
        )
        yield {"_gen": True, "final": None, "error": err,
               "error_kind": LlmFailureKind.UNKNOWN, "offload_noticed": offload_noticed}
        return
    yield {"_gen": True, "final": final, "error": None,
           "error_kind": None, "offload_noticed": offload_noticed}

async def _prepare_model(
    host: str, model: str, runtime: LlmRuntime | None = None
) -> LlmModelRuntime:
    """실행 시작 시 runtime 모델 준비 결과를 고정한다."""
    return await (runtime or create_runtime("ollama", host)).prepare_model(model)


__all__ = [
    "_chat_turn",
    "_generate_turn",
    "_looks_degenerate",
    "_normalize_tool_calls",
    "_parse_args",
    "_prepare_model",
    "_release_llm_for_image",
    "requires_approval",
    "validate_tool_arguments",
]
