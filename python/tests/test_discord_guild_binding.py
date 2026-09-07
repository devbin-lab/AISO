# -*- coding: utf-8 -*-
"""새 서버로 초대했을 때 봇이 실제로 붙는지에 대한 계약.

증상: 봇을 새 서버에 초대했는데 아무 대답이 없고 #aiso 채널도 생기지 않는다.
원인은 세 가지가 겹친 것이었다.
  1. 옛 서버 기록이 남아 있으면 새 초대를 무조건 거절하고 나가 버렸다.
  2. 서버가 바뀌어도 옛 채널 id 를 그대로 들고 있었다.
  3. 슬래시 커맨드 동기화가 '했다/안 했다' 한 비트라, 서버를 옮기면 새 서버에
     /allow 가 하나도 뜨지 않았다.
채널이 없으면 is_authorized 가 모든 메시지를 막으므로, 이 셋 중 무엇이 걸려도
결과는 똑같이 '전면 무응답'이다.
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
    monkeypatch.setattr(discordbot._S, "guild_id", "", raising=False)
    monkeypatch.setattr(discordbot._S, "channel_id", "", raising=False)
    monkeypatch.setattr(discordbot._S, "synced_guild_id", "", raising=False)
    monkeypatch.setattr(discordbot._S, "owner_id", "", raising=False)
    monkeypatch.setattr(discordbot._S, "last_error", None, raising=False)
    return discordbot


class FakeGuild:
    def __init__(self, gid: int, channels=()) -> None:
        self.id = gid
        self.text_channels = list(channels)
        self.left = False

    async def leave(self) -> None:
        self.left = True

    @property
    def system_channel(self):
        return None


def test_a_stale_binding_to_a_server_the_bot_already_left_does_not_block_a_new_invite(bot, monkeypatch):
    """봇이 더는 없는 서버를 가리키는 기록은 낡은 것이다 — 새 초대를 받아들여야 한다."""
    bot._S.guild_id = "111"
    bot._S.channel_id = "222"
    bound: list[int] = []

    async def fake_bind(guild):
        bound.append(guild.id)

    monkeypatch.setattr(bot, "_bind_guild", fake_bind)
    new_guild = FakeGuild(999)
    # 살아 있는 길드 목록에 111 이 없다 = 그 서버에서 이미 나왔다.
    handler = _on_guild_join_handler(bot, monkeypatch, get_guild=lambda _gid: None)

    asyncio.run(handler(new_guild))

    assert new_guild.left is False, "낡은 기록 때문에 새 서버에서 나가면 안 된다"
    assert bound == [999], "새 서버로 고정되어야 한다"
    assert bot._S.channel_id == "", "옛 서버의 채널 기록은 버려야 한다"


def test_b_a_second_invite_while_still_in_the_first_server_is_still_refused(bot, monkeypatch):
    """단일 서버 규칙은 그대로다 — 아직 그 서버에 있으면 새 초대는 거절한다."""
    bot._S.guild_id = "111"
    bound: list[int] = []

    async def fake_bind(guild):
        bound.append(guild.id)

    monkeypatch.setattr(bot, "_bind_guild", fake_bind)
    new_guild = FakeGuild(999)
    # 111 에 아직 남아 있다.
    handler = _on_guild_join_handler(
        bot, monkeypatch, get_guild=lambda gid: object() if gid == 111 else None
    )

    asyncio.run(handler(new_guild))

    assert new_guild.left is True
    assert bound == []
    assert bot._S.guild_id == "111"


def _on_guild_join_handler(bot, monkeypatch, *, get_guild):
    """실제 봇이 등록한 on_guild_join 을 꺼내 온다.

    핸들러는 _build_client 안에서 @client.event 로 달리므로 client 의 속성이 된다.
    본문을 테스트에 베껴 두면 진짜 코드가 바뀌어도 테스트는 통과하니, 여기서는 실물을
    가져와 부른다. 게이트웨이에는 붙지 않는다 — 객체를 만들 뿐이다.
    """
    async def unused_generate(_messages):  # pragma: no cover — 이 테스트는 생성까지 가지 않는다
        return ""

    client = bot._build_client(unused_generate)
    monkeypatch.setattr(client, "get_guild", get_guild, raising=False)
    return client.on_guild_join


def test_c_moving_to_a_new_server_syncs_slash_commands_there_too(bot, monkeypatch):
    """동기화 기록은 길드별이어야 한다. 한 비트면 두 번째 서버가 /allow 를 못 받는다."""
    synced: list[int] = []

    class FakeTree:
        def copy_global_to(self, guild):
            pass

        async def sync(self, guild):
            synced.append(guild.id)

    class FakeObject:
        def __init__(self, id):  # noqa: A002 — discord.Object 의 인자 이름을 따른다
            self.id = id

    monkeypatch.setattr(bot._S, "tree", FakeTree(), raising=False)
    monkeypatch.setattr(bot.discord, "Object", FakeObject)

    async def noop_channel(_guild):
        return None

    monkeypatch.setattr(bot, "_ensure_command_channel", noop_channel)

    asyncio.run(bot._bind_guild(FakeGuild(111)))
    asyncio.run(bot._bind_guild(FakeGuild(999)))

    assert synced == [111, 999], "새 서버에도 슬래시 커맨드를 올려야 한다"


def test_d_binding_to_a_different_server_drops_the_old_channel_record(bot, monkeypatch):
    bot._S.guild_id = "111"
    bot._S.channel_id = "222"
    seen: list[str] = []

    async def capture(_guild):
        seen.append(bot._S.channel_id)

    monkeypatch.setattr(bot, "_ensure_command_channel", capture)
    monkeypatch.setattr(bot._S, "tree", None, raising=False)

    asyncio.run(bot._bind_guild(FakeGuild(999)))

    assert seen == [""], "채널을 확보하러 갈 때 옛 id 가 남아 있으면 안 된다"


def test_e_rebinding_to_the_same_server_keeps_its_channel(bot, monkeypatch):
    """같은 서버로 다시 붙는 재시작에서는 채널을 새로 만들지 않는다."""
    bot._S.guild_id = "111"
    bot._S.channel_id = "222"
    seen: list[str] = []

    async def capture(_guild):
        seen.append(bot._S.channel_id)

    monkeypatch.setattr(bot, "_ensure_command_channel", capture)
    monkeypatch.setattr(bot._S, "tree", None, raising=False)

    asyncio.run(bot._bind_guild(FakeGuild(111)))

    assert seen == ["222"]


def test_f_the_runner_retries_a_missing_command_channel(bot, monkeypatch):
    """채널 확보는 한 번만 시도했다 — 그때 실패하면 앱을 껐다 켜기 전까지 무응답이었다."""
    tries: list[int] = []

    async def fake_ensure(guild):
        tries.append(guild.id)

    guild = FakeGuild(111)
    monkeypatch.setattr(bot, "_ensure_command_channel", fake_ensure)
    monkeypatch.setattr(bot, "bound_guild", lambda: guild)

    bot._S.channel_id = ""
    asyncio.run(bot._retry_command_channel())
    assert tries == [111], "채널이 없으면 다시 시도해야 한다"

    bot._S.channel_id = "222"
    asyncio.run(bot._retry_command_channel())
    assert tries == [111], "이미 있으면 건드리지 않는다"


def test_g_without_a_command_channel_every_message_is_refused(bot):
    """세 결함의 결과가 왜 똑같이 '전면 무응답'인지 고정한다."""
    assert bot.is_authorized("42", "", set(), "42", "777") is False
    assert bot.is_authorized("42", "777", set(), "42", "777") is True


def test_h_channel_lock_survives_an_uncached_bot_member(bot, monkeypatch):
    """guild.me 가 None 이어도 채널을 만들 수 있어야 한다.

    members 인텐트가 꺼져 있으면 갓 조인한 서버에서 guild.me 가 None 이 나온다. 예전에는
    그 None 을 그대로 권한 키에 넣어 discord.py 가 거절했고, 채널 생성이 통째로 실패해
    봇이 어디서도 대답하지 못했다(상태 파일의 channel_id 가 빈 채로 남는다).
    """
    class FakeRole:
        pass

    class FakeGuildNoMe:
        def __init__(self):
            self.default_role = FakeRole()
            self.me = None
            self.owner = None
            self.owner_id = 0

    bot._S.owner_id = ""
    bot._S.app_id = ""
    bot._S.allowlist = set()

    ow = asyncio.run(bot._lock_overwrites(FakeGuildNoMe()))

    assert None not in ow, "None 을 권한 키로 넣으면 채널 생성이 실패한다"
    assert len(ow) == 1, "@everyone 숨김만 남아야 한다"
