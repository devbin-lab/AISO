# -*- coding: utf-8 -*-
"""런 경계를 넘어온 도구 이력은 항상 완전한 짝이어야 한다.

'계속해줘'가 백지에서 시작하지 않으려면 이전 실행의 도구 결과를 다음 요청에 실어야
한다. 그런데 OpenAI 호환 공급자는 **tool_calls를 낸 assistant 메시지마다 각 호출
ID에 대응하는 tool 메시지**를 요구하고, 짝이 맞지 않으면 요청 전체를 400으로 거부한다.

중단된 실행에서는 짝이 깨지기 쉽다 — 모델이 도구 3개를 부르고 1개만 실행된 뒤
안전 한도로 멈추면, 화면에는 호출 3개와 결과 1개가 남는다. 그걸 그대로 보내면
다음 요청이 통째로 실패한다.

그래서 화면이 보내는 이력을 그대로 믿지 않고 서버에서 짝을 강제한다:
  - 결과가 없는 호출은 assistant에서 떼어낸다.
  - 대응하는 호출이 없는 tool 메시지는 버린다.
  - 남는 것은 항상 완전한 짝뿐이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import ChatMessage, _paired_tool_history  # noqa: E402


def _call(call_id: str, name: str = "read_file", **args):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def _msg(role, content="", **extra):
    return ChatMessage(role=role, content=content, **extra)


def _roles(prepared):
    return [m["role"] for m in prepared]


def test_plain_conversation_passes_through():
    prepared = _paired_tool_history([
        _msg("user", "안녕"),
        _msg("assistant", "안녕하세요"),
    ])
    assert prepared == [
        {"role": "user", "content": "안녕"},
        {"role": "assistant", "content": "안녕하세요"},
    ]


def test_complete_pair_is_preserved():
    prepared = _paired_tool_history([
        _msg("user", "읽어줘"),
        _msg("assistant", tool_calls=[_call("c1", path="a.md")]),
        _msg("tool", "파일 내용", tool_call_id="c1"),
        _msg("assistant", "읽었습니다"),
    ])
    assert _roles(prepared) == ["user", "assistant", "tool", "assistant"]
    assert prepared[1]["tool_calls"][0]["id"] == "c1"
    assert prepared[2]["tool_call_id"] == "c1"


def test_call_without_a_result_is_stripped():
    """중단된 배치 — 호출 3개 중 1개만 실행됐다."""
    prepared = _paired_tool_history([
        _msg("assistant", "", tool_calls=[_call("c1"), _call("c2"), _call("c3")]),
        _msg("tool", "1번 결과", tool_call_id="c1"),
    ])
    assert _roles(prepared) == ["assistant", "tool"]
    ids = [c["id"] for c in prepared[0]["tool_calls"]]
    assert ids == ["c1"], f"결과 없는 호출이 남았다: {ids}"


def test_assistant_with_no_results_keeps_only_its_text():
    prepared = _paired_tool_history([
        _msg("assistant", "시도했습니다", tool_calls=[_call("c1"), _call("c2")]),
        _msg("user", "계속해줘"),
    ])
    assert _roles(prepared) == ["assistant", "user"]
    assert "tool_calls" not in prepared[0]


def test_assistant_with_no_results_and_no_text_is_dropped_entirely():
    prepared = _paired_tool_history([
        _msg("assistant", "", tool_calls=[_call("c1")]),
        _msg("user", "계속해줘"),
    ])
    assert _roles(prepared) == ["user"]


def test_orphan_tool_message_is_dropped():
    """짝이 되는 호출 없이 떠 있는 결과는 공급자가 거부한다."""
    prepared = _paired_tool_history([
        _msg("user", "안녕"),
        _msg("tool", "어디서 왔는지 모를 결과", tool_call_id="ghost"),
        _msg("assistant", "네"),
    ])
    assert _roles(prepared) == ["user", "assistant"]


def test_tool_result_without_an_id_cannot_be_paired():
    """ID가 없으면 어느 호출의 결과인지 알 수 없다 — 짝으로 인정하지 않는다."""
    prepared = _paired_tool_history([
        _msg("assistant", "", tool_calls=[_call("c1")]),
        _msg("tool", "결과", tool_call_id=None),
    ])
    assert prepared == []


def test_multiple_turns_each_keep_their_own_pairs():
    prepared = _paired_tool_history([
        _msg("user", "1번"),
        _msg("assistant", "", tool_calls=[_call("a1"), _call("a2")]),
        _msg("tool", "a1 결과", tool_call_id="a1"),
        _msg("user", "2번"),                                   # a2 결과 없음
        _msg("assistant", "", tool_calls=[_call("b1")]),
        _msg("tool", "b1 결과", tool_call_id="b1"),
    ])
    assert _roles(prepared) == ["user", "assistant", "tool", "user", "assistant", "tool"]
    assert [c["id"] for c in prepared[1]["tool_calls"]] == ["a1"]
    assert [c["id"] for c in prepared[4]["tool_calls"]] == ["b1"]


def test_every_call_in_the_output_has_a_matching_result():
    """불변식 자체를 검사한다 — 어떤 입력이 와도 출력은 완전한 짝이다."""
    prepared = _paired_tool_history([
        _msg("assistant", "", tool_calls=[_call("x1"), _call("x2"), _call("x3")]),
        _msg("tool", "r2", tool_call_id="x2"),
        _msg("tool", "r9", tool_call_id="x9"),   # 대응 호출 없음
        _msg("user", "계속"),
    ])
    for index, message in enumerate(prepared):
        if message["role"] != "assistant" or "tool_calls" not in message:
            continue
        expected = [str(c["id"]) for c in message["tool_calls"]]
        following = []
        scan = index + 1
        while scan < len(prepared) and prepared[scan]["role"] == "tool":
            following.append(prepared[scan]["tool_call_id"])
            scan += 1
        assert expected == following, f"짝이 깨졌다: 호출 {expected} vs 결과 {following}"
