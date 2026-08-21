# -*- coding: utf-8 -*-
"""안전 한도로 멈춘 런은 '실제로 한 일'을 다음 런에 넘긴다.

하네스는 안전 한도로 멈출 때 이렇게 안내한다:

    "여기까지 한 내용은 유지됩니다 — 이어서 계속하려면 '계속해줘'라고 해주세요."

그런데 렌더러는 런 경계에서 **마지막 assistant 텍스트 하나만** 이어붙인다
(AgentView.tsx). 도구 실행 기록은 전부 사라진다. 즉 안내가 사실이 아니었고,
'계속해줘'는 백지에서 다시 시작했다.

여기서 고정하는 계약: 안전 한도로 멈춘 런은 `run_summary` 이벤트로 **하네스가 직접
관측한 도구 실행 기록**을 내보낸다.

`content`가 아니라 별도 이벤트인 이유가 중요하다. 모델의 서술과 실행 사실을 섞으면
"했다고 말했지만 실제로는 안 한 것"이 그대로 다음 런으로 넘어간다 — 이미지 생성에서
이미 겪은 부류의 오염이다. 요약은 tool_result가 실제로 나온 것만 담는다.
"""
from __future__ import annotations

import agent
import agent_runner
from conftest import FakeChat, types


def _summaries(events) -> list[str]:
    return [event["text"] for event in events if event.get("type") == "run_summary"]


# ── 요약 문자열 자체의 계약 ─────────────────────────────────────────────


def test_summary_is_empty_without_any_execution():
    assert agent_runner._run_progress_summary([]) == ""


def test_summary_lists_tools_targets_and_outcome():
    text = agent_runner._run_progress_summary([
        {"name": "write_file", "target": "a.md", "ok": True},
        {"name": "run_web", "target": "page.html", "ok": False},
    ])
    assert "write_file a.md" in text and "성공" in text
    assert "run_web page.html" in text and "실패" in text


def test_summary_is_bounded_and_says_what_it_dropped():
    records = [{"name": "read_file", "target": f"{i}.md", "ok": True} for i in range(60)]
    text = agent_runner._run_progress_summary(records)
    assert text.count("\n- ") <= agent_runner.MAX_SUMMARIZED_TOOL_RECORDS
    assert "생략" in text, "잘라내고도 말하지 않으면 전부 한 것처럼 읽힌다"


def test_target_picks_one_recognizable_value():
    pick = agent_runner._tool_record_target
    assert pick({"path": "a.md"}) == "a.md"
    assert pick({"command": "npm test"}) == "npm test"
    assert pick({}) == ""
    assert pick("not-a-dict") == ""


# ── 런 통합 ────────────────────────────────────────────────────────────


def test_stalled_run_emits_what_it_actually_did(env):
    """교대 루프로 멈춘 런이 실행 기록을 남긴다."""
    (env.ws / "a.md").write_text("내용", encoding="utf-8")
    script = []
    for _ in range(30):
        script.append({"calls": [("read_file", {"path": "a.md"})]})
        script.append({"calls": [("list_dir", {"path": "."})]})

    events = env.run(FakeChat(script), approval_mode="auto")

    assert types(events)[-1] == "done"
    summaries = _summaries(events)
    assert summaries, "안전 한도로 멈췄는데 실행 기록을 넘기지 않았다"
    assert "read_file" in summaries[-1] and "list_dir" in summaries[-1]


def test_normal_completion_does_not_emit_a_summary(env):
    """정상 종료에는 모델 자신의 답변이 있다 — 요약을 덧붙이면 중복이다."""
    (env.ws / "a.md").write_text("내용", encoding="utf-8")
    events = env.run(
        FakeChat([
            {"calls": [("read_file", {"path": "a.md"})]},
            {"content": "확인했습니다."},
        ]),
        approval_mode="auto",
    )
    assert types(events)[-1] == "done"
    assert _summaries(events) == []


def test_summary_only_counts_tools_that_actually_ran(env):
    """모델이 부르기만 하고 실행되지 않은 도구는 기록에 없다.

    실행 사실 기록이 모델의 의도를 반영하면 '했다고 말했지만 안 한 것'이 그대로
    다음 런으로 넘어간다.
    """
    (env.ws / "a.md").write_text("내용", encoding="utf-8")
    script = [{"calls": [("nonexistent_tool", {})]}] * 30  # 전부 차단됨
    events = env.run(FakeChat(script), approval_mode="auto")

    for text in _summaries(events):
        assert "nonexistent_tool" not in text


def test_meta_tools_are_not_reported_as_work(env):
    """update_plan은 계획 갱신이지 수행한 작업이 아니다."""
    plan = {"steps": [{"title": "t", "content": "t", "status": "pending"}]}
    script = [{"calls": [("update_plan", plan)]}] * 30
    events = env.run(FakeChat(script), approval_mode="auto")
    for text in _summaries(events):
        assert "update_plan" not in text
