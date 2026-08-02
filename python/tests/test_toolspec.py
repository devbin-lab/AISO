# -*- coding: utf-8 -*-
"""툴 스키마 배열 + 승인 매트릭스 프리즈 — 리팩터가 이걸 바꾸면 실패한다.

- AGENT_TOOLS의 스키마·순서: KV 캐시 프리픽스(측정 11×)에 결정적이라 바이트 고정.
- 승인 매트릭스: 툴 × 모드 전체를 프리즈해 분류 드리프트를 잡는다.
"""
from __future__ import annotations

import json
from pathlib import Path

import agent
from comfy_generation import GENERATE_IMAGE_SCHEMA
from toolspec import FORCE_APPROVAL_IN_AUTO, REGISTRY, get_builtin_tool_catalog, needs_approval

_SNAPSHOT = Path(__file__).parent / "_agent_tools_snapshot.json"

# 프리즈된 분류 (인벤토리 기준)
_META = {"update_plan"}  # 실질 행위 아님 — 어떤 모드에서도 승인 불필요
# 읽기(SAFE) 툴 — read 모드에서 통과, manual 모드에서만 승인
_SAFE = {
    "list_dir", "list_tree", "read_file", "grep", "glob", "create_dir",
    "get_system_time", "web_fetch", "web_search", "search_docs",
}
_AUTO_APPROVAL_REQUIRED = {
    "delete_file", "delete_dir", "run_web", "run_code", "run_command",
    "create_skill", "run_skill",
}
_ALL_TOOLS = [
    "update_plan", "get_system_time", "list_dir", "list_tree", "read_file", "grep", "glob",
    "create_dir", "move", "write_file", "edit_file", "multi_edit",
    "delete_file", "delete_dir", "run_web", "run_code", "run_command",
    "web_fetch", "web_search", "create_skill", "run_skill", "search_docs",
]


def test_agent_tools_snapshot():
    """AGENT_TOOLS 스키마·순서가 프리즈된 스냅샷과 완전히 동일해야 한다."""
    expected = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    assert agent.AGENT_TOOLS == expected


def _expected_approval(name: str, mode: str) -> bool:
    if mode == "auto":
        return name in _AUTO_APPROVAL_REQUIRED
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


def test_auto_keeps_execution_and_deletion_behind_approval():
    """Auto mode is not an escape hatch for arbitrary execution or deletion."""
    for name in _AUTO_APPROVAL_REQUIRED:
        assert agent.needs_approval(name, "auto") is True, name


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
            mode: needs_approval(name, mode) or (mode == "auto" and name in FORCE_APPROVAL_IN_AUTO)
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
    assert by_name["discord_send"]["approval"]["auto"] is True
