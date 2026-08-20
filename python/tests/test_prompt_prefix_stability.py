# -*- coding: utf-8 -*-
"""KV 프리픽스 안정성 계약.

`agent_runner`는 시스템 메시지를 런 내내 바이트 고정해 Ollama가 KV 캐시를 재사용하게
한다(턴당 ~1.5s 재처리 → 15~60ms). 그 목표는 **과거 대화 메시지도 변하지 않을 때만**
성립한다. 프롬프트 앞부분이 한 바이트라도 달라지면 그 지점부터 캐시가 깨진다.

예전 `compact_convo`는 매 턴 캡을 다시 계산했다 — `recent_tool_cap`이 `tool_count`에
의존하고, `cap = ... if i >= len(convo) - 6` 이 슬라이딩 윈도였다. 실측하면 같은 도구
결과 하나가 한 런 안에서

    2296 → 901 → 644 → 501 → 409 → 346 → 300 → 265 → 237

로 아홉 번 다시 잘렸다. 즉 KV 캐시는 시스템 메시지 뒤로 **한 번도** 재사용되지 못했다.

여기서 고정하는 계약은 세 가지다.
  1. 도구 결과는 기록 시점에 한 번 잘리고, 그 뒤 어떤 턴에서도 바이트가 변하지 않는다.
  2. 컨텍스트가 포화되지 않은 동안에는 앞 턴 프롬프트가 뒤 턴 프롬프트의 바이트 동일 접두사다.
  3. 포화되면 **메시지를 통째로 버린다** — 남은 메시지를 다시 자르지는 않는다.

3번을 명시하는 이유: 예산보다 대화가 크면 무언가는 반드시 사라져야 하고, 그 상황에서
완전한 접두사 보존은 원리적으로 불가능하다. 보장할 수 있는 것은 "남은 것은 그대로"다.
"""
from __future__ import annotations

import json

import pytest

import agent
from conftest import FakeChat, types

BIG = "가나다라마바사아자차카타파하 " * 2000  # 캡보다 확실히 큰 도구 결과
ROOMY_CONTEXT = 131072  # 축출이 일어나지 않는 넉넉한 창


def _write_big_files(workspace, count):
    for index in range(count):
        (workspace / f"doc{index}.md").write_text(f"# 문서 {index}\n{BIG}", encoding="utf-8")


def _messages_of(payload) -> list[dict]:
    return payload.get("messages") or [] if isinstance(payload, dict) else []


def _read_docs_script(count):
    return [
        {"calls": [("read_file", {"path": f"doc{index}.md"})]} for index in range(count)
    ] + [{"content": "정리 완료."}]


def _first_divergence(earlier: list[dict], later: list[dict]) -> str | None:
    for index, previous in enumerate(earlier):
        if index >= len(later):
            return f"메시지 {index}: 뒤 턴에서 사라짐 (role={previous.get('role')})"
        if previous != later[index]:
            before = json.dumps(previous, ensure_ascii=False, sort_keys=True)
            after = json.dumps(later[index], ensure_ascii=False, sort_keys=True)
            return (
                f"메시지 {index} (role={previous.get('role')})가 바뀌었다: "
                f"{len(before)}자 → {len(after)}자"
            )
    return None


def _tool_contents_by_document(payloads) -> dict[str, set[str]]:
    """문서별로 프롬프트에 실린 도구 결과 본문을 모은다.

    tool_call_id는 로컬 경로에서 빈 문자열이라 키로 못 쓴다. 각 결과가 어느 문서인지는
    본문 첫 줄('# 문서 N')이 알려준다.
    """
    grouped: dict[str, set[str]] = {}
    for messages in payloads:
        for message in messages:
            if message.get("role") != "tool":
                continue
            content = str(message.get("content") or "")
            identity = content.split("\n", 1)[0][:40]
            grouped.setdefault(identity, set()).add(content)
    return grouped


# ── 1. 기록 시점 1회 절단 ────────────────────────────────────────────────


def test_tool_result_bytes_never_change_after_recording(env):
    """포화 여부와 무관하게, 한 번 기록된 도구 결과는 다시 잘리지 않는다."""
    _write_big_files(env.ws, 9)
    chat = FakeChat(_read_docs_script(9))
    env.run(chat, approval_mode="auto")

    grouped = _tool_contents_by_document([_messages_of(p) for p in chat.payloads])
    drifted = {key: values for key, values in grouped.items() if len(values) > 1}
    assert not drifted, (
        "같은 도구 결과가 턴마다 다른 바이트로 실렸다: "
        + ", ".join(
            f"{key!r}={sorted(len(v) for v in values)}"
            for key, values in list(drifted.items())[:3]
        )
    )


def test_tool_result_cap_does_not_depend_on_conversation_length():
    """캡은 컨텍스트 설정만의 함수다 — 대화가 길어져도 변하지 않는다."""
    cap = agent.tool_result_cap(16384, 5408)
    assert cap == agent.tool_result_cap(16384, 5408)
    assert cap > 0
    # 창이 커지면 캡도 커진다(상한까지).
    assert agent.tool_result_cap(131072, 5408) >= cap


def test_recorded_tool_result_is_bounded():
    """안정화가 '무제한 허용'을 뜻하지 않는다."""
    convo = agent.ModelConversation(tool_result_cap=500)
    convo.append({"role": "tool", "tool_call_id": "x", "content": "가" * 10_000})
    content = convo[0]["content"]
    assert len(content) < 10_000
    assert content.endswith(agent.TOOL_RESULT_TRUNCATION_LABEL)


def test_non_tool_messages_are_recorded_verbatim():
    """자르는 대상은 도구 결과뿐이다 — 사용자 요청을 건드리면 안 된다."""
    convo = agent.ModelConversation(tool_result_cap=10)
    long_request = "요청 " * 5_000
    convo.append({"role": "user", "content": long_request})
    assert convo[0]["content"] == long_request


# ── 2. 비포화 구간의 접두사 안정성 ──────────────────────────────────────


def test_prefix_is_byte_stable_when_context_is_not_saturated(env):
    """축출이 필요 없는 동안에는 앞 턴 프롬프트가 뒤 턴의 바이트 동일 접두사다.

    이게 KV 캐시가 실제로 재사용되는 구간이고, 예전 코드가 깨뜨리던 바로 그 구간이다.
    """
    _write_big_files(env.ws, 9)
    chat = FakeChat(_read_docs_script(9))
    events = env.run(chat, approval_mode="auto", context_length=ROOMY_CONTEXT)
    assert types(events)[-1] == "done"

    payloads = [_messages_of(p) for p in chat.payloads]
    assert len(payloads) >= 8, f"턴이 충분히 돌지 않았다: {len(payloads)}"

    failures = []
    for turn in range(len(payloads) - 1):
        divergence = _first_divergence(payloads[turn], payloads[turn + 1])
        if divergence:
            failures.append(f"[턴 {turn} → {turn + 1}] {divergence}")

    assert not failures, (
        "프롬프트 접두사가 턴 사이에 변했다 — KV 캐시가 이 지점부터 깨진다.\n"
        + "\n".join(failures[:4])
    )


# ── 3. 포화 시에는 통째로 버린다 ────────────────────────────────────────


def test_saturated_context_drops_whole_messages_without_rewriting_the_rest(env):
    """예산을 넘으면 오래된 메시지가 사라진다. 남은 메시지는 원래 바이트 그대로다."""
    _write_big_files(env.ws, 9)
    chat = FakeChat(_read_docs_script(9))
    env.run(chat, approval_mode="auto", context_length=16384)

    payloads = [_messages_of(p) for p in chat.payloads]
    assert any(
        any(m.get("content") == agent.OLDER_TURNS_OMITTED_MARKER for m in messages)
        for messages in payloads
    ), "포화 시나리오인데 축출이 일어나지 않았다 — 테스트 전제가 깨졌다"

    # 축출이 일어나도 도구 결과 본문은 재작성되지 않는다(계약 1과 동일한 보장).
    grouped = _tool_contents_by_document(payloads)
    assert not {k: v for k, v in grouped.items() if len(v) > 1}


def test_drop_watermark_never_moves_backwards():
    """한 번 버린 구간은 되살아나지 않는다 — 되살리면 프리픽스가 다시 흔들린다."""
    convo = agent.ModelConversation(tool_result_cap=200)
    for index in range(40):
        convo.append({"role": "user", "content": f"요청 {index} " + "가" * 500})

    watermarks = []
    for _ in range(5):
        agent.compact_convo(convo, 4096, 0, output_reserve_tokens=1024)
        watermarks.append(convo.dropped_before)

    assert watermarks == sorted(watermarks), f"워터마크가 되돌아갔다: {watermarks}"
    assert watermarks[0] > 0, "이 시나리오는 축출이 일어나야 한다"


def test_evicted_tail_never_starts_with_an_orphan_tool_result():
    """짝이 되는 assistant tool_calls 없이 tool 메시지로 시작하면 공급자가 거부한다."""
    convo = agent.ModelConversation(tool_result_cap=200)
    for index in range(30):
        convo.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": f"{index}.md"}}}],
        })
        convo.append({"role": "tool", "tool_call_id": "", "content": "결과 " + "가" * 400})

    compacted = agent.compact_convo(convo, 4096, 0, output_reserve_tokens=1024)
    body = [m for m in compacted if m.get("content") != agent.OLDER_TURNS_OMITTED_MARKER]
    assert body, "전부 버려졌다"
    assert body[0].get("role") != "tool", f"고아 도구 결과로 시작한다: {body[0]}"
