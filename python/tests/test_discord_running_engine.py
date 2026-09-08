# -*- coding: utf-8 -*-
"""봇이 **지금 무엇으로 답하고 있는지** 상태로 밝힌다는 계약.

저장된 설정과 실제로 돌고 있는 값은 어긋날 수 있다(설정 저장 실패, 공급자를 전용 동의
흐름으로만 바꾸는 구조 등). 그때 저장 파일만 봐서는 로컬로 도는 건지 NVIDIA로 도는 건지
확인할 길이 없었다. 상태에 주입된 값을 그대로 실어 그 의심을 없앤다.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def bot(monkeypatch, tmp_path):
    import discordbot

    monkeypatch.setattr(discordbot._S, "provider", "", raising=False)
    monkeypatch.setattr(discordbot._S, "model", "", raising=False)
    monkeypatch.setattr(discordbot._S, "data_dir", str(tmp_path), raising=False)
    return discordbot


async def _noop_generate(_messages):  # pragma: no cover - 이 테스트는 생성까지 가지 않는다
    return ""


def test_a_status_reports_nothing_before_the_bot_starts(bot):
    status = bot.status()
    assert status["provider"] == ""
    assert status["model"] == ""


def test_b_a_disabled_config_never_claims_an_engine(bot, monkeypatch):
    """꺼진 봇이 공급자를 표시하면 그 자체가 거짓말이다."""
    monkeypatch.setattr(bot, "_load_state", lambda: None)
    asyncio.run(bot.apply_config(
        {"enabled": False, "token": "", "provider": "nvidia", "model": "moonshotai/kimi-k3"},
        _noop_generate,
    ))
    status = bot.status()
    assert status["running"] is False
    assert status["provider"] == ""
    assert status["model"] == ""


def test_c_a_config_without_a_token_never_claims_an_engine(bot, monkeypatch):
    monkeypatch.setattr(bot, "_load_state", lambda: None)
    asyncio.run(bot.apply_config(
        {"enabled": True, "token": "   ", "provider": "nvidia", "model": "moonshotai/kimi-k3"},
        _noop_generate,
    ))
    assert bot.status()["provider"] == ""


def test_d_stopping_clears_the_engine(bot):
    """멈춘 뒤에도 값이 남으면 화면이 옛 공급자를 계속 보여 준다."""
    bot._S.provider = "nvidia"
    bot._S.model = "moonshotai/kimi-k3"
    asyncio.run(bot.stop())
    assert bot.status()["provider"] == ""
    assert bot.status()["model"] == ""


def test_e_status_carries_the_injected_engine_not_the_saved_setting(bot):
    """상태는 주입된 값을 그대로 실어야 한다 — 저장 파일과 어긋나도 실제를 보여 준다."""
    bot._S.provider = "nvidia"
    bot._S.model = "moonshotai/kimi-k3"
    status = bot.status()
    assert status["provider"] == "nvidia"
    assert status["model"] == "moonshotai/kimi-k3"
