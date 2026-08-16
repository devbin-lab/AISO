# -*- coding: utf-8 -*-
"""디스코드 예약(discordsched) 특성화 테스트 — 파싱·발화계산·영속·정규화·승인등급."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python/ 를 import 경로에

import agent  # noqa: E402
import discordsched as sched  # noqa: E402


NOW = datetime(2026, 7, 16, 22, 0)  # 고정 기준시 (Date.now 없이 결정적)


def _reset(tmp_path) -> None:
    sched.configure(str(tmp_path))


# ── when 파싱 ──────────────────────────────────────────────────────────
def test_parse_hhmm_future_today():
    dt, err = sched.parse_when("22:55", "once", now=NOW)
    assert err is None and dt == datetime(2026, 7, 16, 22, 55)


def test_parse_hhmm_past_rolls_to_tomorrow():
    dt, err = sched.parse_when("21:00", "once", now=NOW)  # 이미 지남 → 내일
    assert err is None and dt == datetime(2026, 7, 17, 21, 0)


def test_parse_full_datetime():
    dt, err = sched.parse_when("2026-07-17 08:00", "once", now=NOW)
    assert err is None and dt == datetime(2026, 7, 17, 8, 0)


def test_parse_past_datetime_once_rejected():
    _dt, err = sched.parse_when("2026-07-15 08:00", "once", now=NOW)
    assert err is not None and "지난" in err


def test_parse_bad_format():
    _dt, err = sched.parse_when("아침에", "once", now=NOW)
    assert err is not None and "형식" in err


# ── 인자 정규화(약한 모델 필드명 편차) ─────────────────────────────────
def test_canonical_add_args_synonyms():
    a = sched.canonical_add_args(
        {"channel_name": "공지", "message": "안녕", "at": "08:00", "recurrence": "매일", "type": "briefing"}
    )
    assert a == {"channel": "공지", "text": "안녕", "when": "08:00", "repeat": "daily", "kind": "briefing"}


def test_canonical_add_args_defaults():
    a = sched.canonical_add_args({"channel": "c", "text": "t", "when": "08:00"})
    assert a["repeat"] == "once" and a["kind"] == "message"


# ── 등록·영속·삭제 ─────────────────────────────────────────────────────
def test_add_persists_and_reload(tmp_path):
    _reset(tmp_path)
    job, err = sched.add_job(
        channel_id="1", channel_name="공지", kind="message", text="안녕하세요",
        when="22:55", repeat="once", now=NOW,
    )
    assert err is None and job["next_run"] == "2026-07-16T22:55"
    sched.configure(str(tmp_path))  # 재로드 — 영속 확인
    assert len(sched.jobs()) == 1 and sched.jobs()[0]["text"] == "안녕하세요"


def test_add_rejects_empty_text_and_overflow(tmp_path):
    _reset(tmp_path)
    _j, err = sched.add_job(channel_id="1", channel_name="c", kind="message", text="  ",
                            when="22:55", repeat="once", now=NOW)
    assert err is not None
    for _ in range(sched.MAX_JOBS):
        sched.add_job(channel_id="1", channel_name="c", kind="message", text="x",
                      when="22:55", repeat="once", now=NOW)
    _j, err = sched.add_job(channel_id="1", channel_name="c", kind="message", text="x",
                            when="22:55", repeat="once", now=NOW)
    assert err is not None and "최대" in err


def test_remove(tmp_path):
    _reset(tmp_path)
    job, _ = sched.add_job(channel_id="1", channel_name="c", kind="message", text="x",
                           when="22:55", repeat="once", now=NOW)
    assert sched.remove(job["id"]) is True
    assert sched.remove("nope") is False and sched.jobs() == []


# ── 발화(pop_due): 일회성 소진, 매일 갱신, 놓침 표시 ────────────────────
def test_pop_due_once_consumed(tmp_path):
    _reset(tmp_path)
    sched.add_job(channel_id="1", channel_name="c", kind="message", text="x",
                  when="22:55", repeat="once", now=NOW)
    fired = sched.pop_due(now=datetime(2026, 7, 16, 22, 55))
    assert len(fired) == 1 and fired[0]["missed"] is False
    assert sched.jobs() == []  # 일회성 소진


def test_pop_due_daily_reschedules(tmp_path):
    _reset(tmp_path)
    sched.add_job(channel_id="1", channel_name="c", kind="briefing", text="날씨",
                  when="08:00", repeat="daily", now=NOW)  # 오늘 08시 지남 → 내일 08시
    fired = sched.pop_due(now=datetime(2026, 7, 17, 8, 0))
    assert len(fired) == 1
    assert sched.jobs()[0]["next_run"] == "2026-07-18T08:00"  # 다음날로 갱신


def test_pop_due_marks_missed(tmp_path):
    _reset(tmp_path)
    sched.add_job(channel_id="1", channel_name="c", kind="message", text="x",
                  when="22:55", repeat="once", now=NOW)
    # 발화 예정 시각보다 훨씬 늦게(앱이 꺼져 있었던 것처럼) 확인
    fired = sched.pop_due(now=datetime(2026, 7, 16, 23, 30))
    assert len(fired) == 1 and fired[0]["missed"] is True


def test_pop_due_not_yet(tmp_path):
    _reset(tmp_path)
    sched.add_job(channel_id="1", channel_name="c", kind="message", text="x",
                  when="22:55", repeat="once", now=NOW)
    assert sched.pop_due(now=datetime(2026, 7, 16, 22, 30)) == []  # 아직 시각 전


# ── 리뷰 확정 결함 회귀: 손상 데이터에 죽지 않는다 ──────────────────────
def test_pop_due_survives_broken_next_run_and_fires_good(tmp_path):
    """비문자열·tz-aware·형식오류 next_run이 섞여 있어도 pop_due가 죽지 않고 정상 잡은 발화된다.

    (예전엔 fromisoformat TypeError 또는 naive/aware 비교 TypeError로 pop_due 전체가 마비됐다.)"""
    _reset(tmp_path)
    good, _ = sched.add_job(channel_id="1", channel_name="c", kind="message", text="정상",
                            when="22:55", repeat="once", now=NOW)
    # 저장소를 직접 손상시킨 잡들 주입
    sched._JOBS.extend([
        {"id": "n1", "next_run": None, "repeat": "once", "text": "x", "channel_id": "1"},
        {"id": "n2", "next_run": 999, "repeat": "once", "text": "x", "channel_id": "1"},
        {"id": "n3", "next_run": "2026-07-16T22:55:00+09:00", "repeat": "once", "text": "x", "channel_id": "1"},
        {"id": "n4", "next_run": "형식오류", "repeat": "once", "text": "x", "channel_id": "1"},
    ])
    fired = sched.pop_due(now=datetime(2026, 7, 16, 23, 0))
    fired_ids = {f["id"] for f in fired}
    assert good["id"] in fired_ids  # 정상 잡은 발화됨
    # 깨진 잡들(n1·n2·n4)은 버려져 저장소에서 사라진다(tz-aware n3는 유효 → naive로 강제돼 발화)
    remaining = {j["id"] for j in sched.jobs()}
    assert "n1" not in remaining and "n2" not in remaining and "n4" not in remaining


def test_load_recovers_from_non_list_json(tmp_path):
    """schedules.json이 리스트가 아닌 값(null·숫자·객체)으로 손상돼도 []로 복구(봇 기동 500 방지)."""
    (tmp_path / sched.SCHEDULES_FILE).write_text("null", encoding="utf-8")
    sched.configure(str(tmp_path))
    assert sched.jobs() == []
    (tmp_path / sched.SCHEDULES_FILE).write_text("5", encoding="utf-8")
    sched.configure(str(tmp_path))
    assert sched.jobs() == []
    (tmp_path / sched.SCHEDULES_FILE).write_text('{"not":"a list"}', encoding="utf-8")
    sched.configure(str(tmp_path))
    assert sched.jobs() == []


def test_add_preview_not_truncated(tmp_path):
    """승인 미리보기는 발신 내용을 자르지 않는다 — 소유자가 전체를 보고 승인."""
    long_text = "안내 " + "가" * 800
    j = {"kind": "message", "channel_name": "공지", "text": long_text,
         "repeat": "daily", "next_run": "2026-07-17T08:00"}
    preview = sched.render_add_preview(j)
    assert long_text in preview  # 전체 포함(잘리지 않음)


# ── 에이전트 통합: 등록·노출·승인등급 ──────────────────────────────────
def test_schedule_and_send_tools_registered_and_workspace_free():
    from toolspec import AGENT_TOOLS, REGISTRY

    for name in (
        "discord_send", "discord_schedule_add", "discord_channel_report_add",
        "discord_schedule_list", "discord_schedule_remove",
    ):
        assert name in REGISTRY
        assert name in agent.WORKSPACE_FREE_TOOLS
        assert name not in {t["function"]["name"] for t in AGENT_TOOLS}  # 조건부(KV 스냅샷 불변)


def test_send_and_schedule_add_follow_the_selected_approval_mode():
    """외부 발신도 자동 모드에서는 즉시 실행하고 읽기 모드에서는 확인한다."""
    assert agent.needs_approval("discord_send", "auto") is False
    assert agent.needs_approval("discord_schedule_add", "auto") is False
    assert agent.needs_approval("discord_channel_report_add", "auto") is False
    assert agent.needs_approval("discord_send", "read") is True
    assert agent.needs_approval("discord_schedule_add", "read") is True
    assert agent.needs_approval("discord_channel_report_add", "read") is True
    assert agent.needs_approval("discord_schedule_list", "read") is False


def test_schedule_tools_exposed_when_bot_online(env, monkeypatch):
    from conftest import FakeChat

    monkeypatch.setattr(agent.discordops, "available", lambda: True)
    fc = FakeChat([{"content": "완료"}])
    env.run(fc, approval_mode="auto")
    names = {t["function"]["name"] for t in fc.payloads[0]["tools"]}
    assert {
        "discord_send", "discord_schedule_add", "discord_channel_report_add",
        "discord_schedule_list",
    } <= names
