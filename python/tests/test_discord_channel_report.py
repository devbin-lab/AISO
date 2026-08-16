from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import agent
import discordbot
import discordsched as sched


class FakeAuthor:
    def __init__(self, name: str, *, bot: bool = False) -> None:
        self.display_name = name
        self.bot = bot

    def __str__(self) -> str:
        return self.display_name


class FakeMessage:
    def __init__(self, message_id: int, content: str, *, bot: bool = False) -> None:
        self.id = message_id
        self.content = content
        self.clean_content = content
        self.author = FakeAuthor("Aiso" if bot else "사용자", bot=bot)
        self.attachments = []
        self.created_at = datetime(2026, 8, 11, 12, message_id % 60, tzinfo=timezone.utc)


class FakeChannel:
    def __init__(self, channel_id: int, name: str, messages=None, *, fail_send: bool = False) -> None:
        self.id = channel_id
        self.name = name
        self.messages = list(messages or [])
        self.fail_send = fail_send
        self.sent: list[str] = []
        self.after_ids: list[int | None] = []

    def history(self, *, limit=100, after=None, oldest_first=False):
        self.after_ids.append(getattr(after, "id", None))

        async def iterate():
            values = [m for m in self.messages if after is None or m.id > after.id]
            values = sorted(values, key=lambda item: item.id, reverse=not oldest_first)
            for message in values[:limit]:
                yield message

        return iterate()

    async def send(self, content: str, **_kwargs) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(content)


class FakeGuild:
    def __init__(self, *channels: FakeChannel) -> None:
        self.channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)


def _job(*, cursor: str = "100") -> dict:
    return {
        "id": "report1",
        "kind": "channel_report",
        "channel_id": "20",
        "channel_name": "보고",
        "source_channels": [{"id": "10", "name": "개발", "last_message_id": cursor}],
        "text": "결정과 할 일을 강조",
        "repeat": "interval",
        "interval_hours": 2,
        "next_run": "2026-08-11T14:00",
    }


def test_report_job_repeats_hourly_and_persists_cursor(tmp_path):
    sched.configure(str(tmp_path))
    draft, error = sched.build_channel_report_job(
        source_channels=[{"id": "10", "name": "개발", "last_message_id": "100"}],
        report_channel_id="20",
        report_channel_name="보고",
        interval_hours=2,
        now=datetime(2026, 8, 11, 12, 0),
    )
    assert error is None
    job = sched.commit_job(draft, now=datetime(2026, 8, 11, 12, 0))
    assert job["next_run"] == "2026-08-11T14:00"

    due = sched.pop_due(now=datetime(2026, 8, 11, 14, 0))
    assert [item["id"] for item in due] == [job["id"]]
    assert sched.jobs()[0]["next_run"] == "2026-08-11T16:00"
    assert sched.update_job(job["id"], {"source_channels": [{"id": "10", "name": "개발", "last_message_id": "150"}]})

    sched.configure(str(tmp_path))
    assert sched.jobs()[0]["source_channels"][0]["last_message_id"] == "150"


def test_broken_report_interval_is_dropped_without_blocking_other_jobs(tmp_path):
    sched.configure(str(tmp_path))
    sched._JOBS.extend([
        {**_job(), "id": "broken", "interval_hours": "bad", "next_run": "2026-08-11T14:00"},
        {
            "id": "normal", "kind": "message", "channel_id": "20", "channel_name": "보고",
            "text": "정상", "repeat": "once", "next_run": "2026-08-11T14:00",
        },
    ])
    due = sched.pop_due(now=datetime(2026, 8, 11, 14, 0))
    assert [item["id"] for item in due] == ["normal"]
    assert sched.jobs() == []


def test_collect_reads_after_cursor_and_skips_bot_messages():
    source = FakeChannel(10, "개발", [
        FakeMessage(99, "이전 대화"),
        FakeMessage(101, "새 결정"),
        FakeMessage(102, "이 보고서는 재수집하면 안 됨", bot=True),
        FakeMessage(103, "새 할 일"),
    ])
    lines, updated, errors = asyncio.run(
        discordbot._collect_channel_report_messages(FakeGuild(source), _job())
    )
    assert errors == []
    assert source.after_ids == [100]
    assert len(lines) == 2
    assert "새 결정" in lines[0] and "새 할 일" in lines[1]
    assert all("재수집" not in line for line in lines)
    assert updated[0]["last_message_id"] == "103"


def test_successful_report_commits_cursor_and_second_run_has_no_duplicates(tmp_path, monkeypatch):
    sched.configure(str(tmp_path))
    source = FakeChannel(10, "개발", [FakeMessage(101, "새 작업")])
    destination = FakeChannel(20, "보고")
    guild = FakeGuild(source, destination)
    job = sched.commit_job(_job())

    async def generate(_messages):
        return "## 핵심 요약\n- 새 작업"

    monkeypatch.setattr(discordbot, "bound_guild", lambda: guild)
    monkeypatch.setattr(discordbot._S, "generate", generate)
    asyncio.run(discordbot._run_channel_report(job))

    stored = sched.jobs()[0]
    assert stored["source_channels"][0]["last_message_id"] == "101"
    assert len(destination.sent) == 1

    asyncio.run(discordbot._run_channel_report(stored))
    assert len(destination.sent) == 1
    assert source.after_ids == [100, 101]


def test_failed_report_send_does_not_advance_cursor(tmp_path, monkeypatch):
    sched.configure(str(tmp_path))
    source = FakeChannel(10, "개발", [FakeMessage(101, "보존해야 할 새 작업")])
    destination = FakeChannel(20, "보고", fail_send=True)
    guild = FakeGuild(source, destination)
    job = sched.commit_job(_job())

    async def generate(_messages):
        return "요약"

    monkeypatch.setattr(discordbot, "bound_guild", lambda: guild)
    monkeypatch.setattr(discordbot._S, "generate", generate)
    asyncio.run(discordbot._run_channel_report(job))

    assert sched.jobs()[0]["source_channels"][0]["last_message_id"] == "100"


def test_channel_report_tool_registration_and_approval_policy():
    from toolspec import REGISTRY

    assert "discord_channel_report_add" in REGISTRY
    assert "discord_channel_report_add" in agent.WORKSPACE_FREE_TOOLS
    assert agent.needs_approval("discord_channel_report_add", "auto") is False
    assert agent.needs_approval("discord_channel_report_add", "read") is True


def test_channel_report_schema_is_available_to_discord_tool_chat():
    name = sched.CHANNEL_REPORT_ADD_SCHEMA["function"]["name"]
    required = sched.CHANNEL_REPORT_ADD_SCHEMA["function"]["parameters"]["required"]
    assert name == "discord_channel_report_add"
    assert required == ["channels", "report_channel", "interval_hours"]
