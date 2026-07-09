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
    evs = env.run(FakeChat([
        {"calls": [("run_web", {"path": "x.html"})]},
        {"content": "검증 완료."},
    ]))
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
            {"calls": [("write_file", {"path": "a.txt", "content": "hi"})]},
            {"content": "작성 완료."},
        ]),
        approval_mode="auto",  # 승인 스킵
    )
    assert types(evs)[-1] == "done"
    assert len(env.reindex_calls) >= 1  # dirty + indexed → _fire_reindex 호출


# ── 치명적 오류: 잘못된 워크스페이스 / 연결 실패 ────────────
def test_fatal_bad_workspace(env):
    evs = env.run(FakeChat([{"content": "x"}]), workspace="")
    t = types(evs)
    assert t == ["error"]  # error 하나만, done 없음


def test_fatal_connection(env):
    evs = env.run(FakeChat([{"raise": ConnectionError("refused")}]))
    errs = [e for e in evs if e["type"] == "error"]
    assert errs and "연결 실패" in errs[-1]["error"]
    assert "done" not in types(evs)


# ── RAG 컨텍스트가 안정적 프리픽스에 주입되고 search_docs가 맨 앞 ──
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
    assert "[[RAGCTX]]" in sys_content and "search_docs를 사용하라" in sys_content
    assert payload["tools"][0]["function"]["name"] == "search_docs"  # 맨 앞에 prepend
