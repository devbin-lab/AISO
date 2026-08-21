# -*- coding: utf-8 -*-
"""run_agent 이벤트 스트림 특성화 테스트 — 현재 동작을 고정해 리팩터 회귀를 잡는다.

각 테스트는 mock된 _chat_turn(FakeChat)으로 특정 시나리오를 구동하고,
이벤트 타입 순서와 핵심 필드를 검증한다.
"""
from __future__ import annotations

import pytest

import agent
from conftest import FakeChat, load_crash, parse_error, types


# ── 정상 완료 ───────────────────────────────────────────────
def test_normal_completion(env):
    evs = env.run(FakeChat([
        {"calls": [("list_dir", {"path": "."})]},
        {"content": "끝났습니다."},
    ]))
    t = types(evs)
    assert "tool_call" in t and "tool_result" in t and "done" in t
    assert "error" not in t
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["ok"] is True and "rejected" not in tr


# ── 설정 기반 도구 ON/OFF 실행 경계 ─────────────────────────
def test_programming_tools_are_hidden_by_default_and_prompt_matches_policy(env):
    fake = FakeChat([{"content": "분석만 수행했습니다."}])
    events = env.run(fake)

    tool_names = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert {
        "write_code_file", "edit_code_file", "multi_edit_code_file",
        "run_web", "run_code", "run_command",
    }.isdisjoint(tool_names)
    system_prompt = fake.payloads[0]["messages"][0]["content"]
    assert "Project code authoring and editing are disabled" in system_prompt
    assert "Code, command, and web execution validation are disabled" in system_prompt
    assert types(events)[-1] == "done"


def test_prompt_describes_only_individually_exposed_file_tools(env):
    fake = FakeChat([{"content": "읽었습니다."}])
    env.run(fake, enabled_tools=["read_file"], approval_mode="auto")

    system_prompt = fake.payloads[0]["messages"][0]["content"]
    assert "read_file" in system_prompt
    for hidden in ("list_tree", "list_dir", "grep", "glob", "move", "web_search", "web_fetch"):
        assert hidden not in system_prompt


def test_no_workspace_prompt_uses_actual_scope_not_saved_programming_toggle(env):
    fake = FakeChat([{"content": "작업 폴더가 필요합니다."}])
    env.run(
        fake,
        workspace="",
        enabled_tools=["write_code_file", "run_code"],
        approval_mode="auto",
    )

    assert fake.payloads[0]["tools"] == []
    system_prompt = fake.payloads[0]["messages"][0]["content"]
    assert "Project code authoring and editing are disabled" in system_prompt
    assert "The user enabled project code authoring and editing" not in system_prompt
    assert "write_code_file" not in system_prompt
    assert "run_code" not in system_prompt


def test_enabled_programming_tool_is_exposed_and_can_write_code(env):
    enabled = [
        "write_code_file", "edit_code_file", "multi_edit_code_file",
        "run_web", "run_code", "run_command",
    ]
    fake = FakeChat([
        {"calls": [("write_code_file", {"path": "src/app.py", "content": "print('ok')\n"})]},
        {"content": "작성과 검증을 마쳤습니다."},
    ])
    events = env.run(fake, enabled_tools=enabled, approval_mode="auto")

    tool_names = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert set(enabled) == tool_names
    system_prompt = fake.payloads[0]["messages"][0]["content"]
    assert "The user enabled project code authoring and editing" in system_prompt
    assert "write_code_file" in system_prompt and "run_code" in system_prompt
    assert (env.ws / "src" / "app.py").read_text(encoding="utf-8") == "print('ok')\n"
    result = next(event for event in events if event["type"] == "tool_result")
    assert result["ok"] is True


def test_forced_disabled_programming_call_is_blocked_before_execution(env):
    fake = FakeChat([{
        "calls": [("write_code_file", {"path": "blocked.py", "content": "print('no')\n"})],
    }])
    events = env.run(fake, enabled_tools=["read_file"], approval_mode="auto")

    assert not (env.ws / "blocked.py").exists()
    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    error = next(event["error"] for event in events if event["type"] == "error")
    assert "설정에서 꺼진 도구" in error and "write_code_file" in error
    assert types(events)[-1] == "done"


def test_mixed_allowed_and_disabled_batch_executes_nothing(env):
    fake = FakeChat([{
        "calls": [
            ("write_file", {"path": "allowed.md", "content": "must not be written"}),
            ("write_code_file", {"path": "disabled.py", "content": "print('no')\n"}),
        ],
    }])
    events = env.run(fake, enabled_tools=["write_file"], approval_mode="auto")

    assert not (env.ws / "allowed.md").exists()
    assert not (env.ws / "disabled.py").exists()
    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    assert any("이번 도구 호출 묶음을 실행하지 않았습니다" in event.get("error", "") for event in events)


def test_disabling_run_skill_hides_dynamic_skills_and_blocks_name_call(env, monkeypatch, tmp_path):
    marker = tmp_path / "skill-ran.txt"
    listed = False

    def fake_list_skills():
        nonlocal listed
        listed = True
        return [{"name": "hidden_skill", "description": "should remain hidden"}]

    async def forbidden_run_skill(**_kwargs):
        marker.write_text("ran", encoding="utf-8")
        return "unexpected"

    monkeypatch.setattr(agent, "list_skills", fake_list_skills)
    monkeypatch.setattr(agent, "run_skill", forbidden_run_skill)
    fake = FakeChat([{"calls": [("hidden_skill", {})]}])
    events = env.run(fake, enabled_tools=["create_skill"], approval_mode="auto")

    tool_names = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert "run_skill" not in tool_names and "hidden_skill" not in tool_names
    system_prompt = fake.payloads[0]["messages"][0]["content"]
    assert "Skill execution is disabled" in system_prompt
    assert "Run a newly created skill" not in system_prompt
    assert listed is False
    assert not marker.exists()
    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    assert any("현재 실행 범위 밖" in event.get("error", "") for event in events)


# ── 작업 폴더 없이 실행: 노출 범위 밖 호출은 묶음 전체 차단 ─────
def test_no_workspace_rejects_mixed_exposed_and_hidden_tool_batch(env, monkeypatch, tmp_path):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    fc = FakeChat([
        {"calls": [
            ("write_file", {"path": "a.md", "content": "hi"}),
            ("create_skill", {"name": "greet", "description": "인사", "code": "print('OK')\n"}),
        ]},
        {"content": "완료"},
    ])
    evs = env.drive(fc, approve=True, workspace="", approval_mode="auto")
    # (1) 모델에 넘긴 도구 목록에서 로컬 도구가 빠져 있다(웹·스킬만)
    tool_names = {t["function"]["name"] for t in fc.payloads[0]["tools"]}
    assert "write_file" not in tool_names and "run_command" not in tool_names
    assert "create_skill" in tool_names and "web_search" in tool_names
    # (2) 목록에서 빠진 로컬 도구를 끼워 호출하면 노출된 스킬까지 포함해 아무것도 실행하지 않는다.
    assert not any(e.get("type") in {"tool_call", "tool_result"} for e in evs)
    assert not (tmp_path / "greet" / "main.py").exists()
    assert any("write_file" in e.get("error", "") for e in evs)
    assert types(evs)[-1] == "done"


def test_no_workspace_unknown_tool_gets_one_recovery_turn(env, monkeypatch, tmp_path):
    """지어낸 도구 이름은 실행하지 않되, 런을 끝내지 않고 한 번 교정 기회를 준다.

    계약 변경(의도): 예전에는 존재하지 않는 도구 이름 하나로 런이 즉시 종료됐다.
    도구명 환각은 작은 로컬 모델의 최빈 실패인데 거기에 최고형이 걸려 있었던 셈이다.
    이제 '아직 배치가 하나도 실행되지 않았다'는 기존 안전 조건 위에서, 실제 도구
    목록을 알려 주는 교정 턴을 한 번 준다. route_recovery와 같은 패턴이다.

    바뀌지 않은 것: 실행 이벤트는 여전히 하나도 나가지 않는다.
    """
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))  # 스킬 없음
    fc = FakeChat([
        {"calls": [("nonexistent_tool", {})]},
        {"content": "완료"},
    ])
    evs = env.drive(fc, approve=True, workspace="", approval_mode="auto")

    assert not any(e.get("type") in {"tool_call", "tool_result"} for e in evs)
    assert not any(e.get("type") == "error" for e in evs), "교정 가능한 실수로 런이 죽었다"
    notice = next(e["text"] for e in evs if e.get("type") == "notice")
    assert "존재하지 않는 도구" in notice
    assert types(evs)[-1] == "done"


def test_repeated_unknown_tool_names_still_end_the_run(env, monkeypatch, tmp_path):
    """교정은 한 번뿐이다 — 계속 지어내면 턴을 무한히 소비하지 않고 끝낸다."""
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    fc = FakeChat([{"calls": [("nonexistent_tool", {})]}])  # 마지막 스펙이 반복된다
    evs = env.drive(fc, approve=True, workspace="", approval_mode="auto")

    assert not any(e.get("type") in {"tool_call", "tool_result"} for e in evs)
    error = next(e["error"] for e in evs if e.get("type") == "error")
    assert "현재 실행 범위 밖" in error and "nonexistent_tool" in error
    assert types(evs)[-1] == "done"


def test_real_but_out_of_scope_tool_is_not_recovered(env, monkeypatch, tmp_path):
    """실존하지만 현재 범위 밖인 도구는 교정 대상이 아니다.

    그건 모델의 실수가 아니라 정책 차단이고(예: 기존 웹 산출물 검증 중 수정 도구),
    차단 자체가 목적이다. 환각 교정이 이 보호를 우회하면 안 된다.
    """
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    # write_file은 실존하는 도구지만 무폴더 실행에서는 노출되지 않는다.
    fc = FakeChat([
        {"calls": [("write_file", {"path": "a.md", "content": "x"})]},
        {"content": "완료"},
    ])
    evs = env.drive(fc, approve=True, workspace="", approval_mode="auto")

    assert not any(e.get("type") in {"tool_call", "tool_result"} for e in evs)
    assert any(e.get("type") == "error" for e in evs), "정책 차단이 교정으로 새어나갔다"
    assert types(evs)[-1] == "done"


def test_created_skill_callable_by_name_without_workspace(env, monkeypatch, tmp_path):
    """사용자 시나리오 회귀: 다른 방에서 만든 스킬(get_current_time)을 무폴더 방에서 '이름 그대로'
    부르면 작업 폴더 없이도 실제로 실행된다(예전엔 '작업 폴더 필요'로 실패했음)."""
    import asyncio as _aio
    import runskill
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    _aio.run(runskill.create_skill(name="get_current_time", description="현재 시각", code="print('21:21')\n"))
    fc = FakeChat([
        {"calls": [("get_current_time", {})]},  # 이름으로 직접 호출(모델의 자연스러운 방식)
        {"content": "지금은 21시 21분입니다"},
    ])
    evs = env.drive(fc, approve=True, workspace="", approval_mode="auto")
    # (1) 스킬이 도구 목록에 노출됐다
    tool_names = {t["function"]["name"] for t in fc.payloads[0]["tools"]}
    assert "get_current_time" in tool_names
    # (2) 이름 호출 → 작업 폴더 없이도 실제 실행 성공
    id2name = {e["id"]: e["name"] for e in evs if e.get("type") == "tool_call"}
    results = {id2name[e["id"]]: e for e in evs if e.get("type") == "tool_result"}
    assert results["get_current_time"]["ok"] is True
    assert "21:21" in results["get_current_time"]["output"]


def test_skill_by_name_requires_approval_in_read_mode(env, monkeypatch, tmp_path):
    """이름으로 부른 스킬도 run_skill과 같은 승인 등급 — 읽기 모드에서 승인 필요(거부 시 실행 안 됨)."""
    import asyncio as _aio
    import runskill
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    _aio.run(runskill.create_skill(name="ring", description="알람", code="print('ring')\n"))
    fc = FakeChat([{"calls": [("ring", {})]}, {"content": "done"}])
    evs = env.drive(fc, approve=False, workspace="", approval_mode="read")
    assert any(e.get("type") == "approval_request" and e.get("name") == "ring" for e in evs)
    id2name = {e["id"]: e["name"] for e in evs if e.get("type") == "tool_call"}
    results = {id2name[e["id"]]: e for e in evs if e.get("type") == "tool_result"}
    assert results["ring"]["ok"] is False


# ── 자동 이어가기(넛지): 툴 없이 멈추려는데 계획 미완 ───────────
def test_no_tool_nudge_continues(env):
    evs = env.run(FakeChat([
        {"calls": [("update_plan", {"steps": [{"content": "a", "status": "pending"}]})]},
        {"content": "일단 여기까지."},  # 툴 없음 + 계획 미완 → 넛지
        {"calls": [("update_plan", {"steps": [{"content": "a", "status": "completed"}]})]},
        {"content": "완료."},  # 계획 완료 → 정상 done
    ]))
    t = types(evs)
    assert any("미완 단계가 남아" in e.get("text", "") for e in evs if e["type"] == "notice")
    assert t[-1] == "done" and "error" not in t


# ── 정체(spin): update_plan만 반복 → 중단 ───────────────────
def test_meta_spin_stops(env):
    # update_plan만 4번 반복 → SPIN_LIMIT(4) 도달 → 중단
    evs = env.run(FakeChat([
        {"calls": [("update_plan", {"steps": [{"content": "a", "status": "pending"}]})]},
    ]))
    t = types(evs)
    assert any("실제 작업 없이" in e.get("text", "") for e in evs if e["type"] == "notice")
    assert t[-1] == "done"
    # 무한이 아니라 4턴 만에 멈췄는지
    fake = env.mp  # noqa
    assert t.count("plan") == agent.SPIN_LIMIT  # 4번 계획 갱신 후 중단


# ── 무한루프 감지(stall): 동일 (툴,인자) 반복 ───────────────
def test_stall_identical_calls(env):
    evs = env.run(FakeChat([{"calls": [("list_dir", {"path": "."})]}]))  # 무한 반복 스크립트
    t = types(evs)
    assert any("반복해 멈췄" in e.get("text", "") for e in evs if e["type"] == "notice")
    assert t[-1] == "done"
    # STALL_REPEAT=6 → 6번 실행 후 7번째에서 감지(툴콜은 6번만)
    assert t.count("tool_call") == agent.STALL_REPEAT


def test_stall_near_miss_no_stop(env):
    # 5번 동일 후 다른 호출 → stall 안 걸림
    evs = env.run(FakeChat([
        {"calls": [("list_dir", {"path": "."})]},
        {"calls": [("list_dir", {"path": "."})]},
        {"calls": [("list_dir", {"path": "."})]},
        {"calls": [("list_dir", {"path": "."})]},
        {"calls": [("list_dir", {"path": "."})]},
        {"calls": [("read_file", {"path": "다른.txt"})]},  # 서명 다름 → 리셋
        {"content": "끝."},
    ]))
    assert not any("반복해 멈췄" in e.get("text", "") for e in evs if e["type"] == "notice")
    assert types(evs)[-1] == "done"


# ── 파싱오류 재생성 ─────────────────────────────────────────
def test_parse_retry_recovers(env):
    evs = env.run(FakeChat([
        {"raise": parse_error()},
        {"content": "폴더 구조는 game/ 입니다."},
    ]))
    t = types(evs)
    assert any("다시 생성" in e.get("text", "") for e in evs if e["type"] == "notice")
    assert "content" in t and t[-1] == "done" and "error" not in t


def test_parse_retry_exhausts_cleanly(env):
    evs = env.run(FakeChat([{"raise": parse_error()}]))  # 계속 실패
    errs = [e for e in evs if e["type"] == "error"]
    assert errs and "파싱" in errs[-1]["error"]
    assert "done" not in types(evs)  # fatal → done 없음


# ── 승인 흐름 ───────────────────────────────────────────────
def test_approval_accept(env):
    (env.ws / "f.txt").write_text("x", encoding="utf-8")
    evs = env.drive(
        FakeChat([{"calls": [("delete_file", {"path": "f.txt"})]}, {"content": "삭제 완료."}]),
        approve=True,
    )
    t = types(evs)
    assert "approval_request" in t
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["ok"] is True and "rejected" not in tr
    assert not (env.ws / "f.txt").exists()  # 실제로 삭제됨(휴지통)


def test_approval_reject(env):
    (env.ws / "f.txt").write_text("x", encoding="utf-8")
    evs = env.drive(
        FakeChat([{"calls": [("delete_file", {"path": "f.txt"})]}, {"content": "취소됨."}]),
        approve=False,
    )
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["ok"] is False and tr.get("rejected") is True
    assert (env.ws / "f.txt").exists()  # 거부 → 파일 유지


def test_approval_timeout_does_not_execute_and_is_not_recorded_as_a_rejection(env):
    """무응답은 실행하지 않되(변경 없음), '사용자가 거부함'으로 기록하지 않는다(변경됨).

    계약 변경(의도): 예전에는 타임아웃도 `rejected: True`였다. 실행 판단으로는 옳지만
    (승인 없이 실행하지 않는다) 그 뒤가 틀렸다 — 사용자는 거부한 적이 없고 자리를
    비웠을 뿐인데, 모델에게 "사용자가 승인하지 않았습니다"라고 전달하고 원장에
    영구 기록했다. 12B 모델은 거부(그 방향을 접는다)와 무응답(다시 물어볼 수 있다)에
    다르게 반응해야 한다.

    바뀌지 않은 것: 도구는 실행되지 않는다.
    """
    env.mp.setattr(agent, "APPROVAL_TIMEOUT", 0.05)
    (env.ws / "f.txt").write_text("x", encoding="utf-8")
    evs = env.run(FakeChat([{"calls": [("delete_file", {"path": "f.txt"})]}, {"content": "?"}]))

    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["ok"] is False
    assert tr.get("expired") is True
    assert tr.get("rejected") is False, "무응답이 사용자 거부로 기록됐다"
    assert (env.ws / "f.txt").exists(), "승인 없이 실행됐다 — 안전 계약이 깨졌다"
    assert "응답 없음" in tr["output"]
    assert any("응답이 없어" in e.get("text", "") for e in evs if e.get("type") == "notice")


def test_explicit_denial_is_still_recorded_as_a_rejection(env):
    """실제 거부는 그대로 거부다 — 무응답과 섞이면 안 된다."""
    (env.ws / "f.txt").write_text("x", encoding="utf-8")
    evs = env.drive(
        FakeChat([{"calls": [("delete_file", {"path": "f.txt"})]}, {"content": "?"}]),
        approve=False,
    )

    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["ok"] is False
    assert tr.get("rejected") is True
    assert tr.get("expired") is False
    assert (env.ws / "f.txt").exists()


def test_auto_executes_delete_without_approval_card(env):
    """The runtime gate must not emit an approval request in auto mode."""
    (env.ws / "f.txt").write_text("x", encoding="utf-8")
    evs = env.run(
        FakeChat([{"calls": [("delete_file", {"path": "f.txt"})]}, {"content": "삭제 완료."}]),
        approval_mode="auto",
    )

    assert not any(event.get("type") == "approval_request" for event in evs)
    assert not (env.ws / "f.txt").exists()
    assert next(event for event in evs if event["type"] == "tool_result")["ok"] is True


# ── 툴 에러는 복구 가능(런 안 죽음) ─────────────────────────
def test_tool_error_recoverable(env):
    evs = env.run(FakeChat([
        {"calls": [("read_file", {"path": "없는파일.txt"})]},
        {"content": "그 파일은 없네요."},
    ]))
    t = types(evs)
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["ok"] is False and "[오류]" in tr["output"] and "rejected" not in tr
    assert t[-1] == "done" and "error" not in t  # fatal 아님


# ── 최대 단계 백스톱 ────────────────────────────────────────
def test_max_steps_backstop(env):
    env.mp.setattr(agent, "MAX_STEPS", 2)
    evs = env.run(FakeChat([{"calls": [("list_dir", {"path": "."})]}]))
    t = types(evs)
    assert any("안전선" in e.get("text", "") for e in evs if e["type"] == "notice")
    assert t[-1] == "done"


# ── run_web 스크린샷은 tool_result 뒤에 ─────────────────────
def test_screenshot_after_tool_result(env):
    import dataclasses

    import toolspec

    async def fake_run_web(root, **args):
        return "리포트", "SHOT64"

    fake_spec = dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web)
    env.mp.setitem(toolspec.REGISTRY, "run_web", fake_spec)
    (env.ws / "x.html").write_text("X", encoding="utf-8")
    evs = env.drive(FakeChat([
        {"calls": [("run_web", {"path": "x.html"})]},
        {"content": "검증 완료."},
    ]), approve=True, messages=[{"role": "user", "content": "verify x.html"}], enabled_tools=["run_web"])
    t = types(evs)
    i_tr = t.index("tool_result")
    i_shot = t.index("screenshot")
    assert i_shot == i_tr + 1  # 스크린샷은 tool_result 바로 뒤
    assert next(e for e in evs if e["type"] == "screenshot")["data"] == "SHOT64"


def test_run_web_validation_failure_marks_tool_result_failed_but_keeps_screenshot(env):
    import dataclasses

    import toolspec

    async def fake_run_web(root, **args):
        return "[WEB_VALIDATION v1]\nstatus=FAIL level=interaction\nsummary=버튼 상태 불일치", "FAILSHOT"

    fake_spec = dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web)
    env.mp.setitem(toolspec.REGISTRY, "run_web", fake_spec)
    (env.ws / "x.html").write_text("X", encoding="utf-8")
    events = env.drive(
        FakeChat([
            {"calls": [("run_web", {"path": "x.html"})]},
            {"content": "실패 원인을 확인했습니다."},
        ]),
        approve=True,
        messages=[{"role": "user", "content": "verify x.html"}],
        enabled_tools=["run_web"],
    )

    result = next(event for event in events if event["type"] == "tool_result")
    assert result["ok"] is False
    assert "status=FAIL" in result["output"]
    assert next(event for event in events if event["type"] == "screenshot")["data"] == "FAILSHOT"


@pytest.mark.parametrize(
    ("user_text", "recent_context", "expected"),
    [
        ("검증 기능 다시 활성화했어. 검증해줘", "", True),
        ("검증 기능 다시 활성화했어. 검증해줘", "HTML 테트리스 게임을 완성했습니다.", True),
        ("수정한 index.html을 검증해줘", "", True),
        ("만들어 둔 HTML을 검증해줘", "", True),
        ("고쳐놓은 HTML 다시 테스트해줘", "", True),
        ("기존 HTML 열어서 버튼이 되는지 확인해줘", "", True),
        ("테트리스가 제대로 동작하는지 확인해줘", "", True),
        ("기존 웹 게임을 점검만 해줘", "", True),
        ("기존 HTML을 QA만 해줘", "", True),
        ("기존 웹 앱을 검토만 해줘", "", True),
        ("기존 웹 앱을 살펴봐줘", "", True),
        ("한번 실행해봐", "HTML 게임을 완성했습니다.", True),
        ("audit the existing web app without changing anything", "", True),
        ("review the existing web app without edits", "", True),
        ("inspect the existing web app without changes", "", True),
        ("do not create a new HTML; just test existing", "", True),
        ("새로 만들지 말고 기존 HTML 검증해줘", "", True),
        ("새로 만들지는 말고 기존 HTML 게임 실행해봐", "", True),
        ("새로 만들진 말고 기존 HTML 게임 실행해봐", "", True),
        ("새로 만들 필요 없이 기존 HTML 게임 실행해봐", "", True),
        ("verify the existing web game", "The HTML game is complete.", True),
        ("계속해줘", "기존 HTML을 검증해줘", True),
        ("keep going", "I will verify the existing web app", True),
        ("please continue", "I was verifying the existing web app and index.html.", True),
        ("continue please", "I was verifying the existing web app and index.html.", True),
        ("continue the validation", "I was verifying the existing web app and index.html.", True),
        ("continue validation", "I was verifying the existing web app and index.html.", True),
        ("continue validating", "I was verifying the existing web app and index.html.", True),
        ("please continue with the validation", "I was verifying the existing web app and index.html.", True),
        ("keep validating it", "I was verifying the existing web app and index.html.", True),
        ("resume validation", "I was verifying the existing web app and index.html.", True),
        ("continue it", "Validation of index.html was interrupted.", True),
        ("continue with it", "Validation of index.html was interrupted.", True),
        ("please proceed with the validation", "Validation of index.html was interrupted.", True),
        ("resume where you left off", "Validation of index.html was interrupted.", True),
        ("go ahead with the validation", "Validation of index.html was interrupted.", True),
        ("finish validating", "Validation of index.html was interrupted.", True),
        ("keep the validation going", "Validation of index.html was interrupted.", True),
        ("continue where you left off", "I was verifying the existing web app and index.html.", True),
        ("keep going please", "I was verifying the existing web app and index.html.", True),
        ("go on", "I was verifying the existing web app and index.html.", True),
        ("carry on", "I was verifying the existing web app and index.html.", True),
        ("resume the check", "I was verifying the existing web app and index.html.", True),
        ("run it again", "I was verifying the existing web app and index.html.", True),
        ("rerun it", "I was verifying the existing web app and index.html.", True),
        ("rerun the test", "I was verifying the existing web app and index.html.", True),
        ("retest it", "I was verifying the existing web app and index.html.", True),
        ("test it again", "I was verifying the existing web app and index.html.", True),
        ("check it again", "I was verifying the existing web app and index.html.", True),
        ("run the validation again", "I was verifying the existing web app and index.html.", True),
        ("continue the web check", "Validation of index.html was interrupted.", True),
        ("please carry on with the validation", "Validation of index.html was interrupted.", True),
        ("re-run it", "Validation of index.html was interrupted.", True),
        ("run it once more", "Validation of index.html was interrupted.", True),
        ("please validate it again", "Validation of index.html was interrupted.", True),
        ("재검증해줘", "기존 웹 앱 검증 도중 중단되었습니다.", True),
        ("다시 확인해줘", "기존 웹 앱 검증 도중 중단되었습니다.", True),
        ("한번 더 확인해줘", "기존 웹 앱 검증 도중 중단되었습니다.", True),
        ("재확인해줘", "기존 웹 앱 검증 도중 중단되었습니다.", True),
        ("다시 봐줘", "기존 웹 앱 검증 도중 중단되었습니다.", True),
        ("다시 돌려줘", "기존 웹 앱 검증 도중 중단되었습니다.", True),
        ("기존 웹 앱 확인해줘", "", True),
        ("기존 웹 앱 체크해줘", "", True),
        ("기존 웹 앱 검수해줘", "", True),
        ("기존 웹 앱 한번 봐줘", "", True),
        ("문제 없는지 봐줘", "기존 웹 앱 검증이 중단되었습니다.", True),
        ("제대로 돌아가는지 봐줘", "기존 웹 앱 검증이 중단되었습니다.", True),
        ("please rerun the existing web app test; it is failing", "", True),
        ("could you please test the existing web app because it is failing?", "", True),
        ("The existing web app seems broken; please test it.", "", True),
        ("The existing web app test is failing. Please check it.", "", True),
        ("The existing web app does not work, so validate it.", "", True),
        ("It fails; run the existing web app test.", "", True),
        ("finish it", "I was validating the existing web app and index.html.", True),
        ("finish the validation", "I was validating the existing web app and index.html.", True),
        ("complete the validation", "I was validating the existing web app and index.html.", True),
        ("do the rest", "I was validating the existing web app and index.html.", True),
        ("take it from here", "I was validating the existing web app and index.html.", True),
        ("마저 해줘", "기존 HTML을 검증하고 있었습니다.", True),
        ("나머지도 해줘", "기존 HTML을 검증하고 있었습니다.", True),
        ("계속 진행해줘", "기존 HTML을 검증하고 있었습니다.", True),
        ("검증 계속 진행해줘", "기존 HTML을 검증하고 있었습니다.", True),
        ("계속 검증해줘", "기존 HTML을 검증하고 있었습니다.", True),
        ("검증 마저 해줘", "기존 HTML을 검증하고 있었습니다.", True),
        ("이어서 계속해줘", "기존 HTML을 검증하고 있었습니다.", True),
        ("나머지 검증도 해줘", "기존 HTML을 검증하고 있었습니다.", True),
        ("계속 부탁해", "index.html 검증이 중단되었습니다.", True),
        ("검증 계속 부탁해", "index.html 검증이 중단되었습니다.", True),
        ("계속 진행 부탁해", "index.html 검증이 중단되었습니다.", True),
        ("go ahead", "I was validating the existing web app and index.html.", True),
        ("please go ahead", "I was validating the existing web app and index.html.", True),
        ("use a.html", "Multiple HTML validation candidates: a.html, b.html. Select one.", True),
        ("go with a.html", "Multiple HTML validation candidates: a.html, b.html. Select one.", True),
        ("the first one", "Multiple HTML validation candidates: a.html, b.html. Select one.", True),
        ("the fourth one", "Multiple HTML validation candidates: a.html, b.html, c.html, d.html. Select one.", True),
        ("the fifth one", "Multiple HTML validation candidates: a.html, b.html, c.html, d.html, e.html. Select one.", True),
        ("4th one", "Multiple HTML validation candidates: a.html, b.html, c.html, d.html. Select one.", True),
        ("4번", "HTML 검증 후보는 a.html, b.html, c.html, d.html입니다. 하나를 선택해 주세요.", True),
        ("a.html", "Multiple HTML validation candidates: a.html, b.html. Select one.", True),
        ("a.html please", "Multiple HTML validation candidates: a.html, b.html. Select one.", True),
        ("the a.html file", "Multiple HTML validation candidates: a.html, b.html. Select one.", True),
        ("I want a.html", "Multiple HTML validation candidates: a.html, b.html. Select one.", True),
        ("a.html 선택", "HTML 검증 후보는 a.html, b.html입니다. 하나를 선택해 주세요.", True),
        ("a.html로", "HTML 검증 후보는 a.html, b.html입니다. 하나를 선택해 주세요.", True),
        ("검증해줘", "문서 정리 작업을 완료했습니다.", False),
        ("웹 검증 기능을 새로 만들어줘", "", False),
        ("버그를 고치고 재검증해줘", "웹 게임", False),
        ("HTML을 수정하고 검증해줘", "", False),
        ("검증 기능 다시 켜고 기존 HTML 수정 계속해줘", "", False),
        ("검증 기능 다시 켜고 웹 코딩 계속해줘", "", False),
        ("검증 기능 다시 켜고 기존 웹 작업 계속해줘", "", False),
        ("Review index.html and update it.", "", False),
        ("Review index.html and refactor it.", "", False),
        ("Review index.html and repair it.", "", False),
        ("Review index.html and patch it.", "", False),
        ("Review index.html and replace it.", "", False),
        ("Test index.html then delete it.", "", False),
        ("Review index.html and rename it.", "", False),
        ("Review index.html and move it.", "", False),
        ("웹 앱 검증해보고 문제 있으면 오류 수정 부탁해", "", False),
        ("웹 앱 검증하고 오류 수정 좀 해줘", "", False),
        ("웹 앱 확인 후 버그 패치 부탁해", "", False),
        ("Verify index.html, patch any bugs", "", False),
        ("Test index.html, fix bugs", "", False),
        ("Check the web app, update anything broken", "", False),
        ("웹 테스트하지 마", "", False),
        ("기존 HTML 검증은 필요 없어", "", False),
        ("HTML 테스트 방법 알려줘", "", False),
        ("웹 검증 기능이 어떻게 동작하는지 설명해줘", "", False),
        ("웹 검증이 뭐야", "", False),
        ("웹 검증 기능이 직접 클릭 이벤트가 동작을 안 하는 것 같은데?", "", False),
        ("HTML 검증이 제대로 동작하지 않는 것 같은데?", "", False),
        ("The existing web app test seems broken", "", False),
        ("The existing web app test is failing", "", False),
        ("The existing web app test does not work", "", False),
        ("Why is the existing web app test broken?", "", False),
        ("The existing web app test failed again", "", False),
        ("There is a bug in the existing web app test", "", False),
        ("continue editing the existing web app", "I was verifying index.html.", False),
        ("please continue coding the existing web app", "I was verifying index.html.", False),
        ("계속 개발해줘", "기존 HTML을 검증하고 있었습니다.", False),
        ("검증 가능해?", "HTML 게임을 완성했습니다.", False),
        ("do not test the existing HTML", "", False),
        ("이 Python 파일 검증해줘", "HTML 게임을 완성했습니다.", False),
        ("이 문서 테스트해줘", "지난번 HTML 게임을 만들었습니다.", False),
        ("don't do the rest", "I was validating the existing web app.", False),
        ("do not take it from here", "I was validating the existing web app.", False),
        ("don't keep going", "I was validating the existing web app.", False),
        ("no need to finish it", "I was validating the existing web app.", False),
        ("그렇게 하지 마", "기존 HTML을 검증하고 있었습니다.", False),
        ("이어서 하지 마", "기존 HTML을 검증하고 있었습니다.", False),
        ("What does finish it mean?", "I was validating the existing web app.", False),
        ("Is finish it the right phrase?", "I was validating the existing web app.", False),
        ("마저 할까?", "기존 HTML을 검증하고 있었습니다.", False),
        ("나머지도 해야 할까?", "기존 HTML을 검증하고 있었습니다.", False),
        ("계속해야 해?", "기존 HTML을 검증하고 있었습니다.", False),
        ("finish the code", "I was validating the existing web app.", False),
        ("complete the implementation", "I was validating the existing web app.", False),
        ("do the rest of the coding", "I was validating the existing web app.", False),
        ("take it from here and finish the feature", "I was validating the existing web app.", False),
        ("나머지 코드도 마저 해줘", "기존 HTML을 검증하고 있었습니다.", False),
        ("나머지 HTML 수정도 해줘", "기존 HTML을 검증하고 있었습니다.", False),
        ("수정 마저 해줘", "기존 HTML을 검증하고 있었습니다.", False),
        ("나머지 개발도 해줘", "기존 HTML을 검증하고 있었습니다.", False),
        ("The validation is complete.", "I was validating the existing web app.", False),
        ("Is the validation complete?", "I was validating the existing web app.", False),
        ("검증 완료됐어", "기존 HTML을 검증하고 있었습니다.", False),
        ("나머지는 끝났어", "기존 HTML을 검증하고 있었습니다.", False),
        ("마저 했어", "기존 HTML을 검증하고 있었습니다.", False),
        (
            "use a.html",
            "[PREVIOUS_USER]\n검증 기능 다시 켰어. 검증해줘\n"
            "[PREVIOUS_ASSISTANT]\nHTML 후보는 a.html, b.html입니다.",
            True,
        ),
        (
            "go ahead",
            "[PREVIOUS_USER]\n검증 기능 다시 켰어. 검증해줘\n"
            "[PREVIOUS_ASSISTANT]\nHTML 후보는 a.html, b.html입니다.",
            True,
        ),
        (
            "go ahead",
            "[PREVIOUS_USER]\n검증 기능 다시 켰어. 검증해줘\n"
            "[PREVIOUS_ASSISTANT]\n검증 도중 중단되었습니다.",
            True,
        ),
        (
            "계속해줘",
            "[PREVIOUS_USER]\n검증 기능 다시 켰어. 검증해줘\n"
            "[PREVIOUS_ASSISTANT]\n검증 도중 중단되었습니다.",
            True,
        ),
        ("finish it", "Implement the HTML validation feature in Python", False),
        ("Please test the fix in index.html.", "", True),
        ("Review the current implementation in index.html.", "", True),
        ("Validate the existing HTML after the fix.", "", True),
        ("Please test the bug fix in index.html.", "", True),
        ("Review the latest implementation in index.html.", "", True),
        ("Test the current build in index.html.", "", True),
        ("Test index.html without changes.", "", True),
        ("Please test index.html but do not change it.", "", True),
        ("index.html만 검증해줘", "", True),
        ("기존 index.html만 확인해줘", "", True),
        ("index.html 원본을 유지하고 확인해줘", "", True),
        ("How does web validation work? Verify public.html.", "", True),
        ("Why did the test fail? Now validate public.html.", "", True),
        ("Explain validation, then check public.html.", "", True),
        ("What is run_web? Please test public.html.", "", True),
        ("How does validation work, but verify public.html.", "", True),
        ("How does validation work and verify public.html.", "", True),
        ("Explain validation and verify public.html.", "", True),
        ("What is run_web, and can you test public.html?", "", True),
        ("Why did it fail; instead validate public.html.", "", True),
        ("Verify public.html and explain how validation works.", "", True),
        ("Verify public.html, then explain the result.", "", True),
        ("Check public.html; explain any failures.", "", True),
        ("Validate public.html and explain why it failed.", "", True),
        ("Please inspect public.html, and explain it.", "", True),
        ("검증 방법 알려줘. public.html 검증해줘", "", True),
        ("Do not verify secret.html; verify public.html.", "", True),
        ("verify public.html but do not verify secret.html", "", True),
        ("Do not verify secret.html, but verify public.html.", "", True),
        ("Do not verify secret.html but verify public.html.", "", True),
        ("Don't test secret.html and instead test public.html.", "", True),
        ("Don't run secret.html; instead run public.html.", "", True),
        ("Fix nothing; verify a.html.", "", True),
        ("Edit nothing, just verify a.html.", "", True),
        ("Change nothing; test a.html.", "", True),
        ("Modify nothing; check a.html.", "", True),
        ("Rewrite nothing; inspect a.html.", "", True),
        ("Make no changes; verify a.html.", "", True),
        ("Make zero changes; verify a.html.", "", True),
        ("Patch nothing; verify a.html.", "", True),
        ("Delete nothing; verify a.html.", "", True),
        ("Remove nothing; verify a.html.", "", True),
        ("Replace nothing; verify a.html.", "", True),
        ("Build nothing; verify a.html.", "", True),
        ("Create nothing; verify a.html.", "", True),
        ("Write nothing; verify a.html.", "", True),
        ("validate a different HTML", "기존 HTML 검증이 중단되었습니다.", True),
        ("validate another HTML", "기존 HTML 검증이 중단되었습니다.", True),
        ("validate HTML except the previous target", "기존 HTML 검증이 중단되었습니다.", True),
        ("validate HTML but not the previous one", "기존 HTML 검증이 중단되었습니다.", True),
        ("validate HTML without the old target", "기존 HTML 검증이 중단되었습니다.", True),
        ("validate anything other than before", "기존 HTML 검증이 중단되었습니다.", True),
        (
            "continue",
            "[PREVIOUS_USER]\nthe first one\n"
            "[PREVIOUS_ASSISTANT]\nI selected the first Python refactoring option.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI will edit a.html next.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nthe first one\n"
            "[PREVIOUS_ASSISTANT]\nI am implementing the first HTML test harness now.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nthe first one\n"
            "[PREVIOUS_ASSISTANT]\nValidation result is pending.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nthe fourth one\n"
            "[PREVIOUS_ASSISTANT]\nValidation candidate 4 is pending.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\n2nd one\n"
            "[PREVIOUS_ASSISTANT]\nCandidate selection complete; verification result pending.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI will edit a.html and run unit tests later.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI will fix a.html and then run validation.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI will modify a.html before validation.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI am building a.html and will validate it.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI built a.html and validation is pending.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI rebuilt a.html and validation is pending.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI wrote a.html and validation is pending.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI rewrote a.html and validation is pending.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI made a.html and validation is pending.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI generated a.html and validation is pending.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nI will create a.html, then run_web.",
            False,
        ),
        (
            "continue",
            "[PREVIOUS_USER]\nuse a.html\n"
            "[PREVIOUS_ASSISTANT]\nValidation approval is required.",
            True,
        ),
        (
            "계속해줘",
            "[PREVIOUS_USER]\na.html 선택\n"
            "[PREVIOUS_ASSISTANT]\n검증을 시작하겠습니다.",
            True,
        ),
    ],
)
def test_existing_web_validation_request_classifier(user_text, recent_context, expected):
    assert agent._looks_like_existing_web_validation_request(user_text, recent_context) is expected


@pytest.mark.parametrize(
    ("assistant_text", "expected"),
    [
        ("Validation of edited a.html is pending.", True),
        ("Validation of the fixed a.html is pending.", True),
        ("Verification of modified a.html was interrupted.", True),
        ("Testing the created a.html is in progress.", True),
        ("Validation of a.html is pending after editing.", True),
        ("I will fix a.html and then run validation.", False),
        ("I built a.html and validation is pending.", False),
    ],
)
def test_assistant_validation_state_distinguishes_status_from_development(
    assistant_text,
    expected,
):
    assert agent._assistant_has_active_validation_state(assistant_text) is expected


def test_interrupted_existing_html_validation_resumes_without_rewriting(env):
    import dataclasses

    import toolspec

    original = b"<!doctype html><button id='start'>Start</button>"
    (env.ws / "index.html").write_bytes(original)

    async def fake_run_web(root, **args):
        assert root == env.ws
        assert args["path"] == "index.html"
        assert args["steps"] == [{"assert": "visible", "by": "css", "selector": "#start"}]
        return "[WEB_VALIDATION v1]\nstatus=PASS level=function\nsummary=기존 게임 통과", "SHOT64"

    fake_spec = dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web)
    env.mp.setitem(toolspec.REGISTRY, "run_web", fake_spec)
    messages = [{"role": "user", "content": "검증 기능 다시 활성화했어. 검증해줘"}]
    enabled = [
        "list_tree", "read_file", "glob",
        "write_code_file", "edit_code_file", "multi_edit_code_file",
        "run_web", "run_code", "run_command",
    ]
    fake = FakeChat([
        {"content": "처음부터 다시 만들겠습니다."},
        {"calls": [("glob", {"pattern": "**/*.html"})]},
        {"content": "기존 index.html을 찾았습니다."},
        {"calls": [("run_web", {
            "path": "index.html",
            "steps": [{"assert": "visible", "by": "css", "selector": "#start"}],
        })]},
        {"content": "기존 결과물의 실행 검증을 완료했습니다."},
    ])

    events = env.drive(
        fake,
        approve=True,
        messages=messages,
        enabled_tools=enabled,
        approval_mode="auto",
    )

    exposed = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert exposed == {"list_tree", "read_file", "glob", "run_web"}
    system_prompt = fake.payloads[0]["messages"][0]["content"]
    assert "This request: validate existing web output only" in system_prompt
    assert "Authoring and command tools are temporarily restricted only for this request" in system_prompt
    notices = [event.get("text", "") for event in events if event["type"] == "notice"]
    assert sum("기존 웹 산출물을 먼저 찾습니다" in notice for notice in notices) == 1
    assert sum("실행 검증을 이어갑니다" in notice for notice in notices) == 1
    assert not any("기존 웹 산출물 검증 미실행" in notice for notice in notices)
    assert not any(
        "처음부터 다시" in event.get("text", "")
        for event in events if event["type"] == "content"
    )
    assert any(
        "기존 결과물의 실행 검증" in event.get("text", "")
        for event in events if event["type"] == "content"
    )
    assert any(
        message.get("role") == "user" and 'glob(pattern="**/*.html")' in message.get("content", "")
        for message in fake.payloads[1]["messages"]
    )
    assert any(
        message.get("role") == "user" and '["index.html"]' in message.get("content", "")
        for message in fake.payloads[3]["messages"]
    )
    assert [event["name"] for event in events if event["type"] == "tool_call"] == ["glob", "run_web"]
    assert not any(event["type"] == "approval_request" for event in events)
    result = next(event for event in events if event["type"] == "tool_result" and "status=PASS" in event["output"])
    assert result["ok"] is True
    assert next(event for event in events if event["type"] == "screenshot")["data"] == "SHOT64"
    assert fake.calls == 5
    assert not any(event["type"] == "error" for event in events)
    assert {
        path.relative_to(env.ws).as_posix(): path.read_bytes()
        for path in env.ws.rglob("*") if path.is_file()
    } == {"index.html": original}
    assert env.reindex_calls == []


def test_existing_html_validation_blocks_hallucinated_rewrite_tool(env):
    original = b"<!doctype html><p>keep me</p>"
    (env.ws / "index.html").write_bytes(original)
    messages = [
        {"role": "assistant", "content": "기존 HTML 게임은 index.html에 있습니다."},
        {"role": "user", "content": "검증 기능 다시 활성화했어. 검증해줘"},
    ]
    fake = FakeChat([{
        "calls": [("write_code_file", {"path": "index.html", "content": "<p>replace</p>"})],
    }])

    events = env.run(
        fake,
        messages=messages,
        enabled_tools=["glob", "read_file", "write_code_file", "run_web"],
        approval_mode="auto",
    )

    exposed = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert "write_code_file" not in exposed and "run_web" in exposed
    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    error = next(event["error"] for event in events if event["type"] == "error")
    assert "기존 웹 산출물 검증 요청" in error and "write_code_file" in error
    assert (env.ws / "index.html").read_bytes() == original
    assert env.reindex_calls == []


def test_existing_html_validation_reports_when_run_web_is_disabled(env):
    original = b"<!doctype html><p>unchanged</p>"
    (env.ws / "index.html").write_bytes(original)
    messages = [
        {"role": "assistant", "content": "기존 HTML 페이지를 완성했습니다."},
        {"role": "user", "content": "검증 기능 다시 활성화했어. 검증해줘"},
    ]
    fake = FakeChat([{"content": "현재 상태를 확인했습니다."}])

    events = env.run(
        fake,
        messages=messages,
        enabled_tools=["glob", "read_file", "write_code_file"],
        approval_mode="auto",
    )

    exposed = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert exposed == {"glob", "read_file"}
    notices = [event.get("text", "") for event in events if event["type"] == "notice"]
    assert not any("기존 웹 산출물을 찾아 검증" in notice for notice in notices)
    assert sum("웹 실행 검증 도구가 꺼져" in notice for notice in notices) == 1
    assert (env.ws / "index.html").read_bytes() == original
    assert env.reindex_calls == []


def test_existing_html_validation_with_no_candidate_stops_without_rewrite_or_extra_nudge(env):
    fake = FakeChat([
        {"calls": [("glob", {"pattern": "**/*.html"})]},
        {"content": "기존 HTML 산출물이 없습니다."},
    ])

    events = env.run(
        fake,
        messages=[{"role": "user", "content": "검증 기능 다시 켰어. 검증해줘"}],
        enabled_tools=["glob", "write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert fake.calls == 2
    assert [event["name"] for event in events if event["type"] == "tool_call"] == ["glob"]
    assert any("기존 HTML 산출물이 없습니다" in event.get("text", "") for event in events)
    assert sum(
        "기존 웹 산출물을 찾지 못해" in event.get("text", "")
        for event in events if event["type"] == "notice"
    ) == 1
    assert list(env.ws.iterdir()) == []


def test_existing_html_validation_with_multiple_candidates_asks_instead_of_guessing(env):
    (env.ws / "a.html").write_text("a", encoding="utf-8")
    (env.ws / "b.html").write_text("b", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in env.ws.iterdir()}
    fake = FakeChat([
        {"calls": [("glob", {"pattern": "**/*.html"})]},
        {"content": "a.html과 b.html 중 검증할 파일을 선택해 주세요."},
    ])

    events = env.run(
        fake,
        messages=[{"role": "user", "content": "검증 기능 다시 켰어. 검증해줘"}],
        enabled_tools=["glob", "write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert fake.calls == 2
    assert not any(event["name"] == "run_web" for event in events if event["type"] == "tool_call")
    assert any("검증할 파일을 선택" in event.get("text", "") for event in events)
    assert any("후보가 여러 개" in event.get("text", "") for event in events)
    assert {path.name: path.read_bytes() for path in env.ws.iterdir()} == before


def test_validation_blocks_run_web_target_that_does_not_match_discovery(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("a", encoding="utf-8")
    (env.ws / "b.html").write_text("b", encoding="utf-8")
    executed: list[str] = []

    async def fake_run_web(root, **args):
        executed.append(args["path"])
        assert args["_strict_local_assets"] is True
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([
        {"calls": [("run_web", {"path": "b.html"})]},
        {"calls": [("run_web", {"path": "a.html"})]},
        {"content": "a.html 검증을 마쳤습니다."},
    ])

    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "a.html 검증해줘"}],
        enabled_tools=["run_web"],
        approval_mode="auto",
    )

    assert executed == ["a.html"]
    assert not any(event["type"] == "approval_request" for event in events)
    assert any("허용 대상: a.html" in event.get("output", "") for event in events)
    assert any("a.html 검증을 마쳤습니다" in event.get("text", "") for event in events)


def test_explicit_multiple_validation_targets_are_not_completed_after_only_one(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("a", encoding="utf-8")
    (env.ws / "b.html").write_text("b", encoding="utf-8")

    async def fake_run_web(root, **args):
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([
        {"calls": [("run_web", {"path": "a.html"})]},
        {"content": "모두 완료했습니다."},
    ])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "a.html과 b.html을 검증해줘"}],
        enabled_tools=["run_web"],
        approval_mode="auto",
    )

    assert fake.calls == 3
    assert not any("모두 완료" in event.get("text", "") for event in events)
    assert any("미검증 대상: b.html" in event.get("text", "") for event in events)


def test_explicit_html_path_can_nudge_run_web_without_discovery_tool(env):
    import dataclasses

    import toolspec

    (env.ws / "index.html").write_text("<!doctype html><p>ok</p>", encoding="utf-8")

    async def fake_run_web(root, **args):
        assert args["path"] == "index.html"
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime\nsummary=로드 통과", "SHOT"

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([
        {"content": "확인하겠습니다."},
        {"calls": [("run_web", {"path": "index.html"})]},
        {"content": "기존 index.html 로드 검증을 마쳤습니다."},
    ])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "index.html을 검증해줘"}],
        enabled_tools=["run_web"],
        approval_mode="auto",
    )

    assert fake.calls == 3
    assert {tool["function"]["name"] for tool in fake.payloads[0]["tools"]} == {"run_web"}
    assert not any("glob(" in str(payload["messages"]) for payload in fake.payloads)
    assert sum(
        "실행 검증을 이어갑니다" in event.get("text", "")
        for event in events if event["type"] == "notice"
    ) == 1
    assert any("status=PASS" in event.get("output", "") for event in events)


def test_validation_without_path_or_discovery_tool_does_not_request_nonexistent_tool(env):
    fake = FakeChat([{"content": "검증 경로를 확인할 수 없습니다."}])
    events = env.run(
        fake,
        messages=[{"role": "user", "content": "검증 기능 다시 켰어. 검증해줘"}],
        enabled_tools=["run_web"],
        approval_mode="auto",
    )

    assert fake.calls == 1
    assert not any(event["type"] == "tool_call" for event in events)
    assert not any("먼저 찾습니다" in event.get("text", "") for event in events)
    assert any("기존 웹 산출물을 찾지 못해" in event.get("text", "") for event in events)


def test_run_web_rejection_is_not_reported_as_executed_and_is_not_reprompted(env):
    import dataclasses

    import toolspec

    (env.ws / "index.html").write_text("<!doctype html>", encoding="utf-8")
    executions = 0

    async def fake_run_web(root, **args):
        nonlocal executions
        executions += 1
        return "unexpected", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([
        {"calls": [("run_web", {"path": "index.html"})]},
        {"content": "승인되지 않아 실행하지 못했습니다."},
    ])
    events = env.drive(
        fake,
        approve=False,
        messages=[{"role": "user", "content": "index.html 검증해줘"}],
        enabled_tools=["run_web"],
        approval_mode="read",
    )

    assert executions == 0 and fake.calls == 1
    assert len([event for event in events if event["type"] == "approval_request"]) == 1
    assert not any(event["type"] == "content" for event in events)
    assert sum(
        "승인이 거부·만료" in event.get("text", "")
        for event in events if event["type"] == "notice"
    ) == 1


def test_rejecting_first_run_web_stops_remaining_calls_in_same_batch(env):
    (env.ws / "a.html").write_text("a", encoding="utf-8")
    (env.ws / "b.html").write_text("b", encoding="utf-8")
    fake = FakeChat([{"calls": [
        ("run_web", {"path": "a.html"}),
        ("run_web", {"path": "b.html"}),
    ]}])

    events = env.drive(
        fake,
        approve=False,
        messages=[{"role": "user", "content": "a.html과 b.html을 검증해줘"}],
        enabled_tools=["run_web"],
        approval_mode="read",
    )

    assert len([event for event in events if event["type"] == "approval_request"]) == 1
    assert len([event for event in events if event["type"] == "tool_call"]) == 1
    assert any(event["type"] == "done" for event in events)


def test_reused_rejected_run_web_stops_before_later_batch_approval(env, tmp_path):
    from agent_ledger import AgentExecutionLedger

    (env.ws / "a.html").write_text("a", encoding="utf-8")
    (env.ws / "b.html").write_text("b", encoding="utf-8")
    script = [{"calls": [
        ("run_web", {"path": "a.html"}),
        ("run_web", {"path": "b.html"}),
    ]}]
    common = dict(
        messages=[{"role": "user", "content": "a.html과 b.html을 검증해줘"}],
        enabled_tools=["run_web"],
        approval_mode="read",
        assistant_turn_id="validation-reuse-turn",
    )

    with AgentExecutionLedger(tmp_path / "validation-ledger.sqlite3") as ledger:
        first = env.drive(
            FakeChat(script),
            approve=False,
            execution_ledger=ledger,
            **common,
        )
        second = env.drive(
            FakeChat(script),
            approve=False,
            execution_ledger=ledger,
            **common,
        )

    assert len([event for event in first if event["type"] == "approval_request"]) == 1
    assert not any(event["type"] == "approval_request" for event in second)
    reused = next(event for event in second if event["type"] == "tool_result")
    assert reused["reused"] is True and reused["rejected"] is True
    assert len([event for event in second if event["type"] == "tool_call"]) == 1


def test_run_web_tool_error_is_not_reported_as_completed_validation(env):
    fake = FakeChat([
        {"calls": [("run_web", {"path": "missing.html"})]},
        {"content": "검증을 성공적으로 완료했습니다."},
    ])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "missing.html 검증해줘"}],
        enabled_tools=["run_web"],
        approval_mode="auto",
    )

    assert fake.calls == 2
    assert any("파일이 없습니다" in event.get("output", "") for event in events)
    assert not any("성공적으로 완료" in event.get("text", "") for event in events)
    assert any(
        "유효한 PASS·FAIL·INCONCLUSIVE 보고서를 반환하지 못했습니다" in event.get("text", "")
        for event in events if event["type"] == "notice"
    )


def test_run_web_approval_timeout_is_not_reported_as_executed(env):
    import dataclasses

    import toolspec

    (env.ws / "index.html").write_text("<!doctype html>", encoding="utf-8")
    executions = 0

    async def fake_run_web(root, **args):
        nonlocal executions
        executions += 1
        return "unexpected", None

    env.mp.setattr(agent, "APPROVAL_TIMEOUT", 0.001)
    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.run(
        FakeChat([
            {"calls": [("run_web", {"path": "index.html"})]},
            {"content": "승인 시간이 만료됐습니다."},
        ]),
        messages=[{"role": "user", "content": "index.html 검증해줘"}],
        enabled_tools=["run_web"],
        approval_mode="read",
    )

    assert executions == 0
    assert any(event["type"] == "approval_request" for event in events)
    assert any("승인이 거부·만료" in event.get("text", "") for event in events)


def test_validation_discovery_has_a_separate_bounded_turn_limit(env):
    scripts = [
        {"calls": [("glob", {"pattern": f"**/*{index}.html"})]}
        for index in range(agent._WEB_VALIDATION_DISCOVERY_TURN_LIMIT + 2)
    ]
    fake = FakeChat(scripts)
    events = env.run(
        fake,
        messages=[{"role": "user", "content": "검증 기능 다시 켰어. 검증해줘"}],
        enabled_tools=["glob", "run_web"],
        approval_mode="auto",
    )

    assert fake.calls == agent._WEB_VALIDATION_DISCOVERY_TURN_LIMIT
    assert not any(event["type"] == "approval_request" for event in events)
    assert sum(
        "탐색이 반복되어 안전 한도" in event.get("text", "")
        for event in events if event["type"] == "notice"
    ) == 1


def test_validation_discovery_batch_is_bounded_before_any_scan_runs(env, monkeypatch):
    calls = 0

    def counted_glob(root, **args):
        nonlocal calls
        calls += 1
        return "조건에 맞는 파일이 없습니다."

    import dataclasses
    import toolspec

    monkeypatch.setitem(
        toolspec.REGISTRY,
        "glob",
        dataclasses.replace(toolspec.REGISTRY["glob"], handler=counted_glob),
    )
    fake = FakeChat([{"calls": [
        ("glob", {"pattern": f"**/*{index}.html"}) for index in range(10)
    ]}])
    events = env.run(
        fake,
        messages=[{"role": "user", "content": "기존 웹 앱 검증해줘"}],
        enabled_tools=["glob", "run_web"],
        approval_mode="auto",
    )

    assert calls == 0
    assert not any(event["type"] == "tool_call" for event in events)
    assert any("탐색 호출이 안전 한도를 초과" in event.get("text", "") for event in events)


def test_validation_listing_call_returns_frozen_inventory_without_running_general_glob(
    env,
    monkeypatch,
):
    (env.ws / "index.html").write_text("ok", encoding="utf-8")
    calls = 0

    def forbidden_glob(root, **args):
        nonlocal calls
        calls += 1
        raise AssertionError("general glob must not run in validation-only mode")

    import dataclasses
    import toolspec

    monkeypatch.setitem(
        toolspec.REGISTRY,
        "glob",
        dataclasses.replace(toolspec.REGISTRY["glob"], handler=forbidden_glob),
    )
    fake = FakeChat([
        {"calls": [("glob", {"pattern": "**/*.html"})]},
        {"content": "index.html을 확인했습니다."},
    ])
    events = env.run(
        fake,
        messages=[{"role": "user", "content": "기존 웹 앱 검증해줘"}],
        enabled_tools=["glob", "run_web"],
        approval_mode="auto",
    )

    assert calls == 0
    result = next(event["output"] for event in events if event["type"] == "tool_result")
    assert "AISO_HTML_INVENTORY" in result and "index.html" in result


def test_validation_only_skips_indexed_rag_and_does_not_reindex(env):
    (env.ws / "index.html").write_text("<!doctype html>", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in env.ws.iterdir()}
    env.mp.setattr(agent, "rag_status", lambda root: {"indexed": True, "count": 1})

    async def forbidden_search(*_args, **_kwargs):
        raise AssertionError("validation-only must not call automatic RAG")

    env.mp.setattr(agent, "rag_search", forbidden_search)
    fake = FakeChat([{"content": "실행 도구가 꺼져 있습니다."}])
    events = env.run(
        fake,
        messages=[{"role": "user", "content": "index.html 검증해줘"}],
        enabled_tools=["search_docs", "glob", "read_file", "write_code_file"],
        approval_mode="auto",
    )

    exposed = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert "search_docs" not in exposed
    assert exposed.isdisjoint(agent._WEB_VALIDATION_BLOCKED_MUTATION_TOOLS)
    assert {path.name: path.read_bytes() for path in env.ws.iterdir()} == before
    assert env.reindex_calls == []
    assert any("웹 실행 검증 도구가 꺼져" in event.get("text", "") for event in events)


def test_validation_only_blocks_reading_non_web_assets(env):
    (env.ws / ".env").write_text("SECRET=value", encoding="utf-8")
    fake = FakeChat([{"calls": [("read_file", {"path": ".env"})]}])
    events = env.run(
        fake,
        messages=[{"role": "user", "content": "검증 기능 다시 켰어. 검증해줘"}],
        enabled_tools=["read_file", "run_web"],
        approval_mode="auto",
    )

    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    assert any("확인된 HTML 후보 자체만 읽을 수" in event.get("error", "") for event in events)


def test_validation_only_blocks_unrelated_web_extension_after_target_is_known(env):
    (env.ws / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (env.ws / "credentials.js").write_text("SECRET='do-not-read'", encoding="utf-8")
    fake = FakeChat([{"calls": [("read_file", {"path": "credentials.js"})]}])
    events = env.run(
        fake,
        messages=[{"role": "user", "content": "index.html 검증해줘"}],
        enabled_tools=["read_file", "run_web"],
        approval_mode="auto",
    )

    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    assert any("credentials.js" in event.get("error", "") for event in events)
    assert all("do-not-read" not in str(payload) for payload in fake.payloads)


def test_validation_only_cannot_self_register_an_unconfirmed_html_read(env):
    (env.ws / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (env.ws / "secrets.html").write_text("AISO_SECRET_HTML_7F31", encoding="utf-8")
    fake = FakeChat([
        {"calls": [("glob", {"pattern": "secrets.html"})]},
        {"calls": [("read_file", {"path": "secrets.html"})]},
    ])
    events = env.run(
        fake,
        messages=[{"role": "user", "content": "검증 기능 다시 켰어. 검증해줘"}],
        enabled_tools=["glob", "read_file", "run_web"],
        approval_mode="auto",
    )

    assert [
        event.get("name") for event in events if event["type"] == "tool_call"
    ] == ["glob"]
    assert any("secrets.html" in event.get("error", "") for event in events)
    assert all("AISO_SECRET_HTML_7F31" not in str(payload) for payload in fake.payloads)


def test_narrow_model_discovery_cannot_self_authorize_run_web(env):
    import dataclasses

    import toolspec

    (env.ws / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (env.ws / "secrets.html").write_text("private", encoding="utf-8")
    executed: list[str] = []

    async def fake_run_web(root, **args):
        executed.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([
        {"calls": [("glob", {"pattern": "secrets.html"})]},
        {"calls": [("run_web", {"path": "secrets.html"})]},
        {"content": "대상을 선택해야 합니다."},
    ])

    events = env.run(
        fake,
        messages=[{"role": "user", "content": "기존 웹 앱 검증해줘"}],
        enabled_tools=["glob", "run_web"],
        approval_mode="auto",
    )

    assert executed == []
    assert not any(event["type"] == "approval_request" for event in events)
    assert any("후보가 여러 개" in event.get("output", "") for event in events)


def test_explicit_html_target_freezes_read_allowlist_against_later_discovery(env):
    (env.ws / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (env.ws / "secrets.html").write_text("AISO_SECRET_HTML_9C82", encoding="utf-8")
    fake = FakeChat([
        {"calls": [("glob", {"pattern": "**/*.html"})]},
        {"calls": [("read_file", {"path": "secrets.html"})]},
    ])

    events = env.run(
        fake,
        messages=[{"role": "user", "content": "index.html을 검증해줘"}],
        enabled_tools=["glob", "read_file", "run_web"],
        approval_mode="auto",
    )

    assert not any(
        event.get("name") == "read_file" and event.get("type") == "tool_call"
        for event in events
    )
    assert any("secrets.html" in event.get("error", "") for event in events)
    assert all("AISO_SECRET_HTML_9C82" not in str(payload) for payload in fake.payloads)


def test_multiple_authoritative_candidates_block_every_read_until_user_selects(env):
    (env.ws / "a.html").write_text("A", encoding="utf-8")
    (env.ws / "b.html").write_text("B", encoding="utf-8")
    fake = FakeChat([{"calls": [("read_file", {"path": "a.html"})]}])

    events = env.run(
        fake,
        messages=[{"role": "user", "content": "기존 웹 앱 검증해줘"}],
        enabled_tools=["read_file", "run_web"],
        approval_mode="auto",
    )

    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    assert any("a.html" in event.get("error", "") for event in events)


def test_candidate_selection_followup_keeps_validation_only_and_preserves_file(env):
    original = b"<!doctype html><p>A</p>"
    (env.ws / "a.html").write_bytes(original)
    (env.ws / "b.html").write_text("B", encoding="utf-8")
    fake = FakeChat([{
        "calls": [("write_code_file", {"path": "a.html", "content": "REPLACED"})],
    }])
    messages = [
        {"role": "user", "content": "기존 웹 앱을 검증해줘"},
        {"role": "assistant", "content": "HTML 후보는 a.html, b.html입니다. 하나를 선택해 주세요."},
        {"role": "user", "content": "use a.html"},
    ]

    events = env.run(
        fake,
        messages=messages,
        enabled_tools=["write_code_file", "read_file", "run_web"],
        approval_mode="auto",
    )

    exposed = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert "write_code_file" not in exposed
    assert any("write_code_file" in event.get("error", "") for event in events)
    assert (env.ws / "a.html").read_bytes() == original


def test_ordinal_candidate_selection_requires_an_exact_path(env):
    (env.ws / "a.html").write_text("A", encoding="utf-8")
    (env.ws / "b.html").write_text("B", encoding="utf-8")
    fake = FakeChat([{"calls": [("read_file", {"path": "a.html"})]}])
    messages = [
        {"role": "user", "content": "기존 웹 앱을 검증해줘"},
        {"role": "assistant", "content": "후보는 a.html, b.html입니다. 선택해 주세요."},
        {"role": "user", "content": "the first one"},
    ]

    events = env.run(
        fake,
        messages=messages,
        enabled_tools=["read_file", "run_web"],
        approval_mode="auto",
    )

    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    assert any("확인된 HTML 후보" in event.get("error", "") for event in events)
    assert any("상대 경로" in event.get("text", "") for event in events)


def test_more_recent_non_web_user_task_prevents_stale_validation_continuation(env):
    messages = [
        {"role": "user", "content": "기존 HTML을 검증해줘"},
        {"role": "assistant", "content": "검증을 시작했습니다."},
        {"role": "user", "content": "Python 구현 작업을 진행해줘"},
        {"role": "assistant", "content": "Python 구현을 진행 중입니다."},
        {"role": "user", "content": "finish it"},
    ]
    fake = FakeChat([{"content": "이어가겠습니다."}])

    env.run(
        fake,
        messages=messages,
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    exposed = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert "write_code_file" in exposed


def test_selected_candidate_remains_validation_only_on_following_continue(env):
    (env.ws / "a.html").write_text("ORIGINAL", encoding="utf-8")
    messages = [
        {"role": "user", "content": "검증 기능 다시 켰어. 검증해줘"},
        {"role": "assistant", "content": "후보는 a.html, b.html입니다."},
        {"role": "user", "content": "use a.html"},
        {"role": "assistant", "content": "a.html을 검증 대상으로 선택했습니다."},
        {"role": "user", "content": "continue"},
    ]
    fake = FakeChat([{
        "calls": [("write_code_file", {"path": "a.html", "content": "REPLACED"})],
    }])

    events = env.run(
        fake,
        messages=messages,
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    exposed = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert "write_code_file" not in exposed
    assert any("write_code_file" in event.get("error", "") for event in events)
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "ORIGINAL"


def test_exact_candidate_selection_is_carried_across_continue_with_multiple_inventory(env):
    (env.ws / "a.html").write_text("SELECTED_A", encoding="utf-8")
    (env.ws / "b.html").write_text("B", encoding="utf-8")
    messages = [
        {"role": "user", "content": "기존 웹 앱 검증해줘"},
        {"role": "assistant", "content": "후보는 a.html, b.html입니다."},
        {"role": "user", "content": "use a.html"},
        {"role": "assistant", "content": "Validation approval is required."},
        {"role": "user", "content": "continue"},
    ]
    fake = FakeChat([{"calls": [("read_file", {"path": "a.html"})]}])

    events = env.run(
        fake,
        messages=messages,
        enabled_tools=["read_file", "run_web", "write_code_file"],
        approval_mode="auto",
    )

    result = next(event["output"] for event in events if event["type"] == "tool_result")
    assert "SELECTED_A" in result
    exposed = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert "write_code_file" not in exposed


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("verify index.html, but do not read secret.html", []),
        ("only validate public/index.html; private/admin.html is out of scope", []),
        ("secret.html is out of scope", []),
        ("verify index.html, but don't touch secret.html", []),
        ("leave secret.html alone", []),
        ("except secret.html", []),
        ("not secret.html", []),
        ("secret.html must not be read", []),
        ("keep secret.html private", []),
        ("verify index.html; avoid secret.html", []),
        ("skip secret.html", []),
        ("secret.html is forbidden", []),
        ("omit secret.html", []),
        ("without secret.html", []),
        ("verify a.html.", ["a.html"]),
        ("a.html과 b.html을 검증해줘", ["a.html", "b.html"]),
        ("Please test the bug fix in index.html.", ["index.html"]),
        ("Review the latest implementation in index.html.", ["index.html"]),
        ("Test the current build in index.html.", ["index.html"]),
        ("Only test index.html.", ["index.html"]),
        ("Just test index.html.", ["index.html"]),
        ("Please just test index.html.", ["index.html"]),
        ("Test only index.html.", ["index.html"]),
        ("Test index.html only.", ["index.html"]),
        ("Test index.html without changes.", ["index.html"]),
        ("Verify index.html without editing it.", ["index.html"]),
        ("Only test index.html; do not modify it.", ["index.html"]),
        ("Please test index.html but do not change it.", ["index.html"]),
        ("index.html만 검증해줘", ["index.html"]),
        ("index.html만 테스트해줘", ["index.html"]),
        ("index.html 파일만 검증해줘", ["index.html"]),
        ("index.html을 검증만 해줘", ["index.html"]),
        ("기존 index.html만 확인해줘", ["index.html"]),
        ("index.html 수정하지 말고 검증해줘", ["index.html"]),
        ("index.html은 건드리지 말고 테스트해줘", ["index.html"]),
        ("index.html을 변경 없이 검증해줘", ["index.html"]),
        ("index.html 원본을 유지하고 확인해줘", ["index.html"]),
        ("Validate both a.html and b.html.", ["a.html", "b.html"]),
        ("Please test both a.html and b.html.", ["a.html", "b.html"]),
        ("Validate a.html, b.html, and c.html.", ["a.html", "b.html", "c.html"]),
        ("Check a.html and also b.html.", ["a.html", "b.html"]),
        ("a.html과 b.html 둘 다 검증해줘", ["a.html", "b.html"]),
        ("Review the code in index.html.", ["index.html"]),
        ("Inspect the markup in index.html.", ["index.html"]),
        ("Test the JavaScript in index.html.", ["index.html"]),
        ("Check the game in index.html.", ["index.html"]),
        ("Validate the button in index.html.", ["index.html"]),
        ("Test the UI in index.html.", ["index.html"]),
        ("Review the behavior in index.html.", ["index.html"]),
        ("Test `index.html`.", ["index.html"]),
        ('Verify "index.html".', ["index.html"]),
        ("Check (index.html).", ["index.html"]),
        ("Review [index.html].", ["index.html"]),
        ("Revalidate index.html.", ["index.html"]),
        ("Re-validate index.html.", ["index.html"]),
        ("Retest index.html.", ["index.html"]),
        ("Re-test index.html.", ["index.html"]),
        ("Rerun index.html.", ["index.html"]),
        ("Re-run index.html.", ["index.html"]),
        ("Validate index.html again.", ["index.html"]),
        ("Test index.html again.", ["index.html"]),
        ("Review index.html and update it.", []),
        ("Review index.html and refactor it.", []),
        ("Test index.html then delete it.", []),
        ("verify nothing in secret.html", []),
        ("check nothing in secret.html", []),
        ("test nothing in secret.html", []),
        ("inspect no code in secret.html", []),
        ("review no changes in secret.html", []),
        ("validate neither feature in secret.html", []),
        ("verify anything except the code in secret.html", []),
        ("verify no implementation in secret.html", []),
        ("Could you test index.html?", ["index.html"]),
        ("Can you validate index.html?", ["index.html"]),
        ("Test index.html without changing anything.", ["index.html"]),
        ("Verify index.html without making any changes.", ["index.html"]),
        ("Verify index.html and change nothing.", ["index.html"]),
    ],
)
def test_explicit_html_paths_exclude_negative_or_out_of_scope_targets(text, expected):
    assert agent._explicit_html_paths(text) == expected


def test_denied_explicit_path_never_falls_back_to_unique_inventory(env):
    (env.ws / "secret.html").write_text("AISO_DENY_CANARY_42", encoding="utf-8")
    fake = FakeChat([{"calls": [("read_file", {"path": "secret.html"})]}])

    events = env.run(
        fake,
        messages=[{
            "role": "user",
            "content": "verify missing.html, but do not read secret.html",
        }],
        enabled_tools=["read_file", "run_web"],
        approval_mode="auto",
    )

    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    assert any("확인된 HTML 후보" in event.get("error", "") for event in events)
    assert all("AISO_DENY_CANARY_42" not in str(payload) for payload in fake.payloads)


@pytest.mark.parametrize(
    "text",
    [
        "validate the existing HTML, but not the first one",
        "first validate HTML, then report",
        "validate HTML first",
        "test HTML for the first time",
    ],
)
def test_ordinal_words_outside_positive_selection_do_not_authorize(text):
    inventory = ["a.html", "b.html"]
    context = (
        "[PREVIOUS_USER]\n기존 HTML을 검증해줘\n"
        "[PREVIOUS_ASSISTANT]\n후보는 a.html, b.html입니다."
    )
    assert agent._followup_html_selection_paths(text, context, inventory) == []


def test_ordinal_selection_is_not_resolved_without_a_persisted_snapshot():
    inventory = ["a.html", "b.html", "decoy.html"]
    context = (
        "[PREVIOUS_USER]\n기존 HTML을 검증해줘\n"
        "[PREVIOUS_ASSISTANT]\nDo not use decoy.html. Candidates: b.html, a.html."
    )
    assert agent._followup_html_selection_paths("the first one", context, inventory) == []


@pytest.mark.parametrize(
    "selection",
    [
        "the fourth one",
        "the fifth one",
        "4th one",
        "4번",
    ],
)
def test_later_ordinal_selection_also_requires_an_exact_path(selection):
    inventory = ["a.html", "b.html", "c.html", "d.html", "e.html"]
    context = (
        "[PREVIOUS_USER]\n기존 HTML을 검증해줘\n"
        "[PREVIOUS_ASSISTANT]\n후보는 a.html, b.html, c.html, d.html, e.html입니다."
    )
    assert agent._followup_html_selection_paths(selection, context, inventory) == []


def test_previous_ordinal_is_not_reinterpreted_against_changed_inventory(env):
    for name in ("0-new.html", "a.html", "b.html"):
        (env.ws / name).write_text(name, encoding="utf-8")
    executed: list[str] = []

    async def fake_run_web(root, **args):
        executed.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    import dataclasses
    import toolspec

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{"calls": [("run_web", {"path": "0-new.html"})]}]),
        approve=True,
        messages=[
            {"role": "user", "content": "the first one"},
            {
                "role": "assistant",
                "content": "Validation of a.html was interrupted; run_web approval is pending.",
            },
            {"role": "user", "content": "continue"},
        ],
        enabled_tools=["run_web", "write_code_file"],
    )

    assert executed == []
    assert any(
        "확정하지 못했습니다" in str(event.get(key, ""))
        for event in events for key in ("output", "error", "text")
    )


def test_current_ordinal_never_reuses_previous_exact_path(env):
    for name in ("a.html", "b.html", "c.html"):
        (env.ws / name).write_text(name, encoding="utf-8")
    executed: list[str] = []

    async def fake_run_web(root, **args):
        executed.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    import dataclasses
    import toolspec

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{"calls": [("run_web", {"path": "a.html"})]}]),
        approve=True,
        messages=[
            {"role": "user", "content": "verify a.html"},
            {
                "role": "assistant",
                "content": "Validation candidates are b.html and c.html; selection is pending.",
            },
            {"role": "user", "content": "the second one"},
        ],
        enabled_tools=["run_web", "write_code_file"],
    )

    assert executed == []
    assert any(
        "확정하지 못했습니다" in str(event.get(key, ""))
        for event in events for key in ("output", "error", "text")
    )


def test_scope_exclusion_does_not_reuse_previous_exact_path(env):
    (env.ws / "a.html").write_text("a", encoding="utf-8")
    (env.ws / "b.html").write_text("b", encoding="utf-8")
    executed: list[str] = []

    async def fake_run_web(root, **args):
        executed.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    import dataclasses
    import toolspec

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{"calls": [("run_web", {"path": "b.html"})]}]),
        approve=True,
        messages=[
            {"role": "user", "content": "verify b.html"},
            {"role": "assistant", "content": "Validation of b.html was interrupted."},
            {"role": "user", "content": "validate HTML, but not the second one"},
        ],
        enabled_tools=["run_web", "write_code_file"],
    )

    assert executed == []
    assert any(
        "확정하지 못했습니다" in str(event.get(key, ""))
        for event in events for key in ("output", "error", "text")
    )


def test_scope_change_never_falls_back_to_the_only_old_inventory_target(env):
    (env.ws / "b.html").write_text("b", encoding="utf-8")
    executed: list[str] = []

    async def fake_run_web(root, **args):
        executed.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    import dataclasses
    import toolspec

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{"calls": [("run_web", {"path": "b.html"})]}]),
        approve=True,
        messages=[
            {"role": "user", "content": "verify b.html"},
            {"role": "assistant", "content": "Validation of b.html was interrupted."},
            {"role": "user", "content": "validate a different HTML"},
        ],
        enabled_tools=["run_web", "write_code_file"],
    )

    assert executed == []
    assert any(
        "확정하지 못했습니다" in str(event.get(key, ""))
        for event in events for key in ("output", "error", "text")
    )


def test_malformed_web_validation_results_stop_at_dedicated_limit(env):
    import dataclasses
    import toolspec

    (env.ws / "index.html").write_text("<!doctype html>", encoding="utf-8")
    executions = 0

    async def fake_run_web(root, **args):
        nonlocal executions
        executions += 1
        return f"malformed result {executions}", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([
            {"calls": [("run_web", {"path": "index.html", "steps": [{"action": "wait", "ms": 1}]})]},
            {"calls": [("run_web", {"path": "index.html", "steps": [{"action": "wait", "ms": 2}]})]},
            {"calls": [("run_web", {"path": "index.html", "steps": [{"action": "wait", "ms": 3}]})]},
        ]),
        approve=True,
        messages=[{"role": "user", "content": "index.html 검증해줘"}],
        enabled_tools=["run_web"],
    )

    assert executions == agent._WEB_VALIDATION_INVALID_RUN_LIMIT == 2
    assert any("연속으로 유효하지 않아" in event.get("text", "") for event in events)


def test_html_write_without_validation_is_nudged_once_then_run_web_can_finish(env):
    import dataclasses

    import toolspec

    async def fake_run_web(root, **args):
        return "[WEB_VALIDATION v1]\nstatus=PASS level=interaction\nsummary=통과", "SHOT64"

    fake_spec = dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web)
    env.mp.setitem(toolspec.REGISTRY, "run_web", fake_spec)
    fake = FakeChat([
        {"calls": [("write_code_file", {"path": "index.html", "content": "<button>시작</button>"})]},
        {"content": "완료했습니다."},
        {"calls": [("run_web", {"path": "./index.html", "steps": [
            {"assert": "visible", "by": "role", "role": "button", "name": "시작"},
        ]})]},
        {"content": "실행과 상호작용 검증까지 통과했습니다."},
    ])

    events = env.drive(
        fake,
        approve=True,
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    notices = [event.get("text", "") for event in events if event["type"] == "notice"]
    assert sum("HTML의 실행·상호작용 검증" in notice for notice in notices) == 1
    assert fake.calls == 4
    assert any(
        message.get("role") == "user" and "have not yet received a run_web PASS" in message.get("content", "")
        for message in fake.payloads[2]["messages"]
    )
    results = [event for event in events if event["type"] == "tool_result"]
    assert results[-1]["ok"] is True and "status=PASS" in results[-1]["output"]


def test_html_rewrite_does_not_rearm_validation_nudge_and_reports_unverified(env):
    fake = FakeChat([
        {"calls": [("write_code_file", {"path": "index.html", "content": "<p>v1</p>"})]},
        {"content": "검증 없이 끝냅니다."},
        {"calls": [("write_code_file", {"path": "index.html", "content": "<p>v2</p>"})]},
        {"content": "다시 검증 없이 끝냅니다."},
    ])

    events = env.run(
        fake,
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    notices = [event.get("text", "") for event in events if event["type"] == "notice"]
    assert sum("HTML의 실행·상호작용 검증" in notice for notice in notices) == 1
    assert sum("웹 검증 미완료" in notice for notice in notices) == 1
    assert "index.html" in next(notice for notice in notices if "웹 검증 미완료" in notice)
    assert fake.calls == 4


# ── 오프로드 사다리: 크래시 → CPU 오프로드 알림 1회 → 회복 ──
def test_offload_ladder_notice_once(env):
    # 두 턴 모두 첫 시도에서 로드 크래시 → 다음 사다리 단계로 회복.
    # 오프로드 알림은 런 전체에서 딱 한 번만 떠야 한다(_generate_turn의 offload_noticed 스레딩).
    evs = env.run(FakeChat([
        {"raise": load_crash()},                    # 턴1 시도0: 크래시 → 알림 + 사다리 하강
        {"calls": [("list_dir", {"path": "."})]},   # 턴1 시도1: 회복(툴콜)
        {"raise": load_crash()},                    # 턴2 시도0: 또 크래시 → 이미 알렸으니 알림 없음
        {"content": "끝."},                          # 턴2 시도1: 회복(완료)
    ]))
    t = types(evs)
    offload_notices = [e for e in evs if e["type"] == "notice" and "오프로드" in e.get("text", "")]
    assert len(offload_notices) == 1  # 런 1회만
    assert "error" not in t and t[-1] == "done"  # 크래시에도 정상 완료


# ── 파일 변경 → 백그라운드 재색인 트리거 ────────────────────
def test_dirty_triggers_reindex(env):
    env.mp.setattr(agent, "rag_status", lambda root: {"indexed": True, "embed_model": "e", "count": 1})

    async def fake_search(root, host, q, k):
        return []

    env.mp.setattr(agent, "rag_search", fake_search)
    env.mp.setattr(agent, "format_context", lambda results: "")
    evs = env.run(
        FakeChat([
            # 문서 쓰기는 .md만 허용 — 재색인 트리거(dirty) 검증은 확장자와 무관하므로 .md로 쓴다.
            {"calls": [("write_file", {"path": "a.md", "content": "hi"})]},
            {"content": "작성 완료."},
        ]),
        approval_mode="auto",  # 승인 스킵
    )
    assert types(evs)[-1] == "done"
    assert len(env.reindex_calls) >= 1  # dirty + indexed → _fire_reindex 호출


def test_dirty_reindexes_when_next_model_turn_fails(env):
    env.mp.setattr(agent, "rag_status", lambda root: {"indexed": True, "embed_model": "e", "count": 1})

    async def fake_search(root, host, q, k):
        return []

    env.mp.setattr(agent, "rag_search", fake_search)
    env.mp.setattr(agent, "format_context", lambda results: "")
    events = env.run(
        FakeChat(
            [
                {"calls": [("write_file", {"path": "a.md", "content": "hi"})]},
                {"raise": ConnectionError("refused")},
            ]
        ),
        approval_mode="auto",
    )
    assert "error" in types(events)
    assert len(env.reindex_calls) >= 1


def test_dirty_reindexes_when_agent_stream_is_cancelled(env):
    import asyncio as _aio

    import pytest

    env.mp.setattr(agent, "rag_status", lambda root: {"indexed": True, "embed_model": "e", "count": 1})

    async def fake_search(root, host, q, k):
        return []

    env.mp.setattr(agent, "rag_search", fake_search)
    env.mp.setattr(agent, "format_context", lambda results: "")
    with pytest.raises(_aio.CancelledError):
        env.run(
            FakeChat(
                [
                    {"calls": [("write_file", {"path": "a.md", "content": "hi"})]},
                    {"raise": _aio.CancelledError()},
                ]
            ),
            approval_mode="auto",
        )
    assert len(env.reindex_calls) >= 1


def test_dirty_reindexes_when_mutating_tool_is_cancelled_mid_execution(env):
    """도구 반환 전 취소돼도 이미 쓴 파일과 RAG 색인을 다시 맞춘다."""
    import asyncio as _aio

    import pytest

    env.mp.setattr(agent, "rag_status", lambda root: {"indexed": True, "embed_model": "e", "count": 1})

    async def fake_search(root, host, q, k):
        return []

    started = _aio.Event()

    async def mutating_execute(spec, root, host, args):
        assert spec.mutates is True
        (root / "partial.md").write_text("partial", encoding="utf-8")
        started.set()
        await _aio.Future()

    env.mp.setattr(agent, "rag_search", fake_search)
    env.mp.setattr(agent, "format_context", lambda results: "")
    env.mp.setattr(agent, "execute", mutating_execute)
    chat = FakeChat([{"calls": [("write_file", {"path": "a.md", "content": "hi"})]}])
    env.mp.setattr(agent, "_chat_turn", chat)

    async def cancel_during_execution():
        stream = env._agen(chat, approval_mode="auto")
        iterator = stream.__aiter__()
        assert (await iterator.__anext__())["type"] == "tool_call"
        pending_event = _aio.create_task(iterator.__anext__())
        await _aio.wait_for(started.wait(), timeout=1)
        pending_event.cancel()
        with pytest.raises(_aio.CancelledError):
            await pending_event
        await stream.aclose()

    _aio.run(cancel_during_execution())
    assert (env.ws / "partial.md").read_text(encoding="utf-8") == "partial"
    assert len(env.reindex_calls) >= 1


# ── 치명적 오류: 잘못된 워크스페이스 / 연결 실패 ────────────
def test_fatal_bad_workspace(env, tmp_path):
    # 존재하지 않는 폴더를 '지정'하면 치명적 오류. (빈 문자열=미지정은 무폴더 모드로 별도 테스트)
    evs = env.run(FakeChat([{"content": "x"}]), workspace=str(tmp_path / "no_such_dir"))
    t = types(evs)
    assert t == ["error"]  # error 하나만, done 없음


def test_fatal_connection(env):
    evs = env.run(FakeChat([{"raise": ConnectionError("refused")}]))
    errs = [e for e in evs if e["type"] == "error"]
    assert errs and "연결 실패" in errs[-1]["error"]
    assert "done" not in types(evs)


# ── RAG 컨텍스트는 시스템 지시와 분리되고 search_docs가 맨 앞 ──
def test_rag_context_injected(env):
    env.mp.setattr(agent, "rag_status", lambda root: {"indexed": True, "embed_model": "e", "count": 3})

    async def fake_search(root, host, q, k):
        return [{"file": "a.py", "start": 1, "end": 2, "text": "code", "score": 0.9}]

    env.mp.setattr(agent, "rag_search", fake_search)
    env.mp.setattr(agent, "format_context", lambda results: "[[RAGCTX]]")

    fake = FakeChat([{"content": "답변."}])
    env.run(fake)
    payload = fake.payloads[0]
    sys_content = payload["messages"][0]["content"]
    assert "[[RAGCTX]]" not in sys_content and "Use search_docs to search the workspace semantically" in sys_content
    assert payload["messages"][1]["content"] == "[[RAGCTX]]"
    assert payload["tools"][0]["function"]["name"] == "search_docs"  # 맨 앞에 prepend


def test_workspace_context_requires_approval_before_web_egress_in_read_mode(env):
    """Read mode asks before a web call follows workspace-derived context."""
    (env.ws / "note.txt").write_text("workspace data", encoding="utf-8")
    fake = FakeChat([
        {"calls": [
            ("read_file", {"path": "note.txt"}),
            ("web_search", {"query": "safe public documentation"}),
        ]},
        {"content": "done"},
    ])
    events = env.drive(fake, approve=False, approval_mode="read")
    approvals = [event for event in events if event.get("type") == "approval_request"]
    assert [event["name"] for event in approvals] == ["web_search"]
    web_result = next(
        event for event in events
        if event.get("type") == "tool_result" and event.get("id") == approvals[0]["id"]
    )
    assert web_result["rejected"] is True


def test_workspace_context_web_egress_runs_without_approval_in_auto_mode(env):
    """Auto is an explicit opt-in: workspace context never opens an approval card."""
    import dataclasses

    import toolspec

    (env.ws / "note.txt").write_text("workspace data", encoding="utf-8")
    queries: list[str] = []

    async def fake_web_search(query: str = "", **_ignored):
        queries.append(query)
        return "public search result"

    env.mp.setitem(
        toolspec.REGISTRY,
        "web_search",
        dataclasses.replace(toolspec.REGISTRY["web_search"], handler=fake_web_search),
    )
    fake = FakeChat([
        {"calls": [
            ("read_file", {"path": "note.txt"}),
            ("web_search", {"query": "safe public documentation"}),
        ]},
        {"content": "done"},
    ])

    events = env.run(fake, approval_mode="auto")

    assert not any(event.get("type") == "approval_request" for event in events)
    assert queries == ["safe public documentation"]
    assert any(
        event.get("type") == "tool_result" and event.get("output") == "public search result"
        for event in events
    )


def test_automatic_rag_requires_approval_before_web_egress_in_read_mode(env):
    """Read mode treats automatic RAG as workspace data before a web call."""
    env.mp.setattr(agent, "rag_status", lambda root: {"indexed": True})

    async def fake_search(root, host, q, k):
        return [{"file": "note.txt", "start": 1, "end": 1, "text": "private", "score": 1.0}]

    env.mp.setattr(agent, "rag_search", fake_search)
    env.mp.setattr(agent, "format_context", lambda results: "[UNTRUSTED_WORKSPACE_CONTEXT]")
    fake = FakeChat([{"calls": [("web_search", {"query": "public docs"})]}, {"content": "done"}])
    events = env.drive(fake, approve=False, approval_mode="read")
    approvals = [event for event in events if event.get("type") == "approval_request"]
    assert [event["name"] for event in approvals] == ["web_search"]


@pytest.mark.parametrize(
    "text",
    [
        "Do not revalidate the existing HTML.",
        "Never test the existing HTML.",
        "No need to rerun the existing HTML.",
        "Don't re-test the existing HTML.",
        "Should we re-run the existing HTML?",
        "Why rerun the existing HTML?",
        "I am not asking you to validate the existing HTML.",
        "Show me how to validate the existing HTML.",
        "What happens if I validate the existing HTML?",
        "I will validate the existing HTML later.",
    ],
)
def test_nonexecuting_web_validation_language_is_guarded_without_execution(text):
    masked = agent._mask_html_path_mentions(text)
    assert agent._looks_like_existing_web_validation_request(text, "") is False
    assert agent._looks_like_guarded_web_validation_turn(text, "") is True
    assert agent._is_nonexecuting_web_validation_statement(masked) is True


def test_nonexecuting_web_validation_turn_exposes_no_tools_or_run_web(env):
    import dataclasses

    import toolspec

    (env.ws / "index.html").write_text("ORIGINAL", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([{"calls": [("run_web", {"path": "index.html"})]}])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "Do not revalidate the existing HTML."}],
        enabled_tools=["run_web", "write_code_file", "read_file"],
        approval_mode="auto",
    )

    assert fake.payloads[0]["tools"] == []
    assert executions == []
    assert (env.ws / "index.html").read_text(encoding="utf-8") == "ORIGINAL"
    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)


@pytest.mark.parametrize(
    "text",
    [
        "Explain validation, also verify public.html.",
        "Explain validation; also verify public.html.",
        "Explain validation - verify public.html.",
        "Explain validation while you verify public.html.",
        "How does validation work: verify public.html.",
        "Do not verify secret.html; however verify public.html.",
        "Do not verify secret.html. Also verify public.html.",
        "Do not verify secret.html; rather verify public.html.",
    ],
)
def test_mixed_meta_or_negative_clause_stays_validation_only(text):
    assert agent._looks_like_existing_web_validation_request(text, "") is True
    assert agent._looks_like_guarded_web_validation_turn(text, "") is True


@pytest.mark.parametrize(
    "verb",
    [
        "deploy", "publish", "format", "convert", "export", "archive", "upload",
        "install", "minify", "bundle", "compile", "package", "commit", "push", "release",
    ],
)
def test_validation_followed_by_state_change_is_not_reduced_to_validation_only(verb):
    text = f"Test the existing HTML and {verb} it."
    assert agent._contains_explicit_mutation_request(text) is True
    assert agent._looks_like_existing_web_validation_request(text, "") is False
    assert agent._looks_like_guarded_web_validation_turn(text, "") is False


@pytest.mark.parametrize(
    "path",
    ["../secret.html", "/secret.html", "file:///secret.html", "https://example.com/secret.html", "C:\\secret.html"],
)
def test_invalid_html_mentions_never_fall_back_to_unique_inventory(env, path):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("A", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([{"calls": [("run_web", {"path": "a.html"})]}])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": f"verify {path}"}],
        enabled_tools=["run_web", "write_code_file"],
        approval_mode="auto",
    )

    assert executions == []
    assert "write_code_file" not in {
        tool["function"]["name"] for tool in fake.payloads[0]["tools"]
    }
    assert any(
        any(
            marker in str(event.get(key, ""))
            for marker in ("확정하지 못했습니다", "허용 대상")
        )
        for event in events for key in ("output", "error", "text")
    )


@pytest.mark.parametrize(
    "reply",
    [
        "yes", "sounds good", "the other one", "another one", "anything else",
        "use the other", "go with another one", "all of them", "both", "either one",
    ],
)
def test_active_validation_reply_cannot_switch_to_a_new_unique_target(env, reply):
    import dataclasses

    import toolspec

    (env.ws / "b.html").write_text("B", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([{"calls": [("run_web", {"path": "b.html"})]}])
    events = env.drive(
        fake,
        approve=True,
        messages=[
            {"role": "user", "content": "verify a.html"},
            {"role": "assistant", "content": "Validation of a.html is pending."},
            {"role": "user", "content": reply},
        ],
        enabled_tools=["run_web", "write_code_file"],
        approval_mode="auto",
    )

    assert executions == []
    assert "write_code_file" not in {
        tool["function"]["name"] for tool in fake.payloads[0]["tools"]
    }
    assert any(
        any(
            marker in str(event.get(key, ""))
            for marker in ("확정하지 못했습니다", "허용 대상")
        )
        for event in events for key in ("output", "error", "text")
    )


@pytest.mark.parametrize(
    "assistant_text",
    [
        "I did not edit a.html. Validation is pending.",
        "I made no changes to a.html. Validation is pending.",
        "No edits were made. Validation of a.html is pending.",
        "a.html was not edited; validation is pending.",
    ],
)
def test_no_edit_reassurance_does_not_break_active_validation_state(assistant_text):
    assert agent._assistant_has_active_validation_state(assistant_text) is True


@pytest.mark.parametrize(
    "path",
    [
        "edit.html", "fix.html", "build.html", "create.html", "write.html",
        "code.html", "update.html", "patch.html", "change.html", "remove.html",
        "delete.html", "move.html", "save.html", "explain.html",
    ],
)
def test_command_word_html_filenames_remain_exact_candidate_selections(path):
    context = f"Multiple HTML validation candidates: {path}, safe.html. Select one."
    assert agent._contains_explicit_mutation_request(path) is False
    assert agent._explicit_html_paths(path) == [path]
    assert agent._looks_like_existing_web_validation_request(path, context) is True


@pytest.mark.parametrize(
    "text",
    [
        "Would you mind validating index.html?",
        "Could you kindly test index.html?",
        "Please could you validate index.html?",
        "I need you to validate index.html.",
        "I want you to validate index.html.",
        "I would like you to validate index.html.",
        "Let's validate index.html.",
        "index.html 검증해줄래?",
        "index.html 테스트해줄 수 있어?",
        "index.html 확인해줄래?",
    ],
)
def test_polite_validation_requests_grant_the_exact_html_target(text):
    assert agent._explicit_html_paths(text) == ["index.html"]
    assert agent._has_explicit_validation_execution_command(text) is True
    assert agent._looks_like_guarded_web_validation_turn(text, "") is True


@pytest.mark.parametrize(
    "text",
    [
        "index.html 검증?",
        "index.html 테스트?",
        "Does index.html work?",
        "Is index.html broken?",
        "Are there any errors in index.html?",
        "index.html이 잘 작동해?",
        "index.html에 문제 없어?",
        "index.html에 오류 있어?",
        "Does the existing HTML work?",
        "Does the web app work?",
        "Is the page broken?",
        "Are there errors in the HTML?",
        "Does the website work?",
        "Is the site broken?",
        "Does the browser page work?",
        "HTML이 잘 작동해?",
        "웹 페이지에 문제 없어?",
    ],
)
def test_html_status_questions_are_guarded_but_never_grant_execution(text):
    assert agent._has_explicit_validation_execution_command(text) is False
    assert agent._explicit_html_paths(text) == []
    assert agent._looks_like_guarded_web_validation_turn(text, "") is True


def test_html_status_question_exposes_no_tools_or_execution(env):
    import dataclasses

    import toolspec

    (env.ws / "index.html").write_text("ORIGINAL", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([{"calls": [("run_web", {"path": "index.html"})]}])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "Does index.html work?"}],
        enabled_tools=["run_web", "write_code_file", "run_command"],
        approval_mode="auto",
    )

    assert fake.payloads[0]["tools"] == []
    assert executions == []
    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    assert (env.ws / "index.html").read_text(encoding="utf-8") == "ORIGINAL"


@pytest.mark.parametrize(
    "text",
    [
        "Search for HTML error handling best practices.",
        "Research common web app issues and summarize them.",
        "Explain how HTML status codes work.",
        "Find JavaScript error monitoring libraries.",
        "Search for HTML validation best practices.",
        "Research web app testing tools.",
        "Find JavaScript testing libraries.",
        "Explain how HTML validation works.",
    ],
)
def test_web_research_topics_are_not_captured_by_local_validation(text):
    assert agent._is_topic_only_web_request(agent._mask_html_path_mentions(text)) is True
    assert agent._looks_like_guarded_web_validation_turn(text, "") is False


@pytest.mark.parametrize(
    "text",
    [
        "Read status.html and summarize its content.",
        "Read errors.html.",
        "Open work.html in the text reader.",
    ],
)
def test_html_filenames_and_source_reading_do_not_trigger_status_validation(text):
    assert agent._looks_like_guarded_web_validation_turn(text, "") is False


@pytest.mark.parametrize(
    ("text", "path"),
    [
        ('Verify "my page.html".', "my page.html"),
        ("Verify `sub dir/index.html`.", "sub dir/index.html"),
        ("Verify 'my page.html'.", "my page.html"),
        ("Verify (my page.html).", "my page.html"),
        ("Verify [my page.html].", "my page.html"),
        ("Verify “my page.html”.", "my page.html"),
        ("Verify foo(1).html.", "foo(1).html"),
        ("Verify foo[1].html.", "foo[1].html"),
        ('Verify "Bob\'s.html".', "Bob's.html"),
        ("Verify foo{draft}.html.", "foo{draft}.html"),
    ],
)
def test_quoted_and_internal_punctuation_html_paths_are_preserved(text, path):
    assert agent._html_path_mentions(text) == [path]
    assert agent._html_path_tokens(text) == [path]
    assert agent._explicit_html_paths(text) == [path]


@pytest.mark.parametrize(
    "text",
    [
        "Test the existing HTML and click the Start button.",
        "Test the existing HTML and press Enter.",
        "Test the existing HTML and type a name into the input.",
        "Test the existing HTML and wait two seconds.",
        "Test the existing HTML and take a screenshot.",
        "Test the existing HTML and scroll down.",
        "Test the existing HTML and assert the score changes.",
        "Test the existing HTML and give me the results.",
        "Test the existing HTML and let me know if it works.",
        "Test the existing HTML and list any errors.",
        "Test the existing HTML and return the findings.",
    ],
)
def test_browser_interactions_and_result_reporting_stay_validation_only(text):
    assert agent._has_additional_operation_after_validation(text) is False
    assert agent._looks_like_existing_web_validation_request(text, "") is True
    assert agent._looks_like_guarded_web_validation_turn(text, "") is True


@pytest.mark.parametrize(
    "text",
    [
        "Yes, search the latest NVIDIA news and summarize it.",
        "Anything else? Search the latest NVIDIA news.",
        "Sure, explain how NVIDIA NIM billing works.",
        "Okay. Start a new research task about NVIDIA.",
        "Read README.md and summarize it.",
        "Open notes.txt and tell me what it contains.",
    ],
)
def test_candidate_acknowledgement_does_not_capture_a_new_task(text):
    context = (
        "[PREVIOUS_USER]\nverify the HTML\n"
        "[PREVIOUS_ASSISTANT]\nHTML validation candidates: a.html, b.html. Select one."
    )
    assert agent._is_bounded_active_validation_reply(text) is False
    assert agent._looks_like_existing_web_validation_request(text, context) is False
    assert agent._looks_like_guarded_web_validation_turn(text, context) is False


@pytest.mark.parametrize(
    "text",
    [
        "Explain it",
        "Summarize it",
        "Compare them",
        "List both",
        "Search those files",
        "Search for it",
        "Summarize its content",
        "Explain what it does",
        "Compare their behavior",
        "Explain it and then edit it",
        "Summarize the candidates and delete them",
        "Search the files and modify them",
        "Compare both and rewrite them",
        "List the candidates, then remove them",
    ],
)
def test_candidate_pronoun_topics_remain_guarded(text):
    context = (
        "[PREVIOUS_USER]\nverify the HTML\n"
        "[PREVIOUS_ASSISTANT]\nHTML validation candidates: a.html, b.html. Select one."
    )
    assert agent._looks_like_guarded_web_validation_turn(text, context) is True


@pytest.mark.parametrize(
    "assistant_text",
    [
        "Python refactor candidates for the HTML parser: parser.py, lexer.py. Select one.",
        "Image candidates for the web page: hero.png, logo.png. Choose one.",
        "TypeScript candidates for run_web integration: bridge.ts, ipc.ts. Select one.",
    ],
)
def test_non_html_candidate_lists_never_create_web_validation_state(assistant_text):
    assert agent._assistant_has_pending_validation_candidates(assistant_text) is False


@pytest.mark.parametrize(
    "user_text",
    [
        "edit it", "fix it", "change the first one", "repair it", "overwrite it",
        "save it", "format it", "refactor it", "optimize it", "build it", "deploy it",
        "apply the changes", "go ahead with the changes", "do the revisions",
        "revise as discussed", "polish accordingly", "perform the edits",
        "execute the modifications", "carry out the modifications",
    ],
)
def test_ambiguous_candidate_mutation_keeps_write_tools_closed(env, user_text):
    (env.ws / "a.html").write_text("A", encoding="utf-8")
    (env.ws / "b.html").write_text("B", encoding="utf-8")
    fake = FakeChat([{
        "calls": [("write_code_file", {"path": "b.html", "content": "CHANGED"})],
    }])
    events = env.run(
        fake,
        messages=[
            {"role": "assistant", "content": "Candidates: a.html, b.html. Select one."},
            {"role": "user", "content": user_text},
        ],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert fake.payloads[0]["tools"] == []
    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "A"
    assert (env.ws / "b.html").read_text(encoding="utf-8") == "B"


@pytest.mark.parametrize(
    "user_text",
    [
        "validate ../secret.html and edit it",
        "validate a.html or b.html and edit it",
    ],
)
def test_ambiguous_validation_target_with_mutation_stays_guarded(user_text):
    assert agent._has_ambiguous_validation_target_for_mutation(user_text) is True
    assert agent._looks_like_guarded_web_validation_turn(user_text, "") is True


@pytest.mark.parametrize(
    "assistant_text",
    [
        "index.html 검증이 중단되었습니다.",
        "검증을 중단했습니다. 대상은 index.html입니다.",
        "Validation of index.html was interrupted.",
    ],
)
def test_reactivated_validation_after_interruption_uses_current_unique_artifact(env, assistant_text):
    import dataclasses

    import toolspec

    (env.ws / "index.html").write_text("ORIGINAL", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([
        {"calls": [("run_web", {"path": "index.html"})]},
        {"content": "검증 결과를 요약했습니다."},
    ])
    events = env.drive(
        fake,
        approve=True,
        messages=[
            {"role": "user", "content": "index.html을 검증해줘"},
            {"role": "assistant", "content": assistant_text},
            {"role": "user", "content": "검증 기능 다시 활성화했어 검증해줘"},
        ],
        enabled_tools=["run_web", "write_code_file", "run_command"],
        approval_mode="auto",
    )

    first_tools = {tool["function"]["name"] for tool in fake.payloads[0]["tools"]}
    assert "run_web" in first_tools
    assert "write_code_file" not in first_tools and "run_command" not in first_tools
    assert executions == ["index.html"]
    assert fake.payloads[1]["tools"] == []
    assert not any(event["type"] == "error" for event in events)
    assert (env.ws / "index.html").read_text(encoding="utf-8") == "ORIGINAL"


@pytest.mark.parametrize("status", ["PASS", "FAIL", "INCONCLUSIVE"])
def test_terminal_web_validation_status_disables_repeat_execution(env, status):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("A", encoding="utf-8")
    executions: list[int] = []

    async def fake_run_web(root, **args):
        executions.append(int(args["steps"][0]["ms"]))
        return f"[WEB_VALIDATION v1]\nstatus={status} level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([
        {"calls": [("run_web", {"path": "a.html", "steps": [{"action": "wait", "ms": 1}]})]},
        {"calls": [("run_web", {"path": "a.html", "steps": [{"action": "wait", "ms": 2}]})]},
    ])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "verify a.html"}],
        enabled_tools=["run_web"],
        approval_mode="auto",
    )

    assert executions == [1]
    assert fake.payloads[1]["tools"] == []
    assert sum(event["type"] == "tool_call" for event in events) == 1


def test_duplicate_run_web_batch_is_rejected_before_execution(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("A", encoding="utf-8")
    executions: list[int] = []

    async def fake_run_web(root, **args):
        executions.append(1)
        return "[WEB_VALIDATION v1]\nstatus=FAIL level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([{
        "calls": [
            ("run_web", {"path": "a.html", "steps": [{"action": "wait", "ms": 1}]}),
            ("run_web", {"path": "a.html", "steps": [{"action": "wait", "ms": 2}]}),
        ],
    }])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "verify a.html"}],
        enabled_tools=["run_web"],
        approval_mode="auto",
    )

    assert executions == []
    assert not any(event["type"] == "tool_call" for event in events)
    assert any("반복" in event.get("text", "") for event in events)


@pytest.mark.parametrize("status", ["PASS", "FAIL", "INCONCLUSIVE"])
def test_authored_html_terminal_validation_cannot_repeat_without_revision(env, status):
    import dataclasses

    import toolspec

    executions: list[int] = []

    async def fake_run_web(root, **args):
        executions.append(int(args["steps"][0]["ms"]))
        return f"[WEB_VALIDATION v1]\nstatus={status} level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([
        {"calls": [("write_code_file", {"path": "a.html", "content": "<button>A</button>"})]},
        {"calls": [("run_web", {"path": "a.html", "steps": [{"action": "wait", "ms": 1}]})]},
        {"calls": [("run_web", {"path": "a.html", "steps": [{"action": "wait", "ms": 2}]})]},
    ])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "Create a web page in a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == [1]
    assert (env.ws / "a.html").exists()
    assert any("검증 반복" in event.get("text", "") for event in events)


def test_authored_html_allows_one_revision_retest_but_blocks_a_third_run(env):
    import dataclasses

    import toolspec

    executions: list[int] = []

    async def fake_run_web(root, **args):
        executions.append(int(args["steps"][0]["ms"]))
        status = "FAIL" if len(executions) == 1 else "PASS"
        return f"[WEB_VALIDATION v1]\nstatus={status} level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([
        {"calls": [("write_code_file", {"path": "a.html", "content": "v1"})]},
        {"calls": [("run_web", {"path": "a.html", "steps": [{"action": "wait", "ms": 1}]})]},
        {"calls": [("write_code_file", {"path": "a.html", "content": "v2"})]},
        {"calls": [("run_web", {"path": "a.html", "steps": [{"action": "wait", "ms": 2}]})]},
        {"calls": [("run_web", {"path": "a.html", "steps": [{"action": "wait", "ms": 3}]})]},
    ])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "Create and validate a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == [1, 2]
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "v2"
    assert any("검증 반복" in event.get("text", "") for event in events)


def test_authored_html_validation_rejects_unrelated_target_before_any_batch_action(env):
    import dataclasses

    import toolspec

    (env.ws / "b.html").write_text("B", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    fake = FakeChat([{
        "calls": [
            ("write_code_file", {"path": "a.html", "content": "A"}),
            ("run_web", {"path": "a.html"}),
            ("run_web", {"path": "b.html"}),
        ],
    }])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "Create and validate a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == []
    assert not (env.ws / "a.html").exists()
    assert (env.ws / "b.html").read_text(encoding="utf-8") == "B"
    assert not any(event["type"] == "tool_call" for event in events)
    assert any("검증 범위 밖" in event.get("text", "") for event in events)


@pytest.mark.parametrize(
    "text",
    [
        "redo", "rework", "tweak", "alter", "amend", "revamp", "correct",
        "adjust", "clean up", "touch up", "continue the work", "do the work",
        "Which should I choose?", "Which is better?", "What do you recommend?",
        "What should I pick?", "How should I decide?", "Why not choose automatically?",
    ],
)
def test_pending_candidates_fail_closed_for_ambiguous_open_vocabulary(text):
    context = (
        "[PREVIOUS_USER]\nverify the HTML\n"
        "[PREVIOUS_ASSISTANT]\nHTML validation candidates: a.html, b.html. Select one."
    )
    assert agent._looks_like_guarded_web_validation_turn(text, context) is True


@pytest.mark.parametrize(
    "text",
    [
        "What's the weather in Seoul?",
        "Give me today's weather",
        "Look up EUR/KRW exchange rate",
        "Who is NVIDIA's CEO?",
        "Set an alarm for 5 seconds",
        "Send a Discord message to the team",
        "Calculate 123 * 456",
        "Create a Tetris game",
    ],
)
def test_pending_candidates_release_clear_independent_tasks(text):
    context = (
        "[PREVIOUS_USER]\nverify the HTML\n"
        "[PREVIOUS_ASSISTANT]\nHTML validation candidates: a.html, b.html. Select one."
    )
    assert agent._looks_like_guarded_web_validation_turn(text, context) is False


@pytest.mark.parametrize(
    "text",
    [
        "Edit screenshot.png and save it.",
        "Modify game.cs.",
        "Update LICENSE.",
        "Delete the docs folder.",
    ],
)
def test_pending_candidates_release_explicit_new_non_html_mutations(text):
    context = (
        "[PREVIOUS_USER]\nverify the HTML\n"
        "[PREVIOUS_ASSISTANT]\nHTML validation candidates: a.html, b.html. Select one."
    )
    assert agent._has_explicit_non_html_file_target(text) is True
    assert agent._is_ambiguous_candidate_mutation_reply(text) is False
    assert agent._looks_like_guarded_web_validation_turn(text, context) is False


@pytest.mark.parametrize(
    ("text", "expected_paths", "continued"),
    [
        ("Web validation is re-enabled. Continue.", [], True),
        ("I re-enabled the validator. Please continue.", [], True),
        ("Validation is enabled again; resume.", [], True),
        ("turned validation back on; proceed.", [], True),
        ("Web validation is re-enabled; verify b.html.", ["b.html"], False),
        ("Re-enabled web validation. Please validate b.html.", ["b.html"], False),
        ("검증 기능 다시 활성화했어 b.html을 검증해줘", ["b.html"], False),
    ],
)
def test_reactivation_composite_commands_stay_validation_only(text, expected_paths, continued):
    context = (
        "[PREVIOUS_USER]\nverify b.html\n"
        "[PREVIOUS_ASSISTANT]\nValidation of b.html was interrupted."
    )
    assert agent._is_validation_feature_reactivation_request(text) is True
    assert agent._has_validation_reactivation_continuation(text) is continued
    assert agent._explicit_html_paths(text) == expected_paths
    assert agent._looks_like_guarded_web_validation_turn(text, context) is True


def test_normal_authoring_rejects_invalid_run_web_targets_before_execution(env):
    import dataclasses

    import toolspec

    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(str(args.get("path")))
        raise AssertionError("invalid run_web target must not execute")

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    calls = [("write_code_file", {"path": "a.html", "content": "A"})]
    calls.extend(("run_web", {"path": f"not{index}.txt"}) for index in range(8))
    events = env.drive(
        FakeChat([{"calls": calls}]),
        approve=True,
        messages=[{"role": "user", "content": "Create and validate a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == []
    assert not (env.ws / "a.html").exists()
    assert not any(event["type"] == "tool_call" for event in events)


def test_normal_authoring_rechecks_scope_after_a_failed_edit(env):
    import dataclasses

    import toolspec

    (env.ws / "b.html").write_text("B", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "a.html", "content": "A"}),
                ("edit_code_file", {
                    "path": "b.html", "old_string": "NOT_FOUND", "new_string": "CHANGED",
                }),
                ("run_web", {"path": "b.html"}),
            ],
        }]),
        approve=True,
        messages=[{"role": "user", "content": "Create a.html."}],
        enabled_tools=["write_code_file", "edit_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == []
    assert (env.ws / "b.html").read_text(encoding="utf-8") == "B"
    assert any("실제로 작성·수정에 성공" in event.get("output", "") for event in events)


def test_windows_case_alias_cannot_bypass_terminal_validation_or_leave_pending(env):
    import dataclasses
    import os

    import toolspec

    if os.name != "nt":
        pytest.skip("Windows path alias contract")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([
            {"calls": [("write_code_file", {"path": "a.html", "content": "A"})]},
            {"calls": [("run_web", {"path": "A.html"})]},
            {"calls": [("run_web", {"path": "a.HTML"})]},
        ]),
        approve=True,
        messages=[{"role": "user", "content": "Create and validate a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == ["A.html"]
    assert not any("웹 검증 미완료" in event.get("text", "") for event in events)
    assert any("검증 반복" in event.get("text", "") for event in events)


@pytest.mark.parametrize(
    "text",
    [
        "Create a.html and b.html, then validate both.",
        "Edit a.html and b.html, then test both.",
        "Create a.html using b.html as reference, then validate a.html.",
    ],
)
def test_clear_multi_html_authoring_is_not_forced_into_validation_only(text):
    assert agent._is_clear_multi_html_authoring_request(text) is True
    assert agent._has_ambiguous_validation_target_for_mutation(text) is False
    assert agent._looks_like_guarded_web_validation_turn(text, "") is False


@pytest.mark.parametrize(
    "text",
    [
        "Explain the selected",
        "Summarize the chosen",
        "Compare the available",
        "List the remaining",
        "Recommend the preferred version",
        "Explain the above",
        "Summarize the aforementioned",
        "Find the suitable version",
        "Compare whichever is available",
        "Explain our selection",
    ],
)
def test_pending_candidate_generic_topic_nouns_are_not_context_switches(text):
    context = (
        "[PREVIOUS_USER]\nverify the HTML\n"
        "[PREVIOUS_ASSISTANT]\nHTML validation candidates: a.html, b.html. Select one."
    )
    assert agent._is_topic_only_web_request(text) is False
    assert agent._looks_like_guarded_web_validation_turn(text, context) is True


@pytest.mark.parametrize(
    "text",
    [
        "a.html을 만들어서 테스트했던 과정을 설명해줘",
        "a.html을 만들고 검증하는 방법을 알려줘",
        "a.html을 고치고 테스트했다는 기록을 요약해줘",
    ],
)
def test_korean_meta_discussion_does_not_grant_mutation(text):
    masked = agent._mask_html_path_mentions(text)
    assert agent._contains_explicit_mutation_request(masked) is False


def test_candidate_meta_discussion_cannot_overwrite_existing_artifact(env):
    (env.ws / "a.html").write_text("ORIGINAL", encoding="utf-8")
    messages = [
        {"role": "user", "content": "HTML을 검증해줘"},
        {
            "role": "assistant",
            "content": "HTML validation candidates: a.html, b.html. Select one.",
        },
        {
            "role": "user",
            "content": "a.html을 만들어서 테스트했던 과정을 설명해줘",
        },
    ]
    events = env.drive(
        FakeChat([{
            "calls": [("write_code_file", {"path": "a.html", "content": "OVERWRITTEN"})],
        }]),
        approve=True,
        messages=messages,
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert (env.ws / "a.html").read_text(encoding="utf-8") == "ORIGINAL"
    assert not any(event["type"] == "tool_call" for event in events)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Create a.html and b.html, then validate a.html.", ["a.html"]),
        ("Create a.html and b.html, then validate only a.html.", ["a.html"]),
        ("Edit a.html and b.html, then validate b.html.", ["b.html"]),
        ("Create a.html based on b.html, then validate a.html.", ["a.html"]),
        ("Use b.html as a template to create a.html, then validate a.html.", ["a.html"]),
        ("Read b.html, create a.html from it, then validate a.html.", ["a.html"]),
        ("a.html과 b.html을 만들고 둘 다 검증해줘", ["a.html", "b.html"]),
        ("a.html과 b.html을 만들고 a.html만 검증해줘", ["a.html"]),
        ("b.html을 참고해서 a.html을 만들고 a.html을 검증해줘", ["a.html"]),
    ],
)
def test_multi_html_authoring_extracts_only_requested_validation_outputs(text, expected):
    assert agent._is_clear_multi_html_authoring_request(text) is True
    assert agent._normal_requested_validation_paths(text) == expected
    assert agent._looks_like_guarded_web_validation_turn(text, "") is False


@pytest.mark.parametrize(
    "text",
    [
        "I've re-enabled web validation. Validate b.html.",
        "We re-enabled web validation; please verify b.html.",
        "The validator is back on. Test b.html.",
        "Web validation is back on; run b.html.",
    ],
)
def test_reactivation_variants_preserve_explicit_target(text):
    assert agent._is_validation_feature_reactivation_request(text) is True
    assert agent._explicit_html_paths(text) == ["b.html"]


@pytest.mark.parametrize(
    "text",
    [
        "What is CUDA?",
        "How does NVIDIA NIM work?",
        "Who is Jensen Huang?",
        "Where is NVIDIA headquartered?",
        "When was CUDA released?",
        "Why use CUDA?",
        "What is latest NVIDIA GPU?",
    ],
)
def test_pending_candidates_release_concrete_external_questions(text):
    context = (
        "[PREVIOUS_USER]\nverify the HTML\n"
        "[PREVIOUS_ASSISTANT]\nHTML validation candidates: a.html, b.html. Select one."
    )
    assert agent._looks_like_guarded_web_validation_turn(text, context) is False


@pytest.mark.parametrize(
    "text",
    [
        "Create a.html without running validation.",
        "Edit a.html without testing it.",
        "Create a.html without browser testing.",
        "Write a.html, no web test.",
        "Create a.html; avoid browser checks.",
        "Create a.html; leave validation off.",
        "Create a.html; browser testing is disabled.",
        "Create a.html; don't use run_web.",
        "Create a.html; no need for a browser check.",
        "Create a.html; do not execute the web test.",
        "Create a.html; hold off on testing.",
        "a.html을 만들어줘. 검증하지 마",
        "a.html을 만들고 검증은 하지 마",
    ],
)
def test_authoring_with_validation_denial_keeps_writes_but_hides_run_web(text):
    masked = agent._mask_html_path_mentions(text)
    assert agent._contains_explicit_mutation_request(masked) is True
    assert agent._is_nonexecuting_web_validation_statement(masked) is True
    assert agent._looks_like_guarded_web_validation_turn(text, "") is False


def test_create_and_validate_existing_file_blocks_run_before_mutation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("OLD", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                ("run_web", {"path": "a.html"}),
                ("write_code_file", {"path": "a.html", "content": "NEW"}),
            ],
        }]),
        approve=True,
        messages=[{"role": "user", "content": "Create and validate a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == []
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "OLD"
    assert not any(event["type"] == "tool_call" for event in events)


def test_create_and_validate_existing_file_runs_only_after_successful_mutation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("OLD", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "a.html", "content": "NEW"}),
                ("run_web", {"path": "a.html"}),
            ],
        }, {"content": "완료"}]),
        approve=True,
        messages=[{"role": "user", "content": "Create and validate a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == ["NEW"]
    assert not any("웹 검증 미완료" in event.get("text", "") for event in events)


def test_post_validation_dependency_mutation_invalidates_previous_pass(env):
    import dataclasses

    import toolspec

    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([
            {"calls": [("write_code_file", {"path": "a.html", "content": "<script src='app.js'></script>"})]},
            {"calls": [("run_web", {"path": "a.html"})]},
            {"calls": [("write_code_file", {"path": "app.js", "content": "window.v = 2"})]},
            {"content": "완료"},
        ]),
        approve=True,
        messages=[{"role": "user", "content": "Create and validate a.html with app.js."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == ["a.html"]
    assert any("웹 검증 미완료" in event.get("text", "") for event in events)


def test_reused_successful_write_restores_deferred_validation_scope(env, tmp_path):
    import dataclasses

    import toolspec
    from agent_ledger import AgentExecutionLedger

    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append(args["path"])
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    common = dict(
        assistant_turn_id="resume-write-turn",
        approval_mode="auto",
    )
    write_call = ("write_code_file", {"path": "a.html", "content": "A"})
    with AgentExecutionLedger(tmp_path / "resume-write.sqlite3") as ledger:
        env.drive(
            FakeChat([{"calls": [write_call]}, {"content": "완료"}]),
            approve=True,
            messages=[{"role": "user", "content": "Create a.html."}],
            enabled_tools=["write_code_file"],
            execution_ledger=ledger,
            **common,
        )
        second = env.drive(
            FakeChat([
                {"calls": [write_call]},
                {"calls": [("run_web", {"path": "a.html"})]},
                {"content": "완료"},
            ]),
            approve=True,
            messages=[{"role": "user", "content": "Create and validate a.html."}],
            enabled_tools=["write_code_file", "run_web"],
            execution_ledger=ledger,
            **common,
        )

    assert executions == ["a.html"]
    assert any(event.get("reused") is True for event in second if event["type"] == "tool_result")


def test_reused_run_web_revalidates_current_file_instead_of_reusing_stale_pass(env, tmp_path):
    import dataclasses

    import toolspec
    from agent_ledger import AgentExecutionLedger

    (env.ws / "a.html").write_text("V1", encoding="utf-8")
    observed: list[str] = []

    async def fake_run_web(root, **args):
        observed.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    common = dict(
        messages=[{"role": "user", "content": "Validate a.html."}],
        enabled_tools=["run_web"],
        approval_mode="auto",
        assistant_turn_id="resume-validation-turn",
    )
    script = [{"calls": [("run_web", {"path": "a.html"})]}, {"content": "완료"}]
    with AgentExecutionLedger(tmp_path / "resume-validation.sqlite3") as ledger:
        first = env.drive(FakeChat(script), approve=True, execution_ledger=ledger, **common)
        (env.ws / "a.html").write_text("V2", encoding="utf-8")
        second = env.drive(FakeChat(script), approve=True, execution_ledger=ledger, **common)
        ledger_rows = ledger._db.execute(
            "SELECT COUNT(*) FROM tool_execution_ledger"
        ).fetchone()[0]

    assert observed == ["V1", "V2"]
    run_result = next(event for event in second if event["type"] == "tool_result")
    assert run_result.get("reused") is not True
    first_call = next(event for event in first if event["type"] == "tool_call")
    second_call = next(event for event in second if event["type"] == "tool_call")
    assert first_call["executionId"] != second_call["executionId"]
    assert first_call["approvalId"] != second_call["approvalId"]
    assert ledger_rows == 2


def test_repeating_identical_two_call_mutation_batch_stops_early(env):
    calls = [
        ("write_code_file", {"path": "a.txt", "content": "A"}),
        ("write_code_file", {"path": "b.txt", "content": "B"}),
    ]
    fake = FakeChat([{"calls": calls}])
    events = env.drive(
        fake,
        approve=True,
        messages=[{"role": "user", "content": "Update a.txt and b.txt."}],
        enabled_tools=["write_code_file"],
        approval_mode="auto",
    )

    assert fake.calls == agent.IDENTICAL_TOOL_BATCH_LIMIT + 1
    assert len([event for event in events if event["type"] == "tool_call"]) == 4
    # 판정은 문구가 아니라 이벤트로 한다 — 안내문은 다듬을 수 있어야 한다.
    assert any(
        event.get("type") == "run_limit" and event.get("reason") == "tool_budget"
        for event in events
    )
    assert any(
        event.get("type") == "notice" and "작업 묶음" in event.get("text", "")
        for event in events
    ), "사람에게도 무엇이 반복됐는지 알려야 한다"


def test_noop_write_does_not_unlock_existing_html_validation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("OLD", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "a.html", "content": "OLD"}),
                ("run_web", {"path": "a.html"}),
            ],
        }]),
        approve=True,
        messages=[{"role": "user", "content": "Edit and validate a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == []
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "OLD"
    assert any("[NO_CHANGE]" in event.get("output", "") for event in events)


def test_net_noop_multi_edit_does_not_unlock_existing_html_validation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("A", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                (
                    "multi_edit_code_file",
                    {
                        "path": "a.html",
                        "edits": [
                            {"old_string": "A", "new_string": "B"},
                            {"old_string": "B", "new_string": "A"},
                        ],
                    },
                ),
                ("run_web", {"path": "a.html"}),
            ],
        }]),
        approve=True,
        messages=[{"role": "user", "content": "Edit and validate a.html."}],
        enabled_tools=["multi_edit_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == []
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "A"
    assert any("[NO_CHANGE]" in event.get("output", "") for event in events)


def test_unrelated_mutation_in_prior_turn_does_not_unlock_deferred_html(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("OLD", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([
            {"calls": [("write_code_file", {"path": "notes.txt", "content": "note"})]},
            {"calls": [("run_web", {"path": "a.html"})]},
            {"calls": [("write_code_file", {"path": "a.html", "content": "NEW"})]},
        ]),
        approve=True,
        messages=[{"role": "user", "content": "Create and validate a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == []
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "OLD"
    assert (env.ws / "notes.txt").read_text(encoding="utf-8") == "note"
    assert any("검증 범위 밖" in event.get("text", "") for event in events)


def test_multi_target_deferred_scope_is_not_cross_unlocked(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("A0", encoding="utf-8")
    (env.ws / "b.html").write_text("B0", encoding="utf-8")
    executions: list[str] = []

    async def fake_run_web(root, **args):
        executions.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "a.html", "content": "A1"}),
                ("run_web", {"path": "b.html"}),
            ],
        }]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Edit a.html and b.html, then validate both.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == []
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "A0"
    assert (env.ws / "b.html").read_text(encoding="utf-8") == "B0"
    assert not any(event["type"] == "tool_call" for event in events)


def test_multi_target_each_runs_only_after_its_own_mutation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("A0", encoding="utf-8")
    (env.ws / "b.html").write_text("B0", encoding="utf-8")
    executions: list[tuple[str, str]] = []

    async def fake_run_web(root, **args):
        executions.append((args["path"], (root / args["path"]).read_text(encoding="utf-8")))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "a.html", "content": "A1"}),
                ("run_web", {"path": "a.html"}),
                ("write_code_file", {"path": "b.html", "content": "B1"}),
                ("run_web", {"path": "b.html"}),
            ],
        }, {"content": "완료"}]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Edit a.html and b.html, then validate both.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert executions == [("a.html", "A1"), ("b.html", "B1")]
    assert not any("웹 검증 미완료" in event.get("text", "") for event in events)


def test_named_dependency_mutation_can_unlock_requested_html(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("<script src='app.js'></script>", encoding="utf-8")
    (env.ws / "app.js").write_text("window.v = 1", encoding="utf-8")
    observed: list[str] = []

    async def fake_run_web(root, **args):
        observed.append((root / "app.js").read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "app.js", "content": "window.v = 2"}),
                ("run_web", {"path": "a.html"}),
            ],
        }, {"content": "완료"}]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Edit app.js, then validate a.html.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert observed == ["window.v = 2"]


def test_dependency_cannot_unlock_html_that_request_says_to_create(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("OLD", encoding="utf-8")
    observed: list[str] = []

    async def fake_run_web(root, **args):
        observed.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([
            {"calls": [("write_code_file", {"path": "app.js", "content": "window.v = 2"})]},
            {"calls": [("run_web", {"path": "a.html"})]},
            {"calls": [("write_code_file", {"path": "a.html", "content": "NEW"})]},
        ]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Create and validate a.html with app.js.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert agent._request_directly_mutates_html_path(
        "Create and validate a.html with app.js.", "a.html"
    ) is True
    assert observed == []
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "OLD"
    assert any("검증 범위 밖" in event.get("text", "") for event in events)


@pytest.mark.parametrize(
    ("text", "paths"),
    [
        ("For a.html, create a button using app.js, then validate a.html.", ["a.html"]),
        ("In a.html implement a button using app.js, then validate a.html.", ["a.html"]),
        ("Within a.html, fix the layout using app.js, then validate a.html.", ["a.html"]),
        ("Inside a.html, update the UI using app.js, then validate a.html.", ["a.html"]),
        ("Regarding a.html, implement the feature using app.js, then validate a.html.", ["a.html"]),
        (
            "In a.html and b.html, implement changes using shared.js, then validate both.",
            ["a.html", "b.html"],
        ),
        ("Validate a.html after editing it and app.js.", ["a.html"]),
    ],
)
def test_path_before_mutation_forms_require_direct_html_change(text, paths):
    for path in paths:
        assert agent._request_directly_mutates_html_path(text, path) is True


@pytest.mark.parametrize(
    "text",
    [
        "a.html uses app.js. Edit app.js, then validate a.html.",
        "For a.html that uses app.js, edit app.js, then validate a.html.",
        "In a.html, update app.js and validate a.html.",
        "The entry is a.html. Update app.js, then validate it.",
    ],
)
def test_dependency_only_word_order_does_not_require_html_change(text):
    assert agent._request_directly_mutates_html_path(text, "a.html") is False


def test_validate_after_editing_it_cannot_run_old_html_in_a_later_turn(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("OLD", encoding="utf-8")
    (env.ws / "app.js").write_text("J0", encoding="utf-8")
    observed: list[str] = []

    async def fake_run_web(root, **args):
        observed.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([
            {"calls": [("write_code_file", {"path": "app.js", "content": "J1"})]},
            {"calls": [("run_web", {"path": "a.html"})]},
        ]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Validate a.html after editing it and app.js.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert observed == []
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "OLD"
    assert any("검증 범위 밖" in event.get("text", "") for event in events)


def test_direct_html_change_then_revert_cannot_validate_task_start_bytes(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("OLD", encoding="utf-8")
    observed: list[str] = []

    async def fake_run_web(root, **args):
        observed.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "a.html", "content": "NEW"}),
                ("write_code_file", {"path": "a.html", "content": "OLD"}),
                ("run_web", {"path": "a.html"}),
            ],
        }]),
        approve=True,
        messages=[{"role": "user", "content": "Edit and validate a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert observed == []
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "OLD"
    assert any("작업 시작 시점과 동일" in event.get("output", "") for event in events)


def test_dependency_change_then_revert_cannot_validate_old_dependency(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("<script src='app.js'></script>", encoding="utf-8")
    (env.ws / "app.js").write_text("OLD", encoding="utf-8")
    observed: list[str] = []

    async def fake_run_web(root, **args):
        observed.append((root / "app.js").read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "app.js", "content": "NEW"}),
                ("write_code_file", {"path": "app.js", "content": "OLD"}),
                ("run_web", {"path": "a.html"}),
            ],
        }]),
        approve=True,
        messages=[{"role": "user", "content": "Edit app.js, then validate a.html."}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert observed == []
    assert any("의존 파일의 최종 내용" in event.get("output", "") for event in events)


def test_stale_reused_write_cannot_unlock_validation(env, tmp_path):
    import dataclasses

    import toolspec
    from agent_ledger import AgentExecutionLedger

    observed: list[str] = []

    async def fake_run_web(root, **args):
        observed.append((root / args["path"]).read_text(encoding="utf-8"))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    write_call = ("write_code_file", {"path": "a.html", "content": "NEW"})
    common = dict(assistant_turn_id="stale-write-turn", approval_mode="auto")
    with AgentExecutionLedger(tmp_path / "stale-write.sqlite3") as ledger:
        env.drive(
            FakeChat([{"calls": [write_call]}, {"content": "완료"}]),
            approve=True,
            messages=[{"role": "user", "content": "Create a.html."}],
            enabled_tools=["write_code_file"],
            execution_ledger=ledger,
            **common,
        )
        (env.ws / "a.html").write_text("EXTERNAL", encoding="utf-8")
        second = env.drive(
            FakeChat([
                {"calls": [write_call]},
                {"calls": [("run_web", {"path": "a.html"})]},
            ]),
            approve=True,
            messages=[{"role": "user", "content": "Create and validate a.html."}],
            enabled_tools=["write_code_file", "run_web"],
            execution_ledger=ledger,
            **common,
        )

    assert observed == []
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "EXTERNAL"
    stale = next(
        event for event in second
        if event["type"] == "tool_result" and "[STALE]" in event.get("output", "")
    )
    assert stale["ok"] is False and stale["reused"] is True


def test_dependency_mutation_targets_exclude_unchanged_mentions():
    text = "Edit app.js, leave style.css unchanged, then validate a.html."
    assert agent._request_directly_mutates_dependency_path(text, "app.js") is True
    assert agent._request_directly_mutates_dependency_path(text, "style.css") is False


@pytest.mark.parametrize(
    "text",
    [
        "a.html uses app.js. Edit app.js, then validate a.html.",
        "`a.html` uses `app.js`. Edit only `app.js`, then validate `a.html`.",
        '"a.html" uses "app.js". Edit "app.js", then validate "a.html".',
        "After editing app.js, validate a.html.",
        "Validate a.html after editing app.js.",
    ],
)
def test_dependency_only_variants_identify_dependency_not_html(text):
    assert agent._request_directly_mutates_html_path(text, "a.html") is False
    assert agent._request_directly_mutates_dependency_path(text, "app.js") is True


def test_all_named_dependency_mutations_are_required_before_validation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text(
        "<script src='app.js'></script><link rel='stylesheet' href='style.css'>",
        encoding="utf-8",
    )
    (env.ws / "app.js").write_text("J0", encoding="utf-8")
    (env.ws / "style.css").write_text("C0", encoding="utf-8")
    observed: list[tuple[str, str]] = []

    async def fake_run_web(root, **args):
        observed.append((
            (root / "app.js").read_text(encoding="utf-8"),
            (root / "style.css").read_text(encoding="utf-8"),
        ))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "app.js", "content": "J1"}),
                ("run_web", {"path": "a.html"}),
            ],
        }]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Edit app.js and style.css, then validate a.html.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert observed == []
    assert (env.ws / "app.js").read_text(encoding="utf-8") == "J0"
    assert not any(event["type"] == "tool_call" for event in events)


def test_all_named_dependencies_changed_allows_validation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("A", encoding="utf-8")
    (env.ws / "app.js").write_text("J0", encoding="utf-8")
    (env.ws / "style.css").write_text("C0", encoding="utf-8")
    observed: list[tuple[str, str]] = []

    async def fake_run_web(root, **args):
        observed.append((
            (root / "app.js").read_text(encoding="utf-8"),
            (root / "style.css").read_text(encoding="utf-8"),
        ))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "app.js", "content": "J1"}),
                ("write_code_file", {"path": "style.css", "content": "C1"}),
                ("run_web", {"path": "a.html"}),
            ],
        }, {"content": "완료"}]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Edit app.js and style.css, then validate a.html.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert observed == [("J1", "C1")]


@pytest.mark.parametrize(
    "text",
    [
        "After editing app.js, validate a.html.",
        "Validate a.html after editing app.js.",
    ],
)
def test_dependency_gerund_requests_expose_mutation_and_validation(text):
    masked = agent._mask_html_path_mentions(text)
    assert agent._contains_explicit_mutation_request(masked) is True
    assert agent._looks_like_guarded_web_validation_turn(text, "") is False


def test_direct_html_and_dependency_must_both_change_before_same_batch_validation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("A0", encoding="utf-8")
    (env.ws / "app.js").write_text("J0", encoding="utf-8")
    observed: list[tuple[str, str]] = []

    async def fake_run_web(root, **args):
        observed.append((
            (root / args["path"]).read_text(encoding="utf-8"),
            (root / "app.js").read_text(encoding="utf-8"),
        ))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "a.html", "content": "A1"}),
                ("run_web", {"path": "a.html"}),
            ],
        }]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Edit a.html and app.js, then validate a.html.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert observed == []
    assert (env.ws / "a.html").read_text(encoding="utf-8") == "A0"
    assert (env.ws / "app.js").read_text(encoding="utf-8") == "J0"
    assert not any(event["type"] == "tool_call" for event in events)


def test_direct_html_and_dependency_both_changed_allows_validation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("A0", encoding="utf-8")
    (env.ws / "app.js").write_text("J0", encoding="utf-8")
    observed: list[tuple[str, str]] = []

    async def fake_run_web(root, **args):
        observed.append((
            (root / args["path"]).read_text(encoding="utf-8"),
            (root / "app.js").read_text(encoding="utf-8"),
        ))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "a.html", "content": "A1"}),
                ("write_code_file", {"path": "app.js", "content": "J1"}),
                ("run_web", {"path": "a.html"}),
            ],
        }, {"content": "완료"}]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Edit a.html and app.js, then validate a.html.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert observed == [("A1", "J1")]


def test_renamed_dependency_tracks_source_and_destination_before_validation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("<script src='main.js'></script>", encoding="utf-8")
    (env.ws / "app.js").write_text("J0", encoding="utf-8")
    observed: list[tuple[bool, str]] = []

    async def fake_run_web(root, **args):
        observed.append((
            (root / "app.js").exists(),
            (root / "main.js").read_text(encoding="utf-8"),
        ))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    env.drive(
        FakeChat([{
            "calls": [
                ("move", {"src": "app.js", "dst": "main.js"}),
                ("run_web", {"path": "a.html"}),
            ],
        }, {"content": "완료"}]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Rename app.js to main.js, then validate a.html.",
        }],
        enabled_tools=["move", "run_web"],
        approval_mode="auto",
    )

    assert observed == [(False, "J0")]


def test_explicit_no_edit_path_blocks_entire_mutation_batch(env):
    (env.ws / "a.html").write_text("A0", encoding="utf-8")
    (env.ws / "app.js").write_text("J0", encoding="utf-8")

    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "a.html", "content": "A1"}),
                ("write_code_file", {"path": "app.js", "content": "J1"}),
                ("run_web", {"path": "a.html"}),
            ],
        }]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Do not edit a.html; edit app.js, then validate a.html.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert (env.ws / "a.html").read_text(encoding="utf-8") == "A0"
    assert (env.ws / "app.js").read_text(encoding="utf-8") == "J0"
    assert not any(event["type"] == "tool_call" for event in events)
    assert any("수정하지 말라고" in event.get("text", "") for event in events)


def test_missing_no_edit_path_cannot_be_created_through_windows_case_alias(env):
    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "A.HTML", "content": "A1"}),
                ("write_code_file", {"path": "app.js", "content": "J1"}),
                ("run_web", {"path": "a.html"}),
            ],
        }]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Do not edit a.html; edit app.js, then validate a.html.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert not (env.ws / "a.html").exists()
    assert not (env.ws / "app.js").exists()
    assert not any(event["type"] == "tool_call" for event in events)


def test_no_edit_path_cannot_be_created_by_moving_into_existing_directory(env):
    (env.ws / "src").mkdir()
    (env.ws / "src" / "a.html").write_text("A0", encoding="utf-8")
    (env.ws / "backup").mkdir()

    events = env.drive(
        FakeChat([{
            "calls": [
                ("move", {"src": "src/a.html", "dst": "backup"}),
                ("run_web", {"path": "backup/a.html"}),
            ],
        }]),
        approve=True,
        messages=[{
            "role": "user",
            "content": (
                "Do not create backup/a.html; move src/a.html to backup, "
                "then validate backup/a.html."
            ),
        }],
        enabled_tools=["move", "run_web"],
        approval_mode="auto",
    )

    assert (env.ws / "src" / "a.html").read_text(encoding="utf-8") == "A0"
    assert not (env.ws / "backup" / "a.html").exists()
    assert not any(event["type"] == "tool_call" for event in events)


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        ("delete_dir", {"path": "src"}),
        ("move", {"src": "src", "dst": "backup"}),
    ],
)
def test_no_edit_descendant_is_protected_from_parent_directory_mutation(
    env, tool_name, tool_args
):
    (env.ws / "src").mkdir()
    (env.ws / "src" / "a.html").write_text("A0", encoding="utf-8")

    events = env.drive(
        FakeChat([{"calls": [(tool_name, tool_args)]}]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Keep src/a.html unchanged; edit app.js, then validate src/a.html.",
        }],
        enabled_tools=[tool_name, "write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert (env.ws / "src" / "a.html").read_text(encoding="utf-8") == "A0"
    assert not any(event["type"] == "tool_call" for event in events)


def test_no_edit_descendant_is_protected_through_directory_symlink_alias(env):
    real = env.ws / "real"
    real.mkdir()
    protected = real / "a.html"
    protected.write_text("A0", encoding="utf-8")
    alias = env.ws / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink is unavailable: {exc}")

    events = env.drive(
        FakeChat([{"calls": [("delete_dir", {"path": "alias"})]}]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Keep real/a.html unchanged; edit app.js, then validate real/a.html.",
        }],
        enabled_tools=["delete_dir", "write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert protected.read_text(encoding="utf-8") == "A0"
    assert not any(event["type"] == "tool_call" for event in events)


def test_explicit_no_edit_html_allows_named_dependency_then_validation(env):
    import dataclasses

    import toolspec

    (env.ws / "a.html").write_text("A0", encoding="utf-8")
    (env.ws / "app.js").write_text("J0", encoding="utf-8")
    observed: list[tuple[str, str]] = []

    async def fake_run_web(root, **args):
        observed.append((
            (root / args["path"]).read_text(encoding="utf-8"),
            (root / "app.js").read_text(encoding="utf-8"),
        ))
        return "[WEB_VALIDATION v1]\nstatus=PASS level=runtime", None

    env.mp.setitem(
        toolspec.REGISTRY,
        "run_web",
        dataclasses.replace(toolspec.REGISTRY["run_web"], handler=fake_run_web),
    )
    env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": "app.js", "content": "J1"}),
                ("run_web", {"path": "a.html"}),
            ],
        }, {"content": "완료"}]),
        approve=True,
        messages=[{
            "role": "user",
            "content": "Do not edit a.html; edit app.js, then validate a.html.",
        }],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert observed == [("A0", "J1")]


@pytest.mark.parametrize(
    ("text", "required", "excluded"),
    [
        ("Create app.js, then validate a.html.", ["app.js"], []),
        ("Edit app.js then style.css, then validate a.html.", ["app.js", "style.css"], []),
        ("Edit app.js as well as style.css, then validate a.html.", ["app.js", "style.css"], []),
        ("Do not edit app.js; edit style.css, then validate a.html.", ["style.css"], ["app.js"]),
        ("app.js와 style.css를 수정한 뒤 a.html을 검증해줘.", ["app.js", "style.css"], []),
        ("app.js랑 style.css를 수정하고 a.html을 검증해줘.", ["app.js", "style.css"], []),
        ("app.js를 만들고 a.html을 검증해줘.", ["app.js"], []),
    ],
)
def test_dependency_intent_parses_creation_lists_and_clause_negation(text, required, excluded):
    for path in required:
        assert agent._request_directly_mutates_dependency_path(text, path) is True
    for path in excluded:
        assert agent._request_directly_mutates_dependency_path(text, path) is False


def test_no_edit_html_intent_is_not_inverted():
    assert agent._request_directly_mutates_html_path(
        "Do not edit a.html; edit app.js, then validate a.html.", "a.html"
    ) is False
    assert agent._request_directly_mutates_html_path(
        "Edit app.js without editing a.html, then validate a.html.", "a.html"
    ) is False
    assert agent._request_directly_mutates_html_path(
        "a.html은 수정하지 말고 app.js를 수정한 뒤 a.html을 검증해줘.", "a.html"
    ) is False


@pytest.mark.parametrize(
    "text",
    [
        "Do not edit app.js or style.css; validate a.html.",
        "Validate a.html without editing app.js and style.css.",
        "app.js와 style.css를 수정하지 말고 a.html을 검증해줘.",
    ],
)
def test_no_edit_dependency_lists_preserve_every_named_file(text):
    assert agent._request_directly_mutates_dependency_path(text, "app.js") is False
    assert agent._request_directly_mutates_dependency_path(text, "style.css") is False
    assert agent._request_explicitly_preserves_path(text, "app.js") is True
    assert agent._request_explicitly_preserves_path(text, "style.css") is True


@pytest.mark.parametrize("protected_name", ["my app.js", "한글 파일.js"])
def test_quoted_non_ascii_or_spaced_no_edit_path_cannot_be_mutated(env, protected_name):
    (env.ws / protected_name).write_text("M0", encoding="utf-8")
    (env.ws / "app.js").write_text("J0", encoding="utf-8")
    (env.ws / "a.html").write_text("A0", encoding="utf-8")
    request = (
        f'Do not edit "{protected_name}"; edit app.js, then validate a.html.'
    )

    events = env.drive(
        FakeChat([{
            "calls": [
                ("write_code_file", {"path": protected_name, "content": "M1"}),
                ("write_code_file", {"path": "app.js", "content": "J1"}),
                ("run_web", {"path": "a.html"}),
            ],
        }]),
        approve=True,
        messages=[{"role": "user", "content": request}],
        enabled_tools=["write_code_file", "run_web"],
        approval_mode="auto",
    )

    assert (env.ws / protected_name).read_text(encoding="utf-8") == "M0"
    assert (env.ws / "app.js").read_text(encoding="utf-8") == "J0"
    assert protected_name in agent._non_html_file_tokens(request)
    assert not any(event["type"] == "tool_call" for event in events)


def test_case_distinct_validation_and_dependency_tokens_are_preserved():
    html_request = "Edit A.html and a.html, then validate both A.html and a.html."
    assert agent._normal_requested_validation_paths(html_request) == ["A.html", "a.html"]
    dependency_request = "Edit App.js and app.js, then validate a.html."
    assert agent._non_html_file_tokens(dependency_request) == ["App.js", "app.js"]
