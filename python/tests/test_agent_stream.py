# -*- coding: utf-8 -*-
"""run_agent 이벤트 스트림 특성화 테스트 — 현재 동작을 고정해 리팩터 회귀를 잡는다.

각 테스트는 mock된 _chat_turn(FakeChat)으로 특정 시나리오를 구동하고,
이벤트 타입 순서와 핵심 필드를 검증한다.
"""
from __future__ import annotations

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
    assert "프로젝트 코드 작성·편집은 꺼져 있다" in system_prompt
    assert "코드·명령·웹 실행 검증은 꺼져 있다" in system_prompt
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
    assert "프로젝트 코드 작성·편집은 꺼져 있다" in system_prompt
    assert "프로젝트 코드 작성·편집을 허용했다" not in system_prompt
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
    assert "프로젝트 코드 작성·편집을 허용했다" in system_prompt
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
    assert "스킬 실행은 꺼져 있다" in system_prompt
    assert "만든 스킬은 run_skill로 실행해 정상 동작을 확인" not in system_prompt
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


def test_no_workspace_unknown_tool_is_rejected_before_execution(env, monkeypatch, tmp_path):
    """모델이 노출되지 않은 이름을 지어내도 개별 실행 이벤트 없이 묶음 전체를 닫는다."""
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))  # 스킬 없음
    fc = FakeChat([
        {"calls": [("nonexistent_tool", {})]},
        {"content": "완료"},
    ])
    evs = env.drive(fc, approve=True, workspace="", approval_mode="auto")
    assert not any(e.get("type") in {"tool_call", "tool_result"} for e in evs)
    error = next(e["error"] for e in evs if e.get("type") == "error")
    assert "현재 실행 범위 밖" in error and "nonexistent_tool" in error


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


def test_approval_timeout_rejects(env):
    env.mp.setattr(agent, "APPROVAL_TIMEOUT", 0.05)
    (env.ws / "f.txt").write_text("x", encoding="utf-8")
    evs = env.run(FakeChat([{"calls": [("delete_file", {"path": "f.txt"})]}, {"content": "?"}]))
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["ok"] is False and tr.get("rejected") is True


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
    evs = env.drive(FakeChat([
        {"calls": [("run_web", {"path": "x.html"})]},
        {"content": "검증 완료."},
    ]), approve=True, enabled_tools=["run_web"])
    t = types(evs)
    i_tr = t.index("tool_result")
    i_shot = t.index("screenshot")
    assert i_shot == i_tr + 1  # 스크린샷은 tool_result 바로 뒤
    assert next(e for e in evs if e["type"] == "screenshot")["data"] == "SHOT64"


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
    assert "[[RAGCTX]]" not in sys_content and "search_docs를 사용하라" in sys_content
    assert payload["messages"][1]["content"] == "[[RAGCTX]]"
    assert payload["tools"][0]["function"]["name"] == "search_docs"  # 맨 앞에 prepend


def test_workspace_context_requires_approval_before_web_egress(env):
    """A workspace read makes a later web call visible even in auto mode."""
    (env.ws / "note.txt").write_text("workspace data", encoding="utf-8")
    fake = FakeChat([
        {"calls": [
            ("read_file", {"path": "note.txt"}),
            ("web_search", {"query": "safe public documentation"}),
        ]},
        {"content": "done"},
    ])
    events = env.drive(fake, approve=False, approval_mode="auto")
    approvals = [event for event in events if event.get("type") == "approval_request"]
    assert [event["name"] for event in approvals] == ["web_search"]
    web_result = next(
        event for event in events
        if event.get("type") == "tool_result" and event.get("id") == approvals[0]["id"]
    )
    assert web_result["rejected"] is True


def test_automatic_rag_requires_approval_before_web_egress(env):
    """Automatic RAG counts as workspace data before the first tool call."""
    env.mp.setattr(agent, "rag_status", lambda root: {"indexed": True})

    async def fake_search(root, host, q, k):
        return [{"file": "note.txt", "start": 1, "end": 1, "text": "private", "score": 1.0}]

    env.mp.setattr(agent, "rag_search", fake_search)
    env.mp.setattr(agent, "format_context", lambda results: "[UNTRUSTED_WORKSPACE_CONTEXT]")
    fake = FakeChat([{"calls": [("web_search", {"query": "public docs"})]}, {"content": "done"}])
    events = env.drive(fake, approve=False, approval_mode="auto")
    approvals = [event for event in events if event.get("type") == "approval_request"]
    assert [event["name"] for event in approvals] == ["web_search"]
