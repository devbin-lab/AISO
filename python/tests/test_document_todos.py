from __future__ import annotations

import json
import shutil
import zipfile
from datetime import date
from html import escape
from pathlib import Path

import pytest

from document_todos import (
    _due_time,
    analyze_documents,
    apply_reschedule,
    create_calendar_todo,
    create_todo_item,
    create_todo_event,
    delete_todo,
    list_documents,
    list_saved_todos,
    list_todos,
    manage_todo,
    preview_reschedule,
    save_todos,
    update_todo,
)
from extract import extract_document_segments
from tools import ToolError, run_tool


@pytest.fixture(autouse=True)
def _central_todo_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AISO_DOCUMENT_TODO_DB_PATH", str(tmp_path / "Aiso" / "document-todos.sqlite3"))


def test_document_todo_candidates_keep_exact_source_evidence(tmp_path: Path):
    (tmp_path / "plan.md").write_text("# 계획\n\n1. 핵심 전투 시스템 구현\n2. 2026년 8월 23일 오후 2시 30분 발표 자료 제출\n", encoding="utf-8")

    result = analyze_documents(str(tmp_path), ["plan.md"])

    assert len(result["candidates"]) >= 2
    assert result["candidates"][0]["evidence"] == [{
        "file": "plan.md", "location": "1~4줄", "quote": "핵심 전투 시스템 구현"
    }]
    assert any(item["dueDate"] == "2026-08-23" for item in result["candidates"])
    assert any(item["dueTime"] == "14:30" for item in result["candidates"])


def test_document_todo_rejects_path_outside_workspace(tmp_path: Path):
    with pytest.raises(ToolError, match="작업 폴더 밖"):
        analyze_documents(str(tmp_path), ["../private.pdf"])


def test_document_todo_save_and_status_change_are_persistent(tmp_path: Path):
    (tmp_path / "plan.md").write_text("기획서 작성", encoding="utf-8")
    candidate = analyze_documents(str(tmp_path), ["plan.md"])["candidates"][0]

    saved = save_todos(str(tmp_path), [candidate])
    updated = update_todo(saved["items"][0]["id"], {"status": "done"})

    assert updated["status"] == "done"
    assert list_todos(str(tmp_path))["items"][0]["evidence"][0]["quote"] == "기획서 작성"
    assert not (tmp_path / ".aiso" / "document-todos.json").exists()


def test_saved_todo_can_be_renamed_scheduled_and_deleted(tmp_path: Path):
    (tmp_path / "plan.md").write_text("기획서 작성", encoding="utf-8")
    candidate = analyze_documents(str(tmp_path), ["plan.md"])["candidates"][0]
    saved = save_todos(str(tmp_path), [candidate])
    item_id = saved["items"][0]["id"]

    updated = update_todo(item_id, {"title": "기획서 초안 작성", "dueDate": "2026-08-31"})

    assert updated["title"] == "기획서 초안 작성"
    assert updated["dueDate"] == "2026-08-31"
    delete_todo(item_id)
    assert list_saved_todos()["items"] == []


def test_personal_calendar_event_parses_weekly_time_range_and_persists_centrally() -> None:
    item = create_calendar_todo(
        "매주 일요일 오전 10시부터 오후 8시30분까지 알바 일정 등록해줘",
        today=date(2026, 8, 14),
    )

    assert item["workspace"] == "Aiso Calendar"
    assert item["title"] == "알바"
    assert item["startDate"] == "2026-08-16"
    assert item["dueTime"] == "10:00"
    assert item["endTime"] == "20:30"
    assert item["estimatedMinutes"] == 630
    assert item["recurrence"] == {"frequency": "weekly", "weekdays": [0]}
    stored = list_saved_todos()["items"]
    assert stored[0]["recurrence"] == item["recurrence"]
    assert stored[0]["endTime"] == "20:30"
    assert stored[0]["evidence"] == [{
        "file": "Aiso Calendar", "location": "사용자 직접 등록",
        "quote": "매주 일요일 오전 10시부터 오후 8시30분까지 알바 일정 등록해줘",
    }]


def test_personal_calendar_event_keeps_ampm_in_mixed_colon_and_korean_time_range() -> None:
    item = create_calendar_todo(
        "매주 일요일 10:00부터 오후 8:30까지 알바 일정 등록해줘",
        today=date(2026, 8, 14),
    )

    assert item["title"] == "알바"
    assert item["dueTime"] == "10:00"
    assert item["endTime"] == "20:30"
    assert item["estimatedMinutes"] == 630


@pytest.mark.parametrize(
    "instruction",
    (
        "매주 일요일마다 오전 10시부터 오후 8시 30분까지 알바 일정 등록해줘",
        "매주 일요일에는 오전 10시부터 오후 8시 30분까지 알바 일정 등록해줘",
        "Register my part-time shift every Sunday from 10:00 to 20:30.",
    ),
)
def test_weekly_calendar_event_uses_named_weekday_even_with_a_korean_particle(instruction: str) -> None:
    """A Saturday registration must select the coming Sunday, never today."""
    item = create_calendar_todo(instruction, today=date(2026, 8, 15))

    assert item["startDate"] == "2026-08-16"
    assert item["dueTime"] == "10:00"
    assert item["endTime"] == "20:30"
    assert item["recurrence"] == {"frequency": "weekly", "weekdays": [0]}


def test_weekly_calendar_event_rejects_a_start_date_that_conflicts_with_its_weekday() -> None:
    with pytest.raises(ToolError, match="반복 요일"):
        create_calendar_todo(
            "매주 일요일 2026년 8월 15일부터 오전 10시 알바 일정 등록해줘",
            today=date(2026, 8, 15),
        )


def test_structured_planner_item_supports_precise_dates_times_and_recurrence() -> None:
    item = create_todo_item(
        title="주간 빌드 검토",
        priority="high",
        start_date="2026-08-17",
        end_date="2026-08-17",
        start_time="13:30",
        end_time="15:00",
        estimated_minutes=90,
        recurrence={"frequency": "weekly", "weekdays": [1]},
    )

    assert item["dueTime"] == "13:30"
    assert item["endTime"] == "15:00"
    assert item["recurrence"] == {"frequency": "weekly", "weekdays": [1]}
    assert list_saved_todos()["items"][0]["workspace"] == "Aiso Calendar"


def test_agent_can_reschedule_complete_reopen_and_delete_one_exact_todo() -> None:
    import asyncio

    item = create_todo_item(title="전투 시스템 검증", start_date="2026-08-14")
    changed = asyncio.run(manage_todo(
        "전투 시스템 검증 일정을 다음 월요일 오후 2시부터 4시까지로 옮겨줘",
        todo_id=item["id"],
        action="update",
    ))
    stored = list_saved_todos()["items"][0]
    assert "수정 완료" in changed
    assert stored["dueTime"] == "14:00"
    assert stored["endTime"] == "16:00"

    asyncio.run(manage_todo("전투 시스템 검증 완료해줘", target_title="전투 시스템 검증", action="complete"))
    assert list_saved_todos()["items"][0]["status"] == "done"
    asyncio.run(manage_todo("전투 시스템 검증 다시 열어줘", todo_id=item["id"], action="reopen"))
    assert list_saved_todos()["items"][0]["status"] == "open"
    asyncio.run(manage_todo("전투 시스템 검증 삭제해줘", todo_id=item["id"], action="delete"))
    assert list_saved_todos()["items"] == []


@pytest.mark.parametrize(
    "instruction",
    (
        "등록되어있는 전체 일정 삭제해줘",
        "저장된 모든 캘린더 일정 모두 지워줘",
        "Delete all registered calendar events.",
    ),
)
def test_agent_can_delete_every_calendar_event_only_for_an_explicit_all_scope(instruction: str) -> None:
    import asyncio

    create_todo_item(title="문서 기반 기획서 작성")
    create_calendar_todo("내일 오전 10시 개인 일정 등록해줘", today=date(2026, 8, 14))

    result = asyncio.run(manage_todo(instruction))

    assert "전체 일정 삭제 완료" in result
    assert "삭제한 일정: 2개" in result
    assert list_saved_todos()["items"] == []


def test_agent_bulk_calendar_delete_fails_closed_when_the_scope_has_a_qualifier() -> None:
    import asyncio

    create_todo_item(title="회의 준비")
    create_todo_item(title="기획서 작성")

    with pytest.raises(ToolError, match="전체 삭제"):
        asyncio.run(manage_todo("전체 일정 중 회의 준비만 삭제해줘", action="delete_all"))

    assert {item["title"] for item in list_saved_todos()["items"]} == {"회의 준비", "기획서 작성"}


def test_agent_todo_management_fails_closed_for_ambiguous_target() -> None:
    import asyncio

    create_todo_item(title="검증 준비")
    create_todo_item(title="검증 준비 문서")
    with pytest.raises(ToolError, match="여러 개"):
        asyncio.run(manage_todo("검증 준비 일정 삭제해줘", target_title="검증 준비", action="delete"))
    assert len(list_saved_todos()["items"]) == 2


@pytest.mark.parametrize(
    ("instruction", "expected_start", "expected_recurrence"),
    [
        ("매일 오전 9시 운동 일정 등록해줘", "2026-08-14", {"frequency": "daily"}),
        ("매월 20일 오후 7시 스터디 일정 추가해줘", "2026-08-20", {"frequency": "monthly", "day": 20}),
        ("매년 12월 25일 가족 모임 일정 등록해줘", "2026-12-25", {"frequency": "yearly", "month": 12, "day": 25}),
    ],
)
def test_personal_calendar_event_supports_daily_monthly_and_yearly_recurrence(
    instruction: str,
    expected_start: str,
    expected_recurrence: dict[str, object],
) -> None:
    item = create_calendar_todo(instruction, today=date(2026, 8, 14))

    assert item["startDate"] == expected_start
    assert item["recurrence"] == expected_recurrence


@pytest.mark.parametrize(
    ("instruction", "expected_start", "expected_title"),
    [
        ("내일 오후 2시 병원 일정 등록해줘", "2026-08-15", "병원"),
        ("모레 09:00 발표 리허설 일정 추가해줘", "2026-08-16", "발표 리허설"),
        ("Add a meeting tomorrow at 10:00 to my calendar.", "2026-08-15", "meeting"),
    ],
)
def test_personal_calendar_event_supports_relative_one_off_dates(
    instruction: str,
    expected_start: str,
    expected_title: str,
) -> None:
    item = create_calendar_todo(instruction, today=date(2026, 8, 14))

    assert item["startDate"] == expected_start
    assert item["recurrence"] is None
    assert item["title"] == expected_title


def test_calendar_tool_result_explicitly_confirms_aiso_not_discord() -> None:
    import asyncio

    result = asyncio.run(create_todo_event("매주 일요일 오전 10시부터 오후 8시30분까지 알바 일정 등록해줘"))

    assert "Aiso 캘린더 일정 등록 완료" in result
    assert "Discord에는 메시지나 예약을 만들지 않았습니다." in result


def test_saved_todo_keeps_a_range_duration_and_priority_after_schedule_update(tmp_path: Path):
    (tmp_path / "plan.md").write_text("일정 계획 구현", encoding="utf-8")
    candidate = analyze_documents(str(tmp_path), ["plan.md"])["candidates"][0]
    item_id = save_todos(str(tmp_path), [candidate])["items"][0]["id"]

    updated = update_todo(item_id, {
        "priority": "high",
        "startDate": "2026-08-19",
        "endDate": "2026-08-21",
        "estimatedMinutes": 120,
    })

    assert updated["priority"] == "high"
    assert updated["startDate"] == "2026-08-19"
    assert updated["endDate"] == "2026-08-21"
    assert updated["dueDate"] == "2026-08-21"
    assert updated["estimatedMinutes"] == 120
    stored = list_saved_todos()["items"][0]
    assert stored["startDate"] == "2026-08-19"
    assert stored["endDate"] == "2026-08-21"
    assert stored["estimatedMinutes"] == 120


def test_missed_work_preview_splits_duration_across_future_workdays_after_user_approval(tmp_path: Path):
    (tmp_path / "plan.md").write_text("전투 시스템 구현", encoding="utf-8")
    candidate = analyze_documents(str(tmp_path), ["plan.md"])["candidates"][0]
    item_id = save_todos(str(tmp_path), [candidate])["items"][0]["id"]
    update_todo(item_id, {
        "priority": "high",
        "startDate": "2026-08-18",
        "endDate": "2026-08-18",
        "estimatedMinutes": 120,
    })

    preview = preview_reschedule("2026-08-18")

    assert preview["totalMinutes"] == 120
    assert preview["unallocatedMinutes"] == 0
    assert preview["plans"] == [{
        "todoId": item_id,
        "title": "전투 시스템 구현",
        "priority": "high",
        "totalMinutes": 120,
        "assignments": [
            {"date": "2026-08-19", "minutes": 60},
            {"date": "2026-08-20", "minutes": 60},
        ],
        "unallocatedMinutes": 0,
    }]
    # Preview is read-only; only this explicit apply changes the calendar.
    assert list_saved_todos()["items"][0]["scheduleBlocks"] == []

    applied = apply_reschedule("2026-08-18")
    stored = applied["items"][0]

    assert stored["startDate"] == "2026-08-19"
    assert stored["endDate"] == "2026-08-20"
    assert stored["dueDate"] == "2026-08-20"
    assert stored["scheduleBlocks"] == [
        {"date": "2026-08-19", "minutes": 60},
        {"date": "2026-08-20", "minutes": 60},
    ]

    # A later manual calendar-range edit wins over the generated allocation.
    manually_moved = update_todo(item_id, {"startDate": "2026-08-24", "endDate": "2026-08-25"})
    assert manually_moved["scheduleBlocks"] == []
    assert list_saved_todos()["items"][0]["scheduleBlocks"] == []


def test_saved_todos_are_discoverable_without_a_current_workspace(tmp_path: Path):
    workspace = tmp_path / "game"
    workspace.mkdir()
    (workspace / "plan.md").write_text("발표 자료 제작", encoding="utf-8")

    candidate = analyze_documents(str(workspace), ["plan.md"])["candidates"][0]
    saved = save_todos(str(workspace), [candidate])
    global_list = list_saved_todos()

    assert global_list["items"][0]["id"] == saved["items"][0]["id"]
    assert global_list["items"][0]["workspace"] == str(workspace)
    assert global_list["workspaces"] == [str(workspace)]


def test_central_todos_survive_when_the_source_workspace_is_no_longer_available(tmp_path: Path):
    workspace = tmp_path / "removed-project"
    workspace.mkdir()
    (workspace / "plan.md").write_text("데모 영상 제작", encoding="utf-8")
    candidate = analyze_documents(str(workspace), ["plan.md"])["candidates"][0]
    saved = save_todos(str(workspace), [candidate])

    shutil.rmtree(workspace)
    global_list = list_saved_todos()

    assert global_list["items"][0]["id"] == saved["items"][0]["id"]
    assert global_list["items"][0]["title"] == "데모 영상 제작"


def test_existing_workspace_store_is_migrated_to_the_central_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "legacy-project"
    legacy_dir = workspace / ".aiso"
    legacy_dir.mkdir(parents=True)
    legacy = {
        "version": 1,
        "items": [{
            "id": "old-id", "title": "기존 발표 자료 보완", "priority": "high",
            "dueDate": None, "dueTime": None, "status": "open",
            "evidence": [{"file": "plan.pptx", "location": "슬라이드 3", "quote": "발표 자료 보완"}],
            "createdAt": "2026-08-12T00:00:00+00:00", "updatedAt": "2026-08-12T00:00:00+00:00",
        }],
    }
    (legacy_dir / "document-todos.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("AISO_DOCUMENT_TODO_BOOTSTRAP_WORKSPACES", json.dumps([str(workspace)]))

    migrated = list_saved_todos()

    assert [item["title"] for item in migrated["items"]] == ["기존 발표 자료 보완"]
    assert not (legacy_dir / "document-todos.json").exists()


def test_list_documents_excludes_internal_store_and_keeps_supported_documents(tmp_path: Path):
    (tmp_path / ".aiso").mkdir()
    (tmp_path / ".aiso" / "hidden.md").write_text("hidden", encoding="utf-8")
    (tmp_path / "brief.pdf").write_bytes(b"%PDF")
    (tmp_path / "image.png").write_bytes(b"png")

    assert list_documents(str(tmp_path)) == [{"path": "brief.pdf", "extension": ".pdf", "size": 4}]


def test_pptx_evidence_segment_keeps_slide_number(tmp_path: Path):
    target = tmp_path / "brief.pptx"
    xml = (
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>발표 자료 제작</a:t>'
        '</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
    )
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("ppt/slides/slide3.xml", xml)

    assert extract_document_segments(target)[0].location == "슬라이드 3"


def _write_presentation(target: Path, slides: dict[int, list[str]]) -> None:
    namespace = (
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    )
    with zipfile.ZipFile(target, "w") as archive:
        for number, lines in slides.items():
            paragraphs = "".join(
                f"<a:p><a:r><a:t>{escape(line)}</a:t></a:r></a:p>" for line in lines
            )
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                f"{namespace}<p:cSld><p:spTree><p:sp><p:txBody>{paragraphs}"
                "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
            )


def test_presentation_todos_are_feature_packages_not_headings_or_schedule_labels(tmp_path: Path):
    target = tmp_path / "chess.pptx"
    _write_presentation(target, {
        2: ["핵심 설계 원칙", "실력은 완전히 공정"],
        7: ["게임 모드", "일반전", "등급전", "VS CPU", "친구 대국"],
        14: ["시즌 운영", "6개월 / 시즌", "소프트 리셋", "명예의 전당"],
    })

    result = analyze_documents(str(tmp_path), ["chess.pptx"])

    assert [item["title"] for item in result["candidates"]] == [
        "일반전·등급전·VS CPU·친구 대국 모드 구현",
        "시즌 리셋·마왕 기록·명예의 전당 운영 구현",
    ]
    assert all(item["dueTime"] is None for item in result["candidates"])
    assert _due_time("6개월 / 시즌") is None


def test_reanalysis_replaces_old_items_from_the_same_document_source(tmp_path: Path):
    target = tmp_path / "chess.pptx"
    _write_presentation(target, {7: ["일반전", "등급전", "VS CPU", "친구 대국"]})
    old = {
        "id": "old-heading", "title": "핵심 설계 원칙", "priority": "medium", "dueDate": None,
        "status": "open", "evidence": [{"file": "chess.pptx", "location": "슬라이드 2", "quote": "핵심 설계 원칙"}],
    }
    save_todos(str(tmp_path), [old])

    result = analyze_documents(str(tmp_path), ["chess.pptx"])
    save_todos(str(tmp_path), result["candidates"], replace_sources={"chess.pptx"})

    assert [item["title"] for item in list_todos(str(tmp_path))["items"]] == [
        "일반전·등급전·VS CPU·친구 대국 모드 구현"
    ]


def test_agent_document_todo_tool_saves_evidence_backed_items(tmp_path: Path):
    (tmp_path / "brief.md").write_text("캐릭터 수익 모델 구축", encoding="utf-8")

    result = run_tool(tmp_path, "analyze_document_calendar", {"paths": ["brief.md"]})

    assert "brief.md · 1~1줄" in result
    assert "기존 일정을 교체해 저장했습니다" in result
    assert list_todos(str(tmp_path))["items"][0]["evidence"][0]["quote"] == "캐릭터 수익 모델 구축"
