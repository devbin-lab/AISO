# -*- coding: utf-8 -*-
"""툴 스키마 배열 + 승인 매트릭스 프리즈 — 리팩터가 이걸 바꾸면 실패한다.

- AGENT_TOOLS의 스키마·순서: KV 캐시 프리픽스(측정 11×)에 결정적이라 바이트 고정.
- 승인 매트릭스: 툴 × 모드 전체를 프리즈해 분류 드리프트를 잡는다.
"""
from __future__ import annotations

import json
from pathlib import Path

import agent
import pytest
from comfy_generation import GENERATE_IMAGE_SCHEMA
from tools import ToolError
from toolspec import (
    AGENT_TOOLS,
    CallKind,
    DEFAULT_ENABLED_TOOLS,
    MODEL_AGENT_TOOLS,
    PROGRAMMING_TOOLS,
    REGISTRY,
    get_builtin_tool_catalog,
    model_schema_for,
    model_tool_schemas,
    needs_approval,
    normalize_enabled_tool_names,
)

_SNAPSHOT = Path(__file__).parent / "_agent_tools_snapshot.json"

# 프리즈된 분류 (인벤토리 기준)
_META = {"update_plan"}  # 실질 행위 아님 — 어떤 모드에서도 승인 불필요
# 읽기(SAFE) 툴 — read 모드에서 통과, manual 모드에서만 승인
_SAFE = {
    "list_dir", "list_tree", "read_file", "grep", "glob", "create_dir",
    "get_system_time", "list_calendar_events", "web_fetch", "web_search", "search_docs",
    "list_mydb_library", "list_mydb_history", "list_mydb_trash",
}
_ALL_TOOLS = [
    "update_plan", "get_system_time", "list_calendar_events", "create_calendar_event",
    "list_mydb_library", "list_mydb_history", "list_mydb_trash", "restore_mydb_trash_node",
    "list_dir", "list_tree", "read_file", "convert_document", "analyze_document_calendar", "grep", "glob",
    "create_dir", "move", "write_file", "edit_file", "multi_edit",
    "write_code_file", "edit_code_file", "multi_edit_code_file",
    "delete_file", "delete_dir", "run_web", "run_code", "run_command",
    "web_fetch", "web_search", "create_skill", "run_skill", "search_docs",
]


def test_agent_tools_snapshot():
    """AGENT_TOOLS 스키마·순서가 프리즈된 스냅샷과 완전히 동일해야 한다."""
    expected = [
        schema for schema in json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
        if schema["function"]["name"] != "list_change_history"
    ]
    mydb_schemas = [
        REGISTRY[name].schema
        for name in ("list_mydb_library", "list_mydb_history", "list_mydb_trash", "restore_mydb_trash_node")
    ]
    calendar_index = next(index for index, schema in enumerate(expected) if schema["function"]["name"] == "list_calendar_events")
    expected = expected[:calendar_index + 1] + mydb_schemas + expected[calendar_index + 1:]
    assert agent.AGENT_TOOLS == expected
    assert "list_change_history" not in {schema["function"]["name"] for schema in agent.AGENT_TOOLS}


def test_model_tool_schemas_are_english_deep_copies_while_ui_schemas_stay_korean():
    """The model wire view must not leak back into Settings > Tools."""
    assert [tool["function"]["name"] for tool in MODEL_AGENT_TOOLS] == [
        tool["function"]["name"] for tool in AGENT_TOOLS
    ]
    model_wire = json.dumps(MODEL_AGENT_TOOLS, ensure_ascii=False)
    ui_wire = json.dumps(AGENT_TOOLS, ensure_ascii=False)
    assert not any("가" <= char <= "힣" for char in model_wire)
    assert any("가" <= char <= "힣" for char in ui_wire)

    model_copy = model_schema_for(REGISTRY["list_tree"].schema)
    model_copy["function"]["description"] = "changed only in the model copy"
    assert REGISTRY["list_tree"].schema["function"]["description"] != "changed only in the model copy"


def test_conditional_model_tool_schemas_keep_requested_order_and_use_english_contracts():
    schemas = model_tool_schemas(("web_search", "web_fetch", "discord_send"))
    assert [schema["function"]["name"] for schema in schemas] == [
        "web_search", "web_fetch", "discord_send",
    ]
    assert not any(
        "가" <= char <= "힣"
        for schema in schemas
        for char in json.dumps(schema, ensure_ascii=False)
    )


def test_saved_todos_use_the_workspace_free_async_handler():
    spec = REGISTRY["list_calendar_events"]
    assert spec.kind is CallKind.ASYNC_PLAIN
    assert spec.handler is not None


def test_calendar_todos_use_the_workspace_free_mutating_async_handler():
    spec = REGISTRY["create_calendar_event"]
    assert spec.kind is CallKind.ASYNC_PLAIN
    assert spec.handler is not None
    assert spec.mutates is True


def test_calendar_management_keeps_the_existing_approval_boundary_for_explicit_bulk_delete():
    spec = REGISTRY["manage_calendar_event"]
    action_schema = spec.schema["function"]["parameters"]["properties"]["action"]

    assert spec.kind is CallKind.ASYNC_PLAIN
    assert spec.mutates is True
    assert "delete_all" in action_schema["enum"]
    assert agent.needs_approval("manage_calendar_event", "manual") is True
    assert agent.needs_approval("manage_calendar_event", "read") is True
    assert agent.needs_approval("manage_calendar_event", "auto") is False


def test_mydb_agent_scope_is_metadata_only_with_explicit_trash_restore():
    for name in ("list_mydb_library", "list_mydb_history", "list_mydb_trash"):
        spec = REGISTRY[name]
        assert spec.kind is CallKind.ASYNC_PLAIN
        assert spec.mutates is False
        assert agent.needs_approval(name, "read") is False
    restore = REGISTRY["restore_mydb_trash_node"]
    assert restore.kind is CallKind.ASYNC_PLAIN
    assert restore.mutates is False  # My DB only; never causes workspace indexing.
    assert agent.needs_approval("restore_mydb_trash_node", "read") is True
    assert agent.needs_approval("restore_mydb_trash_node", "auto") is False


def _expected_approval(name: str, mode: str) -> bool:
    if mode == "auto":
        return False
    if name in _META:
        return False  # 메타(update_plan)는 어떤 모드에서도 승인 불필요
    if mode == "manual":
        return True  # 수동: 읽기 포함 전부 승인
    return name not in _SAFE  # read: 읽기(SAFE) 제외 전부 승인


def test_approval_matrix_frozen():
    """needs_approval(name, mode)가 모든 툴 × 모드에서 프리즈된 값과 일치."""
    for name in _ALL_TOOLS:
        for mode in ("manual", "read", "auto"):
            assert agent.needs_approval(name, mode) is _expected_approval(name, mode), (name, mode)


def test_create_dir_is_safe():
    """create_dir는 SAFE — read·auto에선 통과, manual(전부 승인)에서만 승인."""
    assert agent.needs_approval("create_dir", "read") is False
    assert agent.needs_approval("create_dir", "auto") is False
    assert agent.needs_approval("create_dir", "manual") is True


def test_auto_never_requires_tool_approval():
    """Auto is an explicit full-autonomy choice for every enabled tool."""
    for name in _ALL_TOOLS:
        assert agent.needs_approval(name, "auto") is False, name


def test_programming_policy_defaults_off_and_code_writes_keep_approval_semantics():
    """새·마이그레이션 기본값은 프로그래밍 전체 OFF, 명시적으로 켜도 기존 승인 축은 유지한다."""
    assert PROGRAMMING_TOOLS == {
        "write_code_file", "edit_code_file", "multi_edit_code_file",
        "run_web", "run_code", "run_command",
    }
    assert PROGRAMMING_TOOLS.isdisjoint(DEFAULT_ENABLED_TOOLS)
    for name in ("write_code_file", "edit_code_file", "multi_edit_code_file"):
        assert agent.needs_approval(name, "manual") is True
        assert agent.needs_approval(name, "read") is True
        assert agent.needs_approval(name, "auto") is False
    for name in ("run_web", "run_code", "run_command"):
        assert agent.needs_approval(name, "auto") is False


def test_enabled_tool_policy_rejects_unknowns_and_duplicates_fail_closed():
    assert normalize_enabled_tool_names(None) == frozenset(DEFAULT_ENABLED_TOOLS)
    with pytest.raises(ToolError, match="지원하지 않는"):
        normalize_enabled_tool_names(["read_file", "invented_tool"])
    with pytest.raises(ToolError, match="중복"):
        normalize_enabled_tool_names(["read_file", "read_file"])


def test_builtin_tool_catalog_tracks_registry_and_conditional_tools():
    """설정의 툴 목록은 실제 레지스트리·이미지 스키마와 반드시 함께 변한다."""
    catalog = get_builtin_tool_catalog()
    by_name = {item["name"]: item for item in catalog}

    assert [item["name"] for item in catalog] == [*REGISTRY, "generate_image"]
    for name, spec in REGISTRY.items():
        item = by_name[name]
        function = spec.schema["function"]
        assert item["description"] == function["description"]
        assert [param["name"] for param in item["parameters"]] == list(
            function.get("parameters", {}).get("properties", {})
        )
        assert item["mutates"] is spec.mutates
        assert item["approval"] == {
            mode: needs_approval(name, mode)
            for mode in ("manual", "read", "auto")
        }

    image = by_name["generate_image"]
    assert image["description"] == GENERATE_IMAGE_SCHEMA["function"]["description"]
    assert image["availability"] == "image"
    assert image["mutates"] is True
    assert image["approval"] == {"manual": True, "read": True, "auto": False}
    assert {"명시적 이미지 생성 요청", "ComfyUI 연결", "등록 모델 준비"} <= set(image["requirements"])
    assert by_name["search_docs"]["availability"] == "rag"
    assert "색인 완료" in by_name["search_docs"]["requirements"]
    assert by_name["discord_send"]["availability"] == "discord"
    assert by_name["discord_send"]["approval"]["auto"] is False
    todo = by_name["analyze_document_calendar"]
    assert todo["category"] == "plan"
    assert todo["availability"] == "workspace"
    assert todo["mutates"] is True
    assert todo["approval"] == {"manual": True, "read": True, "auto": False}
    calendar = by_name["create_calendar_event"]
    assert calendar["category"] == "plan"
    assert calendar["availability"] == "always"
    assert calendar["mutates"] is True
    assert calendar["approval"] == {"manual": True, "read": True, "auto": False}
    for name in PROGRAMMING_TOOLS:
        assert by_name[name]["category"] == "programming"
        assert by_name[name]["availability"] == "workspace"
        assert "설정에서 프로그래밍 도구 사용" in by_name[name]["requirements"]
