# -*- coding: utf-8 -*-
"""정체(무한 루프) 감지 계약.

기존 감지는 **연속 동일** 호출만 잡았다:

    sig = f"{name}:{args}"
    if sig == last_call_sig: repeat_count += 1
    else:                    repeat_count, last_call_sig = 0, sig

그래서 A→B→A→B→… 교대 루프는 매번 서명이 달라져 카운터가 0으로 리셋되고 **영원히
걸리지 않는다**. 작은 로컬 모델에서 가장 흔한 퇴행 형태가 정확히 이 모양이다
(고치고 검증하고 같은 걸 다시 고치는 왕복). 남는 안전선은 도구 호출 총량 상한(128)과
MAX_STEPS(300)뿐이라, 사용자는 몇 분간 같은 두 동작이 반복되는 걸 지켜봐야 했다.

`IDENTICAL_TOOL_BATCH_LIMIT`도 `len(substantive_batch) > 1` 조건이 붙어 있어
단일 도구 묶음의 반복에는 아예 적용되지 않는다.

여기서 고정하는 계약:
  - 최근 호출 창의 **동작 다양성**이 임계 이하로 떨어지면 정체로 보고 멈춘다.
  - 정상 진행(매번 다른 대상/다른 내용)은 걸리지 않는다.
"""
from __future__ import annotations

import agent
from conftest import FakeChat, types


def _tool_results(events):
    return [event for event in events if event.get("type") == "tool_result"]


# 정체로 멈췄음을 뜻하는 사유들. 한국어 안내문을 훑어보던 것을 이걸로 바꿨다 —
# 문구를 다듬을 때마다 이 테스트가 깨졌고(실제로 깨졌다), 문구는 계약이 아니다.
_STALL_REASONS = {"stall", "repeat"}


def _stall_notice(events) -> str | None:
    """정체로 멈췄으면 사용자에게 보인 안내문을 돌려준다(아니면 None).

    판정은 run_limit 이벤트로 하고, 안내문은 '사람에게도 알렸는지' 확인용으로 함께 본다.
    둘 중 하나라도 빠지면 사용자는 왜 멈췄는지 모른 채 화면만 보게 된다.
    """
    if not any(
        event.get("type") == "run_limit" and event.get("reason") in _STALL_REASONS
        for event in events
    ):
        return None
    for event in events:
        if event.get("type") == "notice" and "멈췄습니다" in event.get("text", ""):
            return event["text"]
    raise AssertionError("정체로 멈췄는데 사람에게 보이는 안내가 없다")


def test_alternating_two_call_loop_is_stopped(env):
    """A→B→A→B… 교대 루프를 잡는다. 예전에는 총량 상한(128)까지 갔다."""
    (env.ws / "a.md").write_text("내용", encoding="utf-8")
    script = []
    for _ in range(40):
        script.append({"calls": [("read_file", {"path": "a.md"})]})
        script.append({"calls": [("list_dir", {"path": "."})]})

    events = env.run(FakeChat(script), approval_mode="auto")

    assert types(events)[-1] == "done"
    assert _stall_notice(events), f"정체 안내가 없다: {types(events)[-8:]}"
    assert len(_tool_results(events)) < 20, (
        f"교대 루프가 {len(_tool_results(events))}회까지 돌았다 — 조기에 멈추지 않았다"
    )


def test_three_way_rotation_is_also_stopped(env):
    """A→B→C→A→B→C… 도 같은 퇴행이다. 두 개짜리만 잡으면 곧 세 개짜리로 옮겨간다."""
    (env.ws / "a.md").write_text("내용", encoding="utf-8")
    (env.ws / "b.md").write_text("내용", encoding="utf-8")
    script = []
    for _ in range(30):
        script.append({"calls": [("read_file", {"path": "a.md"})]})
        script.append({"calls": [("read_file", {"path": "b.md"})]})
        script.append({"calls": [("list_dir", {"path": "."})]})

    events = env.run(FakeChat(script), approval_mode="auto")

    assert types(events)[-1] == "done"
    assert _stall_notice(events)
    assert len(_tool_results(events)) < 24


def test_genuine_progress_is_not_flagged(env):
    """매번 다른 대상을 다루는 정상 진행은 걸리지 않는다(오탐 방지)."""
    for index in range(12):
        (env.ws / f"doc{index}.md").write_text(f"문서 {index}", encoding="utf-8")

    script = [
        {"calls": [("read_file", {"path": f"doc{index}.md"})]} for index in range(12)
    ] + [{"content": "12개 문서를 모두 읽었습니다."}]

    events = env.run(FakeChat(script), approval_mode="auto")

    assert types(events)[-1] == "done"
    assert _stall_notice(events) is None, "정상 진행이 정체로 오판됐다"
    assert len(_tool_results(events)) == 12, "정상 진행이 중간에 잘렸다"


def test_iterative_fix_and_validate_is_not_flagged(env):
    """내용을 바꿔 가며 같은 파일을 반복 검증하는 흐름은 정상이다.

    이게 오탐 경계선이다. 확인 도구의 서명은 매번 같지만 `write_file` 내용이 매번
    달라지므로 '동작 다양성'은 유지된다 — 진짜 퇴행은 양쪽이 다 같을 때다.

    이 테스트는 `IDENTICAL_TOOL_BATCH_LIMIT`의 `len(substantive_batch) > 1` 조건을
    지키는 역할도 한다. 적대 검토는 그 조건을 "단일 도구 묶음에 적용되지 않으니 제거하라"고
    제안했지만, 제거하면 여기 있는 확인 호출이 **3회째에 중단된다**(같은 인자 = 같은
    지문). 반복 수정-확인은 정상 흐름이므로 그 제안은 채택하지 않았다. 단일 도구의
    반복은 연속 검사(STALL_REPEAT)와 다양성 창이 이미 덮는다.
    """
    script = []
    for index in range(6):
        script.append({"calls": [("write_file", {"path": "page.md", "content": f"버전 {index}"})]})
        script.append({"calls": [("read_file", {"path": "page.md"})]})
    script.append({"content": "반복 수정 완료."})

    events = env.run(FakeChat(script), approval_mode="auto")

    assert types(events)[-1] == "done"
    assert _stall_notice(events) is None, "정상적인 수정-확인 반복이 정체로 오판됐다"


def test_consecutive_identical_calls_still_stop(env):
    """기존 계약 유지 — 완전히 동일한 호출의 연속 반복은 그대로 잡는다."""
    (env.ws / "a.md").write_text("내용", encoding="utf-8")
    events = env.run(
        FakeChat([{"calls": [("read_file", {"path": "a.md"})]}] * 30),
        approval_mode="auto",
    )

    assert types(events)[-1] == "done"
    assert _stall_notice(events)
    assert len(_tool_results(events)) <= agent.STALL_REPEAT + 2
