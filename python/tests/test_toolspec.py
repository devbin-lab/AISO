# -*- coding: utf-8 -*-
"""툴 스키마 배열 + 승인 매트릭스 프리즈 — 리팩터가 이걸 바꾸면 실패한다.

- AGENT_TOOLS의 스키마·순서: KV 캐시 프리픽스(측정 11×)에 결정적이라 바이트 고정.
- 승인 매트릭스: 툴 × 모드 전체를 프리즈해 분류 드리프트를 잡는다.
"""
from __future__ import annotations

import json
from pathlib import Path

import agent

_SNAPSHOT = Path(__file__).parent / "_agent_tools_snapshot.json"

# 프리즈된 분류 (인벤토리 기준)
_META = {"update_plan"}  # 실질 행위 아님 — 어떤 모드에서도 승인 불필요
# 읽기(SAFE) 툴 — read 모드에서 통과, manual 모드에서만 승인
_SAFE = {
    "list_dir", "list_tree", "read_file", "grep", "glob", "create_dir",
    "run_web", "run_code", "web_fetch", "web_search", "search_docs",
}
_ALL_TOOLS = [
    "update_plan", "list_dir", "list_tree", "read_file", "grep", "glob",
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
