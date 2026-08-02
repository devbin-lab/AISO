from __future__ import annotations

import pytest

from llm.tool_calls import ToolCallAssembler, ToolCallProtocolError


def delta(index, *, call_id="", name="", arguments="", call_type=""):
    return {
        "index": index,
        "id": call_id,
        "type": call_type,
        "function": {"name": name, "arguments": arguments},
    }


def test_single_split_call_is_reassembled_only_after_valid_completion():
    assembler = ToolCallAssembler()
    assembler.add([delta(0, call_id="call_", name="get_", arguments='{"z":')])
    assembler.add([delta(0, call_id="1", name="status", arguments='2,"a":1}')])
    calls = assembler.finalize(saw_done=True, finish_reason="tool_calls")
    assert [(call.index, call.provider_tool_call_id, call.name) for call in calls] == [
        (0, "call_1", "get_status")
    ]
    assert calls[0].arguments == {"z": 2, "a": 1}
    assert calls[0].canonical_arguments == '{"a":1,"z":2}'


def test_interleaved_parallel_calls_are_returned_in_stable_index_order():
    assembler = ToolCallAssembler()
    assembler.add([
        delta(1, call_id="call-b", name="get_", arguments='{"b":'),
        delta(0, call_id="call-a", name="get_", arguments='{"a":'),
    ])
    assembler.add([
        delta(0, name="status", arguments="1}"),
        delta(1, name="status", arguments="2}"),
    ])
    calls = assembler.finalize(saw_done=True, finish_reason="tool_calls")
    assert [call.provider_tool_call_id for call in calls] == ["call-a", "call-b"]
    assert [call.arguments for call in calls] == [{"a": 1}, {"b": 2}]


@pytest.mark.parametrize(
    "bad_delta",
    [
        delta(-1, call_id="x", name="ok", arguments="{}"),
        delta(True, call_id="x", name="ok", arguments="{}"),
        {"index": 0, "id": "x", "function": None},
        delta(0, call_id="x", name="bad name", arguments="{}"),
    ],
)
def test_bad_fragments_never_finalize(bad_delta):
    assembler = ToolCallAssembler()
    if bad_delta["index"] in (-1, True) or bad_delta.get("function") is None:
        with pytest.raises(ToolCallProtocolError):
            assembler.add([bad_delta])
    else:
        assembler.add([bad_delta])
        with pytest.raises(ToolCallProtocolError):
            assembler.finalize(saw_done=True, finish_reason="tool_calls")


@pytest.mark.parametrize(
    ("saw_done", "finish_reason"),
    [(False, None), (False, "tool_calls"), (True, None), (True, "stop"), (True, "length")],
)
def test_partial_cancelled_or_wrong_finish_never_releases_calls(saw_done, finish_reason):
    assembler = ToolCallAssembler()
    assembler.add([delta(0, call_id="call-a", name="get_status", arguments="{}")])
    with pytest.raises(ToolCallProtocolError):
        assembler.finalize(saw_done=saw_done, finish_reason=finish_reason)


def test_duplicate_provider_id_and_non_contiguous_indexes_are_rejected():
    duplicate = ToolCallAssembler()
    duplicate.add([
        delta(0, call_id="same", name="get_status", arguments="{}"),
        delta(1, call_id="same", name="get_status", arguments="{}"),
    ])
    with pytest.raises(ToolCallProtocolError):
        duplicate.finalize(saw_done=True, finish_reason="tool_calls")

    gap = ToolCallAssembler()
    gap.add([delta(1, call_id="call-b", name="get_status", arguments="{}")])
    with pytest.raises(ToolCallProtocolError):
        gap.finalize(saw_done=True, finish_reason="tool_calls")


def test_duplicate_index_collision_and_ambiguous_json_are_rejected():
    collision = ToolCallAssembler()
    collision.add([delta(0, call_id="call-a", name="get_status", arguments="{}")])
    with pytest.raises(ToolCallProtocolError):
        collision.add([delta(0, call_id="call-b", name="other", arguments="{}")])

    duplicate_key = ToolCallAssembler()
    duplicate_key.add([
        delta(0, call_id="call-a", name="get_status", arguments='{"x":1,"x":2}')
    ])
    with pytest.raises(ToolCallProtocolError):
        duplicate_key.finalize(saw_done=True, finish_reason="tool_calls")
