# -*- coding: utf-8 -*-
"""Discord 봇 MVP — 인가 게이트(보안 핵심)·청킹·동적 상태 영속을 고정한다.

실제 게이트웨이 없이 순수 함수만 검증: 소유자+허용목록만, 명령 채널에서만,
스노플레이크 int/str 정규화, 소유자 미판별=fail-closed. 소유자·채널은 런타임 자동 판별이라
is_authorized에 값으로 주입해 검증한다.
"""
from __future__ import annotations

import discordbot

OWNER = "111"
CH = "999"
ALLOW = {"222", "333"}


# ── 인가 게이트 ──────────────────────────────────────────────────────────
def test_owner_in_command_channel_allowed():
    assert discordbot.is_authorized(OWNER, CH, ALLOW, 111, 999) is True


def test_snowflake_int_vs_str_normalized():
    assert discordbot.is_authorized("111", "999", ALLOW, 111, "999") is True
    assert discordbot.is_authorized("111", "999", ALLOW, "111", 999) is True


def test_allowlisted_user_allowed():
    assert discordbot.is_authorized(OWNER, CH, ALLOW, 222, 999) is True
    assert discordbot.is_authorized(OWNER, CH, ALLOW, 333, 999) is True


def test_non_allowlisted_denied():
    assert discordbot.is_authorized(OWNER, CH, ALLOW, 444, 999) is False


def test_wrong_channel_denied_even_owner():
    assert discordbot.is_authorized(OWNER, CH, ALLOW, 111, 12345) is False


def test_no_owner_fail_closed():
    assert discordbot.is_authorized("", CH, ALLOW, 111, 999) is False
    assert discordbot.is_authorized(None, CH, ALLOW, 111, 999) is False


def test_no_channel_denied():
    assert discordbot.is_authorized(OWNER, "", ALLOW, 111, 999) is False


def test_empty_allowlist_only_owner():
    assert discordbot.is_authorized(OWNER, CH, set(), 111, 999) is True
    assert discordbot.is_authorized(OWNER, CH, set(), 222, 999) is False


# ── 메시지 청킹 ──────────────────────────────────────────────────────────
def test_chunk_under_limit_single():
    assert discordbot.chunk_message("안녕하세요") == ["안녕하세요"]


def test_chunk_splits_at_2000():
    long = "가" * 4500
    parts = discordbot.chunk_message(long)
    assert len(parts) == 3 and all(len(p) <= 2000 for p in parts)
    assert "".join(parts) == long


def test_chunk_empty_placeholder():
    assert discordbot.chunk_message("") == ["(빈 응답)"]
    assert discordbot.chunk_message("   ") == ["(빈 응답)"]


# ── 동적 상태 영속(guild·channel·allowlist) ─────────────────────────────
def test_state_save_load_roundtrip(tmp_path):
    discordbot._S.data_dir = str(tmp_path)
    discordbot._S.guild_id = "555"
    discordbot._S.channel_id = "666"
    discordbot._S.allowlist = {"222", "333"}
    discordbot._save_state()

    # 상태를 비우고 다시 로드하면 복원돼야 한다
    discordbot._S.guild_id = ""
    discordbot._S.channel_id = ""
    discordbot._S.allowlist = set()
    discordbot._load_state()
    assert discordbot._S.guild_id == "555"
    assert discordbot._S.channel_id == "666"
    assert discordbot._S.allowlist == {"222", "333"}


def test_state_load_missing_is_noop(tmp_path):
    discordbot._S.data_dir = str(tmp_path)  # state.json 없음
    discordbot._S.allowlist = {"keep"}
    discordbot._load_state()  # 파일 없으면 그대로 둠(무해)
    assert discordbot._S.allowlist == {"keep"}
