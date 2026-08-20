"""Regression contracts at the model/tool protocol and context boundary.

These are intentionally separate from request-routing tests.  A model may
select the correct tool and still be unsafe if its call payload is ambiguous,
or unreliable if one large recent tool result consumes the next model turn.
"""
from __future__ import annotations

import pytest

import agent
import agent_execution as execution
from llm.tool_calls import ToolCallProtocolError, canonicalize_tool_arguments
from toolspec import MODEL_AGENT_TOOLS


def _raw_call(arguments: object, **extra: object) -> list[dict[str, object]]:
    return [
        {
            "id": "provider-call-1",
            "function": {"name": "web_search", "arguments": arguments},
            **extra,
        }
    ]


@pytest.mark.parametrize(
    "arguments",
    (
        '{"q":',  # malformed JSON
        '["a", "tool argument array is not an object"]',
        '"tool argument scalar is not an object"',
        '{"q":"first","q":"conflicting duplicate"}',
        42,
        None,
    ),
)
def test_normalize_tool_calls_rejects_ambiguous_or_non_object_arguments(arguments: object) -> None:
    """Never turn malformed model arguments into an empty/default tool call."""
    with pytest.raises(ToolCallProtocolError):
        execution._normalize_tool_calls(_raw_call(arguments), "turn-1")


def test_normalize_tool_calls_recomputes_canonical_arguments_from_parsed_payload() -> None:
    """A provider-supplied canonical string is untrusted metadata, not authority."""
    parsed = {"query": "Aiso", "limit": 3}
    calls = execution._normalize_tool_calls(
        _raw_call(parsed, canonical_arguments='{"query":"tampered"}'),
        "turn-1",
    )

    assert calls[0]["function"]["arguments"] == parsed
    assert calls[0]["canonical_arguments"] == canonicalize_tool_arguments(parsed)
    assert calls[0]["canonical_arguments"] != '{"query":"tampered"}'


def test_declared_tool_argument_types_and_required_keys_are_checked_before_dispatch() -> None:
    web_search = next(
        schema for schema in MODEL_AGENT_TOOLS
        if schema["function"]["name"] == "web_search"
    )

    with pytest.raises(ToolCallProtocolError, match="missing required key"):
        execution.validate_tool_arguments("web_search", {}, web_search)
    with pytest.raises(ToolCallProtocolError, match="string is required"):
        execution.validate_tool_arguments("web_search", {"query": 42}, web_search)


def test_recording_caps_a_huge_tool_result_without_losing_tool_identity() -> None:
    """Recent tool output must not monopolize the next model context window.

    계약 이동(의도): 이 절단은 예전에 `compact_convo`가 매 턴 수행했다. 그러면 캡이
    대화 길이의 함수가 되어 같은 결과가 턴마다 다른 바이트로 실리고, KV 프리픽스가
    계속 깨졌다(실측: 한 결과가 한 런에서 아홉 번 다시 잘림). 이제 절단은 대화에
    **기록되는 순간 한 번** 일어나고, 그 뒤로 바이트가 변하지 않는다.

    검증 대상은 동일하다 — 거대한 도구 결과가 컨텍스트를 독점하지 않고, 도구 식별자와
    본문 앞부분(인용 근거)은 보존된다. 실행 지점만 옮겼다.
    """
    huge_result = "payload-prefix: source document excerpt\n" + ("0123456789" * 20_000)
    convo = agent.ModelConversation(
        tool_result_cap=agent.tool_result_cap(context_length=4096, reserve_tokens=1200)
    )
    convo.append({"role": "user", "content": "Read the document."})
    convo.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call-42", "function": {"name": "read_file", "arguments": {"path": "brief.pdf"}}}
        ],
    })
    convo.append({
        "role": "tool",
        "tool_call_id": "call-42",
        "name": "read_file",
        "content": huge_result,
    })
    convo.append({"role": "assistant", "content": "I will use the cited excerpt."})

    result = convo[2]
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call-42"
    assert result["name"] == "read_file"
    assert "payload-prefix" in str(result["content"])
    assert len(str(result["content"])) < 5_000
    assert result["content"] != huge_result

    # compact_convo를 몇 번 통과시켜도 그 바이트는 그대로다.
    for _ in range(3):
        compacted = agent.compact_convo(convo, context_length=4096, reserve_tokens=1200)
        assert compacted[2]["content"] == result["content"]
