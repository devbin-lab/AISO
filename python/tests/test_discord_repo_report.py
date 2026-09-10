# -*- coding: utf-8 -*-
"""저장소 보고 예약의 계약.

예약은 사람이 보지 않는 동안 돈다. 그래서 저장소 경로는 **등록 시점에 한 번 고정**하고,
그 뒤로는 모델이 바꿀 수 없어야 한다. 주기와 개수 한도는 기존 예약과 같은 것을 쓴다.
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discordbot  # noqa: E402
import discordsched  # noqa: E402
import gitreport  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path):
    discordsched.configure(str(tmp_path))
    yield


def build(**overrides):
    args = {
        "repo_path": r"D:\GitHub\CK_SemesterProject",
        "branch": "main",
        "report_channel_id": "123",
        "report_channel_name": "dev-log",
        "interval_hours": 24,
        "instruction": "",
        "head": "abc123def456",
        "now": datetime(2026, 9, 8, 15, 0),
    }
    args.update(overrides)
    return discordsched.build_repo_report_job(**args)


def test_a_valid_registration_pins_the_repository_and_the_baseline_commit():
    job, err = build()
    assert err is None
    assert job["kind"] == "repo_report"
    assert job["repo_path"] == r"D:\GitHub\CK_SemesterProject"
    assert job["branch"] == "main"
    assert job["last_commit"] == "abc123def456", "등록 시점 HEAD 를 기준으로 삼아야 한다"
    assert job["repeat"] == "interval"
    assert job["interval_hours"] == 24
    assert job["next_run"] == "2026-09-09T15:00"


def test_the_baseline_prevents_dumping_the_whole_history_on_the_first_report():
    """기준 커밋이 비어 있으면 첫 보고가 저장소 전체 역사를 쏟아낸다."""
    job, err = build(head="")
    assert err is None
    assert job["last_commit"] == ""  # 수집 쪽이 '최신 1건'으로 처리한다


def test_an_empty_path_is_refused():
    _job, err = build(repo_path="   ")
    assert err is not None and "경로" in err


def test_an_overlong_path_is_refused():
    _job, err = build(repo_path="C:/" + "a" * discordsched.MAX_REPO_PATH)
    assert err is not None


def test_an_overlong_branch_is_refused():
    _job, err = build(branch="b" * (discordsched.MAX_REPO_BRANCH + 1))
    assert err is not None


def test_a_missing_branch_falls_back_to_the_checked_out_one():
    job, err = build(branch="")
    assert err is None
    assert job["branch"] == "HEAD"


@pytest.mark.parametrize("hours", [0, -1, discordsched.MAX_REPORT_INTERVAL_HOURS + 1])
def test_an_interval_outside_the_allowed_range_is_refused(hours):
    _job, err = build(interval_hours=hours)
    assert err is not None and "interval_hours" in err


def test_a_non_numeric_interval_is_refused():
    _job, err = build(interval_hours="매일")
    assert err is not None


def test_an_overlong_instruction_is_refused():
    _job, err = build(instruction="x" * (discordsched.MAX_REPORT_INSTRUCTION + 1))
    assert err is not None


def test_registration_respects_the_shared_job_limit():
    """저장소 보고도 다른 예약과 같은 상한을 쓴다 — 별도 한도를 만들면 폭주 방지가 새어 나간다."""
    for index in range(discordsched.MAX_JOBS):
        job, err = build(head=f"sha{index}")
        assert err is None
        discordsched.commit_job(job)
    _job, err = build()
    assert err is not None and str(discordsched.MAX_JOBS) in err


def test_a_registered_job_is_listed_with_its_repository():
    job, _err = build()
    committed = discordsched.commit_job(job)
    rendered = discordsched.render_job(committed)
    assert "저장소 보고" in rendered
    assert "CK_SemesterProject" in rendered
    assert "#dev-log" in rendered
    assert "24시간마다" in rendered


def test_the_cursor_advances_only_through_update_job():
    """실행기가 성공 후에만 커서를 옮긴다는 전제. 여기서는 그 통로가 있음을 고정한다."""
    job, _err = build()
    committed = discordsched.commit_job(job)
    discordsched.update_job(committed["id"], {"last_commit": "newsha123456"})
    stored = [j for j in discordsched.jobs() if j["id"] == committed["id"]][0]
    assert stored["last_commit"] == "newsha123456"
    assert stored["repo_path"] == r"D:\GitHub\CK_SemesterProject", "경로는 고정이다"


def test_the_tool_schema_requires_a_path_and_never_defaults_it():
    """모델이 경로를 짐작해 넣지 못하게 필수로 두고, 설명에도 못박는다."""
    schema = discordsched.REPO_REPORT_ADD_SCHEMA["function"]
    assert schema["name"] == "discord_repo_report_add"
    required = schema["parameters"]["required"]
    assert "repo_path" in required
    assert "report_channel" in required
    assert "interval_hours" in required
    assert "branch" not in required
    assert "그대로" in schema["description"]


# ── 실행 계약 ───────────────────────────────────────────────────────────
# 아래는 예약이 실제로 발화했을 때의 계약이다. 이 기능은 새 커밋이 없으면 아무 말도
# 하지 않는 것이 정상이라, 밖에서 보면 정상과 고장이 똑같이 조용하다. 그래서 "언제
# 무엇까지 보고했는가"를 남기는 것이 관측의 유일한 수단이다.
class FakeChannel:
    def __init__(self, channel_id: int, *, name: str = "dev-log", fail_send: bool = False) -> None:
        self.id = channel_id
        self.name = name
        self.fail_send = fail_send
        self.sent: list[str] = []

    async def send(self, content: str, **_kwargs) -> None:
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(content)


class FakeGuild:
    def __init__(self, *channels: FakeChannel, guild_id: str = "777") -> None:
        self.id = guild_id
        self.channels = {channel.id: channel for channel in channels}

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)


def _commit(sha: str) -> gitreport.Commit:
    return gitreport.Commit(
        sha=sha,
        author_name="devbin",
        author_email="devbin@example.com",
        when="2026-09-10 12:00",
        subject=f"feat: {sha}",
        body="",
        files=[gitreport.CommitFile(path="src/main.py", added=10, removed=2)],
    )


def _run_job(head: str = "cafe1234", **overrides) -> dict:
    job, err = build(instruction="", head=head, **overrides)
    assert err is None and job is not None
    return discordsched.commit_job(job)


def _arrange(monkeypatch, commits, channel, *, guilds=None, generate=None, command=None,
             collect_error="", ref_heads=None, moved_branches=""):
    """저장소 읽기와 모델 생성을 대역으로 바꾼다 — 남는 것은 커서·시각 기록뿐이다."""
    async def collect(_path, branch, _cursor, **_kw):
        if collect_error:
            raise gitreport.GitReportError(collect_error)
        return gitreport.RepoReport(
            repo_path=r"D:\GitHub\CK_SemesterProject",
            branch=branch,
            head=commits[-1].sha if commits else "cafe1234",
            commits=list(commits),
        )

    async def default_generate(_messages):
        return "추가된 것\n- 무언가"

    # bound_guild 대역은 **인자를 받는다**. 예약 러너에는 "지금 처리 중인 서버"라는
    # 문맥이 없어서, 잡에 적힌 서버로 찾아가는 것이 유일하게 옳은 경로다.
    # 실패는 보고 채널이 아니라 명령 채널(#aiso)로 간다 — 전송 자체가 실패한 회차에는
    # 보고 채널로 아무 말도 할 수 없기 때문이다.
    command_channel = command if command is not None else FakeChannel(999, name="aiso")
    table = guilds if guilds is not None else {"": FakeGuild(channel, command_channel)}
    async def collect_all(_path, cursors, **_kw):
        if collect_error:
            raise gitreport.GitReportError(collect_error)
        return gitreport.RepoReport(
            repo_path="/repo",
            branch=moved_branches or gitreport.ALL_BRANCHES,
            head="",
            commits=list(commits),
            ref_heads=dict(ref_heads or {}),
        )

    monkeypatch.setattr(gitreport, "collect", collect)
    monkeypatch.setattr(gitreport, "collect_all_branches", collect_all)
    monkeypatch.setattr(discordbot, "bound_guild", lambda guild_id="": table.get(str(guild_id)))
    monkeypatch.setattr(discordbot, "command_channel_id", lambda guild_id="": "999")
    monkeypatch.setattr(discordbot._S, "generate", generate or default_generate)
    return command_channel


def test_a_successful_report_records_both_the_cursor_and_the_time(monkeypatch):
    channel = FakeChannel(123)
    job = _run_job()
    _arrange(monkeypatch, [_commit("aaaa1111"), _commit("bbbb2222")], channel)

    asyncio.run(discordbot._run_repo_report(job))

    stored = discordsched.jobs()[0]
    assert stored["last_commit"] == "bbbb2222"
    # 시각이 없으면 설정 탭이 '아직 보고 없음'이라 말하게 된다 — 보고했는데도.
    assert stored["last_reported_at"]
    assert len(channel.sent) == 1


def test_a_failed_send_records_neither_so_the_next_run_retries(monkeypatch):
    channel = FakeChannel(123, fail_send=True)
    job = _run_job()
    _arrange(monkeypatch, [_commit("aaaa1111")], channel)

    asyncio.run(discordbot._run_repo_report(job))

    stored = discordsched.jobs()[0]
    assert stored["last_commit"] == "cafe1234"
    assert "last_reported_at" not in stored


def test_silence_is_not_recorded_as_a_report(monkeypatch):
    """새 커밋이 없어 조용히 넘어간 회차는 '보고했다'고 기록하지 않는다."""
    channel = FakeChannel(123)
    job = _run_job()
    _arrange(monkeypatch, [], channel)

    asyncio.run(discordbot._run_repo_report(job))

    stored = discordsched.jobs()[0]
    assert "last_reported_at" not in stored
    assert channel.sent == []


def test_the_report_goes_to_the_server_it_was_registered_on(monkeypatch):
    """봇이 여러 서버에 붙어 있어도, 잡에 적힌 서버의 채널로 간다."""
    right = FakeChannel(123)
    wrong = FakeChannel(123)
    job = _run_job(guild_id="222")
    _arrange(monkeypatch, [_commit("aaaa1111")], right,
             guilds={"111": FakeGuild(wrong), "222": FakeGuild(right)})

    asyncio.run(discordbot._run_repo_report(job))

    assert len(right.sent) == 1
    assert wrong.sent == []


def test_a_job_without_a_server_does_not_guess_one(monkeypatch):
    """서버를 못 정하면 아무 서버에나 쏘지 않는다 — 잘못된 서버로 나가는 것이 최악이다."""
    channel = FakeChannel(123)
    job = _run_job()
    assert job["guild_id"] == ""
    _arrange(monkeypatch, [_commit("aaaa1111")], channel,
             guilds={"111": FakeGuild(FakeChannel(123)), "222": FakeGuild(FakeChannel(123))})

    asyncio.run(discordbot._run_repo_report(job))

    assert discordsched.jobs()[0]["last_commit"] == "cafe1234", "보내지 못했으면 커서도 그대로다"


# ── 설정 탭에서의 등록 ───────────────────────────────────────────────────
# 앱 창에서 사람이 폴더·브랜치·채널을 고르고 누르는 경로. 디스코드로 들어오는 등록은
# 소유자에게 승인 버튼을 묻지만, 여기서는 앱 주인이 직접 고른 것이 곧 승인이다.
# 대신 검증은 똑같이 거쳐야 한다 — 발화할 때마다 실패하는 예약을 만들지 않기 위해서다.

def _register(monkeypatch, *, running=True, guilds=None, collect_error="", **overrides):
    async def collect(_path, branch, _cursor, **_kw):
        if collect_error:
            raise gitreport.GitReportError(collect_error)
        return gitreport.RepoReport(repo_path="/repo", branch=branch, head="beef1234", commits=[])

    table = guilds if guilds is not None else {"777": FakeGuild(FakeChannel(123))}
    monkeypatch.setattr(discordbot, "is_running", lambda: running)
    monkeypatch.setattr(discordbot, "bound_guild", lambda guild_id="": table.get(str(guild_id)))
    monkeypatch.setattr(gitreport, "collect", collect)
    # FakeChannel 을 글 채널로 인정시킨다. 실제 discord.TextChannel 은 커넥션 상태가
    # 있어야 만들 수 있어서, 검사 대상 타입을 바꿔 끼우는 것이 유일하게 깨끗한 이음매다.
    monkeypatch.setattr(discordbot.discord, "TextChannel", FakeChannel)

    args = {
        "repo_path": r"D:\GitHub\CK_SemesterProject",
        "branch": "origin/main",
        "guild_id": "777",
        "channel_id": "123",
        "interval_hours": 6,
        "instruction": "",
    }
    args.update(overrides)
    return asyncio.run(discordbot.register_repo_report(**args))


def test_registering_from_the_app_pins_the_server_channel_and_baseline(monkeypatch):
    job, error = _register(monkeypatch)
    assert error is None and job is not None
    assert job["guild_id"] == "777"
    assert job["channel_id"] == "123"
    assert job["branch"] == "origin/main"
    assert job["last_commit"] == "beef1234", "등록 시점 HEAD 부터 본다 — 역사를 쏟아내지 않는다"
    assert job["interval_hours"] == 6
    assert discordsched.jobs()[0]["id"] == job["id"]


def test_a_branch_that_does_not_exist_is_refused_at_registration(monkeypatch):
    """없는 ref 로 등록되면 발화 때마다 실패하는데, 침묵이 정상이라 밖에서는 구분되지 않는다."""
    job, error = _register(monkeypatch, branch="oigin/main", collect_error="git 명령 실패: unknown revision")
    assert job is None
    assert error is not None and "unknown revision" in error
    assert discordsched.jobs() == []


def test_registration_needs_a_connected_bot(monkeypatch):
    job, error = _register(monkeypatch, running=False)
    assert job is None and error is not None and "연결" in error
    assert discordsched.jobs() == []


def test_a_channel_the_bot_cannot_see_is_refused(monkeypatch):
    """보낼 수 없는 채널로 등록되면 발화 시각마다 조용히 실패한다."""
    job, error = _register(monkeypatch, channel_id="999")
    assert job is None and error is not None and "채널" in error
    assert discordsched.jobs() == []


def test_an_unknown_server_is_refused(monkeypatch):
    job, error = _register(monkeypatch, guild_id="000")
    assert job is None and error is not None and "서버" in error


def test_the_interval_limits_are_the_same_as_the_natural_language_path(monkeypatch):
    job, error = _register(monkeypatch, interval_hours=0)
    assert job is None and error is not None and "interval_hours" in error


# ── 실패한 회차는 통째로 미룬다 ──────────────────────────────────────────
# 예전에는 생성이 실패해도 "(보고서 생성 실패: …)" 를 본문 삼아 보내고 커서를 옮겼다.
# 전송에는 성공하니 커밋이 소모되어, 그 회차의 커밋은 영영 다시 보고되지 않았다.

def _boom(message="모델이 죽었습니다"):
    async def generate(_messages):
        raise RuntimeError(message)

    return generate


async def _timeout(_messages):
    raise asyncio.TimeoutError


async def _empty(_messages):
    return "   "


def test_a_generation_failure_keeps_the_commits_for_the_next_run(monkeypatch):
    channel = FakeChannel(123)
    job = _run_job()
    command = _arrange(monkeypatch, [_commit("aaaa1111")], channel, generate=_boom())

    asyncio.run(discordbot._run_repo_report(job))

    assert channel.sent == [], "실패 문구를 보고서인 척 보내지 않는다"
    stored = discordsched.jobs()[0]
    assert stored["last_commit"] == "cafe1234", "커밋이 소모되면 영영 보고되지 않는다"
    assert "last_reported_at" not in stored
    assert len(command.sent) == 1 and "모델이 죽었습니다" in command.sent[0]


def test_a_timeout_is_treated_the_same_as_any_other_failure(monkeypatch):
    """12B 로 커밋 수십 개를 요약하다 시간을 넘기는 것은 드문 일이 아니다."""
    channel = FakeChannel(123)
    job = _run_job()
    command = _arrange(monkeypatch, [_commit("aaaa1111")], channel, generate=_timeout)

    asyncio.run(discordbot._run_repo_report(job))

    assert channel.sent == []
    assert discordsched.jobs()[0]["last_commit"] == "cafe1234"
    assert "초를 넘겨" in command.sent[0]


def test_an_empty_answer_is_a_failure_not_a_report(monkeypatch):
    """헤더만 있는 보고서를 보내고 커밋을 소모하면 같은 손실이 난다."""
    channel = FakeChannel(123)
    job = _run_job()
    command = _arrange(monkeypatch, [_commit("aaaa1111")], channel, generate=_empty)

    asyncio.run(discordbot._run_repo_report(job))

    assert channel.sent == []
    assert discordsched.jobs()[0]["last_commit"] == "cafe1234"
    assert "빈 보고서" in command.sent[0]


def test_the_failure_notice_says_how_many_commits_are_waiting(monkeypatch):
    channel = FakeChannel(123)
    job = _run_job()
    command = _arrange(
        monkeypatch, [_commit("aaaa1111"), _commit("bbbb2222")], channel, generate=_boom()
    )

    asyncio.run(discordbot._run_repo_report(job))

    assert "커밋 2개는 아직 보고되지 않았습니다" in command.sent[0]
    assert "다시 시도합니다" in command.sent[0]


def test_the_same_failure_is_announced_once_not_every_cycle(monkeypatch):
    """모델이 며칠 멈춰 있으면 주기마다 같은 문장이 쌓여 명령 채널이 못 쓰게 된다."""
    channel = FakeChannel(123)
    job = _run_job()
    command = _arrange(monkeypatch, [_commit("aaaa1111")], channel, generate=_boom())

    asyncio.run(discordbot._run_repo_report(job))
    asyncio.run(discordbot._run_repo_report(discordsched.jobs()[0]))
    asyncio.run(discordbot._run_repo_report(discordsched.jobs()[0]))

    assert len(command.sent) == 1
    assert discordsched.jobs()[0]["last_commit"] == "cafe1234", "묵살해도 재시도는 계속된다"


def test_a_different_failure_is_announced_again(monkeypatch):
    channel = FakeChannel(123)
    job = _run_job()
    command = _arrange(monkeypatch, [_commit("aaaa1111")], channel, generate=_boom("첫 번째"))
    asyncio.run(discordbot._run_repo_report(job))

    _arrange(monkeypatch, [_commit("aaaa1111")], channel, generate=_boom("두 번째"), command=command)
    asyncio.run(discordbot._run_repo_report(discordsched.jobs()[0]))

    assert len(command.sent) == 2


def test_a_success_clears_the_failure_so_the_next_one_is_announced(monkeypatch):
    """지우지 않으면 회복한 뒤에 난 실패가 '같은 사유'로 묵살된다."""
    channel = FakeChannel(123)
    job = _run_job()
    command = _arrange(monkeypatch, [_commit("aaaa1111")], channel, generate=_boom())
    asyncio.run(discordbot._run_repo_report(job))

    _arrange(monkeypatch, [_commit("aaaa1111")], channel, command=command)
    asyncio.run(discordbot._run_repo_report(discordsched.jobs()[0]))
    assert discordsched.jobs()[0]["last_failure"] == ""

    _arrange(monkeypatch, [_commit("bbbb2222")], channel, generate=_boom(), command=command)
    asyncio.run(discordbot._run_repo_report(discordsched.jobs()[0]))

    assert len(command.sent) == 2


def test_a_failed_send_is_announced_on_the_command_channel(monkeypatch):
    """보고 채널로 못 보낸 회차야말로 다른 채널로 알려야 한다."""
    channel = FakeChannel(123, fail_send=True)
    job = _run_job()
    command = _arrange(monkeypatch, [_commit("aaaa1111")], channel)

    asyncio.run(discordbot._run_repo_report(job))

    assert len(command.sent) == 1 and "보내지 못했습니다" in command.sent[0]
    assert discordsched.jobs()[0]["last_commit"] == "cafe1234"


def test_an_unreadable_repository_is_announced_on_the_command_channel(monkeypatch):
    """폴더가 사라진 상태로 몇 주가 지나면 보고 채널이 경고문으로 뒤덮인다."""
    channel = FakeChannel(123)
    job = _run_job()
    command = _arrange(monkeypatch, [], channel, collect_error="폴더를 찾을 수 없습니다")

    asyncio.run(discordbot._run_repo_report(job))

    assert channel.sent == [], "실패는 보고 채널로 가지 않는다"
    assert len(command.sent) == 1 and "폴더를 찾을 수 없습니다" in command.sent[0]


# ── 모든 브랜치 모드 ─────────────────────────────────────────────────────
# 브랜치 하나짜리 예약으로는 각자 자기 브랜치에서 일하는 팀을 따라갈 수 없다.
# 기준점이 sha 하나가 아니라 ref 마다 하나라는 것이 이 모드의 유일한 구조적 차이다.

ALL = gitreport.ALL_BRANCHES
HEADS = {"origin/main": "aaaa1111", "origin/feature/net": "bbbb2222"}


def _all_job(cursors=None):
    job, err = build(branch=ALL, head="", cursors=cursors or {"origin/main": "0000start"})
    assert err is None and job is not None
    return discordsched.commit_job(job)


def test_all_branches_registration_keeps_a_cursor_per_ref():
    """브랜치가 각자 다른 속도로 움직인다. sha 하나로는 어디까지 봤는지 적을 수 없다."""
    job, err = build(branch=ALL, head="", cursors=HEADS)
    assert err is None
    assert job["branch"] == ALL
    assert job["branch_cursors"] == HEADS
    assert job["last_commit"] == ""


def test_a_single_branch_job_carries_no_per_ref_cursors():
    job, _err = build()
    assert job["branch_cursors"] == {}
    assert job["last_commit"] == "abc123def456"


def test_the_list_calls_it_all_branches_not_a_star():
    rendered = discordsched.render_job(discordsched.commit_job(build(branch=ALL, head="")[0]))
    assert "모든 브랜치" in rendered
    assert "(*)" not in rendered


def test_a_successful_all_branches_report_advances_every_cursor(monkeypatch):
    channel = FakeChannel(123)
    job = _all_job()
    _arrange(monkeypatch, [_commit("aaaa1111")], channel, ref_heads=HEADS,
             moved_branches="origin/feature/net")

    asyncio.run(discordbot._run_repo_report(job))

    stored = discordsched.jobs()[0]
    assert stored["branch_cursors"] == HEADS
    assert stored["last_commit"] == "", "이 모드에서는 sha 하나짜리 커서를 쓰지 않는다"
    assert stored["last_reported_at"]


def test_the_header_says_which_branches_moved(monkeypatch):
    """여러 브랜치를 한꺼번에 보면, 이 줄이 없이는 어디서 벌어진 일인지 다 읽어야 안다."""
    channel = FakeChannel(123)
    job = _all_job()
    _arrange(monkeypatch, [_commit("aaaa1111")], channel, ref_heads=HEADS,
             moved_branches="origin/feature/net")

    asyncio.run(discordbot._run_repo_report(job))

    assert "origin/feature/net" in channel.sent[0]


def test_a_failed_all_branches_report_keeps_every_cursor(monkeypatch):
    channel = FakeChannel(123)
    job = _all_job()
    _arrange(monkeypatch, [_commit("aaaa1111")], channel, ref_heads=HEADS, generate=_boom())

    asyncio.run(discordbot._run_repo_report(job))

    assert channel.sent == []
    assert discordsched.jobs()[0]["branch_cursors"] == {"origin/main": "0000start"}


def test_a_quiet_cycle_still_pins_newly_discovered_branches(monkeypatch):
    """새 브랜치가 생겼는데 새 커밋이 없는 회차. 기준을 안 잡으면 다음에 통째로 쏟아진다."""
    channel = FakeChannel(123)
    job = _all_job()
    _arrange(monkeypatch, [], channel, ref_heads=HEADS)

    asyncio.run(discordbot._run_repo_report(job))

    assert channel.sent == []
    stored = discordsched.jobs()[0]
    assert stored["branch_cursors"] == HEADS
    assert "last_reported_at" not in stored, "조용한 회차는 보고가 아니다"


def test_registering_all_branches_baselines_every_ref(monkeypatch):
    async def collect_all(_path, _cursors, **_kw):
        return gitreport.RepoReport(
            repo_path="/repo", branch=ALL, head="", commits=[], ref_heads=HEADS,
        )

    monkeypatch.setattr(gitreport, "collect_all_branches", collect_all)
    job, error = _register(monkeypatch, branch=ALL)

    assert error is None and job is not None
    assert job["branch"] == ALL
    assert job["branch_cursors"] == HEADS, "등록 시점의 모든 ref 가 기준이 된다"


def test_natural_language_ways_of_saying_all_branches_land_on_the_same_mode():
    """자연어 등록에서 모델이 무엇을 적어 보낼지 알 수 없다. 한 곳에서 모은다."""
    for said in ("모든 브랜치", "전체", "*", "all branches", " 모든브랜치 "):
        job, err = build(branch=said, head="")
        assert err is None, said
        assert job["branch"] == ALL, said


def test_a_real_branch_name_is_never_mistaken_for_all_branches():
    for said in ("main", "origin/main", "feature/all", "allocation"):
        job, _err = build(branch=said, head="")
        assert job["branch"] == said


# ── 지금 보고 ────────────────────────────────────────────────────────────
# 등록하고 나면 다음 회차까지 몇 시간을 기다려야 "이게 실제로 되나"를 알 수 있었다.
# 디스코드에서 "지금 만들어줘"라고 하면 모델이 집을 수 있는 도구가 등록밖에 없어서
# 저장소 경로와 주기를 처음부터 다시 물었다 — 그 자리를 메우는 경로다.

def test_with_nothing_registered_it_says_so(monkeypatch):
    assert "없습니다" in asyncio.run(discordbot.report_repo_now())


def test_a_single_registration_needs_no_id(monkeypatch):
    channel = FakeChannel(123)
    _run_job()
    _arrange(monkeypatch, [_commit("aaaa1111")], channel)

    answer = asyncio.run(discordbot.report_repo_now())

    assert len(channel.sent) == 1
    assert "#dev-log" in answer


def test_with_several_registrations_it_refuses_to_guess(monkeypatch):
    """보고는 채널로 나가는 발신이다. 잘못 고르면 되돌릴 수 없다."""
    channel = FakeChannel(123)
    _run_job(head="aaaa0000")
    _run_job(head="bbbb0000")
    _arrange(monkeypatch, [_commit("aaaa1111")], channel)

    answer = asyncio.run(discordbot.report_repo_now())

    assert channel.sent == []
    assert "여럿" in answer


def test_a_short_id_is_enough(monkeypatch):
    """목록이 id 앞 8자리만 보여 준다. 사람이 볼 수 있는 값으로 고를 수 있어야 한다."""
    channel = FakeChannel(123)
    _run_job(head="aaaa0000")
    wanted = _run_job(head="bbbb0000")
    _arrange(monkeypatch, [_commit("aaaa1111")], channel)

    answer = asyncio.run(discordbot.report_repo_now(wanted["id"][:8]))

    assert len(channel.sent) == 1
    assert "보냈습니다" in answer


def test_an_unknown_id_is_not_silently_ignored(monkeypatch):
    _run_job()
    assert "찾지 못했습니다" in asyncio.run(discordbot.report_repo_now("nope"))


def test_a_quiet_run_says_there_was_nothing_to_send(monkeypatch):
    """정기 회차의 침묵은 정상이지만, 사람이 누른 자리의 침묵은 고장과 구별되지 않는다."""
    channel = FakeChannel(123)
    _run_job()
    _arrange(monkeypatch, [], channel)

    answer = asyncio.run(discordbot.report_repo_now())

    assert channel.sent == []
    assert "새 커밋이 없습니다" in answer


def test_a_failed_run_points_at_the_command_channel(monkeypatch):
    channel = FakeChannel(123)
    _run_job()
    command = _arrange(monkeypatch, [_commit("aaaa1111")], channel, generate=_boom())

    answer = asyncio.run(discordbot.report_repo_now())

    assert "명령 채널" in answer
    assert len(command.sent) == 1


def test_an_immediate_run_advances_the_cursor_like_any_other(monkeypatch):
    """지금 보고한 커밋이 다음 정기 회차에 또 나가면 안 된다."""
    channel = FakeChannel(123)
    _run_job()
    _arrange(monkeypatch, [_commit("aaaa1111")], channel)

    asyncio.run(discordbot.report_repo_now())

    assert discordsched.jobs()[0]["last_commit"] == "aaaa1111"


def test_two_runs_of_one_job_never_overlap(monkeypatch):
    """버튼을 누른 순간 마침 예약 회차가 발화하면 같은 커밋이 두 번 나갈 수 있다."""
    channel = FakeChannel(123)
    job = _run_job()
    holding = asyncio.Event()

    async def slow_generate(_messages):
        await holding.wait()
        return "추가된 것"

    _arrange(monkeypatch, [_commit("aaaa1111")], channel, generate=slow_generate)

    async def scenario():
        first = asyncio.create_task(discordbot._run_repo_report(job))
        await asyncio.sleep(0)  # 첫 실행이 가드를 잡을 틈을 준다
        second = await discordbot.report_repo_now()
        holding.set()
        return await first, second

    _first, answer = asyncio.run(scenario())

    assert len(channel.sent) == 1, "같은 잡이 겹쳐 돌면 안 된다"
    assert "이미" in answer


def test_the_now_tool_tells_the_model_it_is_not_a_registration():
    """모델이 '지금 만들어줘'에 등록 도구를 집어 저장소 경로부터 되묻던 자리를 메운다."""
    schema = discordsched.REPO_REPORT_NOW_SCHEMA["function"]
    assert schema["name"] == "discord_repo_report_now"
    assert schema["parameters"]["required"] == []
    assert "새 예약을 만들지 않으므로" in schema["description"]


def test_only_the_owner_can_fire_a_report_on_demand(monkeypatch):
    """허용목록 사용자가 보고 채널을 마음대로 울릴 수 있게 두지 않는다."""
    channel = FakeChannel(123)
    _run_job()
    _arrange(monkeypatch, [_commit("aaaa1111")], channel)
    monkeypatch.setattr(discordbot._S, "owner_id", "1")

    refused = asyncio.run(
        discordbot._run_bot_tool(channel, "2", "discord_repo_report_now", {})
    )
    assert refused.startswith("[거부]")
    assert channel.sent == []

    allowed = asyncio.run(
        discordbot._run_bot_tool(channel, "1", "discord_repo_report_now", {})
    )
    assert "보냈습니다" in allowed
