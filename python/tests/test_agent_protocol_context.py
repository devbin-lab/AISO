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


def test_compact_convo_caps_a_recent_huge_tool_result_without_losing_tool_identity() -> None:
    """Recent tool output must not monopolize the next model context window."""
    huge_result = "payload-prefix: source document excerpt\n" + ("0123456789" * 20_000)
    convo = [
        {"role": "user", "content": "Read the document."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call-42", "function": {"name": "read_file", "arguments": {"path": "brief.pdf"}}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-42",
            "name": "read_file",
            "content": huge_result,
        },
        {"role": "assistant", "content": "I will use the cited excerpt."},
    ]

    compacted = agent.compact_convo(convo, context_length=4096, reserve_tokens=1200)
    result = compacted[2]

    assert result["role"] == "tool"
    assert result["tool_call_id"] == "call-42"
    assert result["name"] == "read_file"
    assert "payload-prefix" in str(result["content"])
    assert len(str(result["content"])) < 5_000
    assert result["content"] != huge_result
