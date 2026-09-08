# -*- coding: utf-8 -*-
"""저장소 보고 예약의 계약.

예약은 사람이 보지 않는 동안 돈다. 그래서 저장소 경로는 **등록 시점에 한 번 고정**하고,
그 뒤로는 모델이 바꿀 수 없어야 한다. 주기와 개수 한도는 기존 예약과 같은 것을 쓴다.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discordsched  # noqa: E402


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
