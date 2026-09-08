# -*- coding: utf-8 -*-
"""여러 서버에 동시에 붙는다는 계약.

예전에는 봇이 한 서버에만 있을 수 있어 두 번째 초대를 그냥 퇴장시켰고, 기록이 낡으면
새 초대까지 막혀 "초대했는데 아무 반응이 없다"가 됐다. 이제 서버마다 명령 채널과
허용목록을 따로 두고 동시에 동작한다.

격리는 편의가 아니라 안전 요건이다. 한 서버에서 허용한 사용자가 다른 서버의 명령
채널에서 통과하면 그 사용자는 남의 서버를 조작할 수 있다.

명령 채널이 없으면 is_authorized 가 모든 메시지를 막으므로(fail-closed), 채널 확보가
실패한 서버는 조용히 전면 무응답이 된다. 그래서 복구 경로도 함께 고정한다.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def bot(monkeypatch, tmp_path):
    import discordbot

    monkeypatch.setattr(discordbot._S, "data_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(discordbot._S, "guilds", {}, raising=False)
    monkeypatch.setattr(discordbot._S, "synced_guilds", set(), raising=False)
    monkeypatch.setattr(discordbot._S, "owner_id", "", raising=False)
    monkeypatch.setattr(discordbot._S, "last_error", None, raising=False)
    discordbot._CURRENT_GUILD.set("")
    return discordbot


class FakeGuild:
    def __init__(self, gid: int, name: str = "", channels=()) -> None:
        self.id = gid
        self.name = name or f"guild-{gid}"
        self.text_channels = list(channels)
        self.left = False

    async def leave(self) -> None:
        self.left = True

    @property
    def system_channel(self):
        return None


def _on_guild_join(bot, monkeypatch, *, get_guild=lambda _gid: None):
    """실제 봇이 등록한 on_guild_join 을 꺼내 온다 — 본문을 베끼지 않는다."""
    async def unused_generate(_messages):  # pragma: no cover
        return ""

    client = bot._build_client(unused_generate)
    monkeypatch.setattr(client, "get_guild", get_guild, raising=False)
    return client.on_guild_join


# ── 여러 서버 ─────────────────────────────────────────────────────────────

def test_a_second_server_is_accepted_not_kicked(bot, monkeypatch):
    """예전에는 두 번째 초대를 퇴장시켰다. 이제 둘 다 붙는다."""
    bound: list[int] = []

    async def fake_bind(guild):
        bound.append(guild.id)
        bot.guild_state(str(guild.id)).name = guild.name

    monkeypatch.setattr(bot, "_bind_guild", fake_bind)
    handler = _on_guild_join(bot, monkeypatch)

    asyncio.run(handler(FakeGuild(111, "A팀")))
    second = FakeGuild(999, "B팀")
    asyncio.run(handler(second))

    assert second.left is False, "두 번째 서버에서 나가면 안 된다"
    assert bound == [111, 999]


def test_the_guild_cap_is_enforced(bot, monkeypatch):
    """무제한이면 서버마다 채널·문맥을 들고 있어 메모리와 API 호출이 함께 늘어난다."""
    for index in range(bot.MAX_GUILDS):
        bot.guild_state(str(1000 + index))

    async def fake_bind(guild):  # pragma: no cover - 한도에 걸리면 불리지 않는다
        raise AssertionError("한도를 넘겨 붙으면 안 된다")

    monkeypatch.setattr(bot, "_bind_guild", fake_bind)
    extra = FakeGuild(9999)
    asyncio.run(_on_guild_join(bot, monkeypatch)(extra))
    assert extra.left is True


def test_a_server_already_known_is_rebound_even_at_the_cap(bot, monkeypatch):
    """이미 아는 서버의 재연결까지 한도로 막으면 복구가 불가능해진다."""
    for index in range(bot.MAX_GUILDS):
        bot.guild_state(str(1000 + index))
    bound: list[int] = []

    async def fake_bind(guild):
        bound.append(guild.id)

    monkeypatch.setattr(bot, "_bind_guild", fake_bind)
    known = FakeGuild(1000)
    asyncio.run(_on_guild_join(bot, monkeypatch)(known))
    assert known.left is False
    assert bound == [1000]


def test_leaving_a_server_drops_its_record(bot):
    """남아 있으면 화면이 없는 서버를 보여 준다."""
    async def unused_generate(_messages):  # pragma: no cover
        return ""

    client = bot._build_client(unused_generate)
    bot.guild_state("111").channel_id = "222"
    bot._S.synced_guilds.add("111")
    asyncio.run(client.on_guild_remove(FakeGuild(111)))
    assert "111" not in bot._S.guilds
    assert "111" not in bot._S.synced_guilds


# ── 서버별 격리 ───────────────────────────────────────────────────────────

def test_each_server_keeps_its_own_command_channel(bot):
    bot.guild_state("111").channel_id = "aaa"
    bot.guild_state("222").channel_id = "bbb"
    assert bot.command_channel_id("111") == "aaa"
    assert bot.command_channel_id("222") == "bbb"


def test_an_unknown_server_has_no_command_channel(bot):
    assert bot.command_channel_id("404") == ""


def test_an_allowlisted_user_of_one_server_is_not_authorized_in_another(bot):
    """격리의 핵심. 이 성질이 깨지면 남의 서버를 조작할 수 있다."""
    bot._S.owner_id = "1"
    bot.guild_state("111").channel_id = "aaa"
    bot.guild_state("111").allowlist.add("555")
    bot.guild_state("222").channel_id = "bbb"

    assert bot.is_authorized("1", bot.command_channel_id("111"),
                             bot.guild_state("111").allowlist, "555", "aaa") is True
    assert bot.is_authorized("1", bot.command_channel_id("222"),
                             bot.guild_state("222").allowlist, "555", "bbb") is False


def test_the_owner_is_authorized_in_every_server(bot):
    """소유자는 앱 주인이라 서버마다 따로 허용할 필요가 없다."""
    bot.guild_state("111").channel_id = "aaa"
    bot.guild_state("222").channel_id = "bbb"
    assert bot.is_authorized("1", "aaa", set(), "1", "aaa") is True
    assert bot.is_authorized("1", "bbb", set(), "1", "bbb") is True


# ── 요청 문맥 ─────────────────────────────────────────────────────────────

def test_the_current_server_falls_back_only_when_there_is_exactly_one(bot):
    """서버가 여럿인데 문맥이 없으면 아무 서버나 고르지 않는다 — 그것이 가장 나쁜 실패다."""
    bot.guild_state("111")
    assert bot.current_guild_id() == "111"
    bot.guild_state("222")
    assert bot.current_guild_id() == ""


def test_an_explicit_context_wins(bot):
    bot.guild_state("111")
    bot.guild_state("222")
    bot._CURRENT_GUILD.set("222")
    assert bot.current_guild_id() == "222"
    bot._CURRENT_GUILD.set("")


def test_bound_guild_refuses_to_guess_between_servers(bot, monkeypatch):
    bot.guild_state("111")
    bot.guild_state("222")
    monkeypatch.setattr(bot, "is_running", lambda: True)
    assert bot.bound_guild() is None, "문맥 없이 서버를 고르면 안 된다"


# ── 채널 복구 ─────────────────────────────────────────────────────────────

def test_the_runner_retries_every_server_missing_a_channel(bot, monkeypatch):
    """한 서버의 실패가 다른 서버의 복구를 막으면 안 된다."""
    tried: list[str] = []

    async def fake_ensure(guild):
        tried.append(str(guild.id))
        if guild.id == 111:
            raise RuntimeError("권한 없음")

    bot.guild_state("111").channel_id = ""
    bot.guild_state("222").channel_id = ""
    bot.guild_state("333").channel_id = "already"
    monkeypatch.setattr(bot, "_ensure_command_channel", fake_ensure)
    monkeypatch.setattr(bot, "bound_guild", lambda gid="": FakeGuild(int(gid)) if gid else None)

    asyncio.run(bot._retry_command_channel())
    assert tried == ["111", "222"], "이미 있는 서버는 건드리지 않고, 실패해도 다음으로 넘어간다"


def test_channel_lock_survives_an_uncached_bot_member(bot):
    """guild.me 가 None 이어도 채널을 만들 수 있어야 한다(members 인텐트 꺼짐)."""
    class FakeRole:
        pass

    class FakeGuildNoMe:
        id = 42

        def __init__(self):
            self.default_role = FakeRole()
            self.me = None
            self.owner = None
            self.owner_id = 0

    bot._S.owner_id = ""
    bot._S.app_id = ""
    ow = asyncio.run(bot._lock_overwrites(FakeGuildNoMe()))
    assert None not in ow, "None 을 권한 키로 넣으면 채널 생성이 실패한다"
    assert len(ow) == 1


def test_without_a_command_channel_every_message_is_refused(bot):
    assert bot.is_authorized("42", "", set(), "42", "777") is False
    assert bot.is_authorized("42", "777", set(), "42", "777") is True
