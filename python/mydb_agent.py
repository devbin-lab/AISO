"""Narrow Agent access to the user-owned My DB library.

The SQLite database and managed files remain exclusively in Electron main.
This module talks to a per-launch, token-protected loopback bridge which offers
only metadata lookup, history lookup, trash lookup, and one-item restoration.
There is intentionally no create, edit, delete, link, export, or file-content
operation here.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from tools import ToolError


_BRIDGE_URL = os.getenv("AISO_MYDB_AGENT_BRIDGE_URL", "").rstrip("/")
_BRIDGE_TOKEN = os.getenv("AISO_MYDB_AGENT_BRIDGE_TOKEN", "")
_REQUEST_TIMEOUT_SECONDS = 4
_MAX_LIBRARY_ITEMS = 240
_MAX_HISTORY_ITEMS = 80
_MAX_TRASH_ITEMS = 80


LIST_MYDB_LIBRARY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_mydb_library",
        "description": "My DB에 저장된 코어·파일·관계를 읽기 전용으로 조회한다. 파일 원문이나 외부 원본 경로는 읽지 않는다.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "이름 또는 태그로 좁힐 선택 검색어"},
                "updated_period": {
                    "type": "string",
                    "enum": ["today", "last_24_hours", "week", "all"],
                    "description": "마지막으로 추가·수정된 기간. 생략하면 전체 라이브러리를 조회한다.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["all", "core", "file"],
                    "description": "반환할 항목 종류. 코어 이름만 필요하면 core를 사용한다.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIBRARY_ITEMS, "description": "반환할 최대 항목 수"},
            },
            "additionalProperties": False,
        },
    },
}

LIST_MYDB_HISTORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_mydb_history",
        "description": "My DB 변경 이력을 읽기 전용으로 조회한다. 오늘 또는 최근 24시간 변경 보고를 만들 때 사용한다.",
        "parameters": {
            "type": "object",
            "properties": {
                "period": {
                    "type": "string",
                    "enum": ["today", "last_24_hours", "week", "all"],
                    "description": "조회 기간. 생략하면 오늘의 로컬 변경 이력을 조회한다.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_HISTORY_ITEMS, "description": "반환할 최대 이력 수"},
            },
            "additionalProperties": False,
        },
    },
}

LIST_MYDB_TRASH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_mydb_trash",
        "description": "My DB 휴지통의 코어·파일을 읽기 전용으로 조회한다. 삭제하거나 비우지 않는다.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}

RESTORE_MYDB_TRASH_NODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "restore_mydb_trash_node",
        "description": "My DB 휴지통의 정확한 항목 ID 하나를 복구한다. 새 항목을 만들거나 기존 항목을 수정·삭제하지 않는다.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "minLength": 36, "maxLength": 36, "description": "list_mydb_trash 결과의 정확한 항목 ID"},
            },
            "required": ["node_id"],
            "additionalProperties": False,
        },
    },
}


def _bridge_request(path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _BRIDGE_URL or not _BRIDGE_TOKEN:
        raise ToolError("My DB Agent 연결이 준비되지 않았습니다. Aiso를 다시 시작한 뒤 시도해 주세요.")
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = Request(
        f"{_BRIDGE_URL}{path}",
        data=payload,
        method=method,
        headers={
            "X-Aiso-Mydb-Agent-Token": _BRIDGE_TOKEN,
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed loopback URL from Electron main
            raw = response.read(2 * 1024 * 1024)
    except HTTPError as error:
        if error.code == 404:
            raise ToolError("요청한 My DB 항목을 찾을 수 없습니다.") from error
        raise ToolError("My DB 요청을 처리하지 못했습니다.") from error
    except (URLError, OSError) as error:
        raise ToolError("My DB Agent 연결이 끊어졌습니다. Aiso를 다시 시작한 뒤 시도해 주세요.") from error
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ToolError("My DB 응답 형식이 올바르지 않습니다.") from error
    if not isinstance(value, dict):
        raise ToolError("My DB 응답 형식이 올바르지 않습니다.")
    return value


async def _request(path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    return await asyncio.to_thread(_bridge_request, path, method=method, body=body)


def _limit(value: Any, *, maximum: int, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ToolError(f"조회 개수는 1~{maximum} 사이의 정수여야 합니다.")
    return value


def _node_summary(node: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": str(node.get("id") or ""),
        "kind": str(node.get("kind") or ""),
        "title": str(node.get("title") or ""),
        "updatedAt": str(node.get("updatedAt") or ""),
    }
    if result["kind"] == "file":
        result.update({
            "fileType": str(node.get("fileType") or "other"),
            "size": int(node.get("size") or 0),
            "tags": [str(tag) for tag in node.get("tags", []) if isinstance(tag, str)][:12],
        })
    return result


def _library_hierarchy(
    nodes_by_id: dict[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    selected_ids: set[str],
) -> dict[str, Any]:
    """Return the selected library slice in root-core-to-leaf order.

    The agent must not infer folder membership from titles.  This derives a
    compact reporting tree only from My DB's explicit ``contains`` relations,
    while retaining the ancestor cores needed to explain where a changed file
    belongs.
    """
    core_parent: dict[str, str] = {}
    files_by_core: dict[str, list[str]] = {}
    child_cores: dict[str, list[str]] = {}
    file_owner: dict[str, str] = {}
    for edge in edges:
        if str(edge.get("relation") or "") != "contains":
            continue
        source_id = str(edge.get("sourceId") or "")
        target_id = str(edge.get("targetId") or "")
        source = nodes_by_id.get(source_id)
        target = nodes_by_id.get(target_id)
        if not isinstance(source, dict) or source.get("kind") != "core" or not isinstance(target, dict):
            continue
        if target.get("kind") == "core" and target_id not in core_parent:
            core_parent[target_id] = source_id
            child_cores.setdefault(source_id, []).append(target_id)
        elif target.get("kind") == "file" and target_id not in file_owner:
            file_owner[target_id] = source_id
            files_by_core.setdefault(source_id, []).append(target_id)

    included_cores: set[str] = set()
    included_files: set[str] = set()
    ungrouped_ids: set[str] = set()
    for node_id in selected_ids:
        node = nodes_by_id.get(node_id)
        if not isinstance(node, dict):
            continue
        if node.get("kind") == "file":
            included_files.add(node_id)
            parent_id = file_owner.get(node_id)
            if parent_id is None:
                ungrouped_ids.add(node_id)
                continue
            cursor = parent_id
        elif node.get("kind") == "core":
            cursor = node_id
        else:
            ungrouped_ids.add(node_id)
            continue
        seen: set[str] = set()
        while cursor and cursor not in seen:
            seen.add(cursor)
            included_cores.add(cursor)
            cursor = core_parent.get(cursor, "")

    def sort_ids(ids: list[str]) -> list[str]:
        return sorted(ids, key=lambda node_id: str(nodes_by_id.get(node_id, {}).get("title") or "").casefold())

    def render_core(core_id: str) -> dict[str, Any]:
        core = nodes_by_id[core_id]
        return {
            "core": _node_summary(core),
            "matched": core_id in selected_ids,
            "files": [
                _node_summary(nodes_by_id[file_id])
                for file_id in sort_ids(files_by_core.get(core_id, []))
                if file_id in included_files and file_id in nodes_by_id
            ],
            "children": [
                render_core(child_id)
                for child_id in sort_ids(child_cores.get(core_id, []))
                if child_id in included_cores and child_id in nodes_by_id
            ],
        }

    roots = [
        core_id for core_id in included_cores
        if core_parent.get(core_id) not in included_cores
    ]
    return {
        "roots": [render_core(core_id) for core_id in sort_ids(roots)],
        "unlinked": [
            _node_summary(nodes_by_id[node_id])
            for node_id in sort_ids(list(ungrouped_ids))
            if node_id in nodes_by_id
        ],
    }


async def list_mydb_library(
    query: str = "",
    updated_period: str = "all",
    kind: str = "all",
    limit: int | None = None,
) -> str:
    """Return safe library metadata without file contents or source paths."""
    if not isinstance(query, str):
        raise ToolError("검색어는 문자열이어야 합니다.")
    count = _limit(limit, maximum=_MAX_LIBRARY_ITEMS, default=60)
    if not isinstance(updated_period, str):
        raise ToolError("조회 기간이 올바르지 않습니다.")
    if kind not in {"all", "core", "file"}:
        raise ToolError("항목 종류가 올바르지 않습니다.")
    now = datetime.now().astimezone()
    updated_after = _history_start(updated_period, now)
    snapshot = await _request("/v1/library")
    nodes = [node for node in snapshot.get("nodes", []) if isinstance(node, dict)]
    if updated_after is not None:
        nodes = [
            node for node in nodes
            if (timestamp := _parse_timestamp(node.get("updatedAt"))) is not None and timestamp >= updated_after
        ]
    if kind != "all":
        nodes = [node for node in nodes if node.get("kind") == kind]
    needle = query.strip().casefold()
    if needle:
        nodes = [
            node for node in nodes
            if needle in str(node.get("title") or "").casefold()
            or any(needle in str(tag).casefold() for tag in node.get("tags", []) if isinstance(tag, str))
        ]
    selected = nodes[:count]
    selected_ids = {str(node.get("id") or "") for node in selected}
    nodes_by_id = {str(node.get("id") or ""): node for node in snapshot.get("nodes", []) if isinstance(node, dict)}
    edges = [
        edge for edge in snapshot.get("edges", [])
        if isinstance(edge, dict)
        and str(edge.get("sourceId") or "") in selected_ids
        and str(edge.get("targetId") or "") in selected_ids
    ][: max(count * 2, 60)]
    core_groups: dict[str, dict[str, Any]] = {}
    for edge in snapshot.get("edges", []):
        if not isinstance(edge, dict) or str(edge.get("relation") or "") != "contains":
            continue
        source_id = str(edge.get("sourceId") or "")
        target_id = str(edge.get("targetId") or "")
        source = nodes_by_id.get(source_id)
        target = nodes_by_id.get(target_id)
        if not isinstance(source, dict) or source.get("kind") != "core" or not isinstance(target, dict) or target_id not in selected_ids:
            continue
        group = core_groups.setdefault(source_id, {"coreId": source_id, "coreTitle": str(source.get("title") or ""), "coreCount": 0, "fileCount": 0})
        if target.get("kind") == "core":
            group["coreCount"] += 1
        elif target.get("kind") == "file":
            group["fileCount"] += 1
    file_types = Counter(str(node.get("fileType") or "other") for node in selected if node.get("kind") == "file")
    return json.dumps({
        "query": query.strip() or None,
        "updatedPeriod": updated_period,
        "kind": kind,
        "from": updated_after.isoformat() if updated_after is not None else None,
        "to": now.isoformat(),
        "totalMatches": len(nodes),
        "returned": len(selected),
        "summary": {
            "cores": sum(1 for node in selected if node.get("kind") == "core"),
            "files": sum(1 for node in selected if node.get("kind") == "file"),
            "fileTypes": dict(sorted(file_types.items())),
            "coreGroups": sorted(core_groups.values(), key=lambda group: (-int(group["fileCount"]), str(group["coreTitle"]).casefold())),
        },
        "hierarchy": _library_hierarchy(nodes_by_id, [edge for edge in snapshot.get("edges", []) if isinstance(edge, dict)], selected_ids),
        "nodes": [_node_summary(node) for node in selected],
        "relations": [
            {
                "sourceId": str(edge.get("sourceId") or ""),
                "targetId": str(edge.get("targetId") or ""),
                "relation": str(edge.get("relation") or "related"),
            }
            for edge in edges
        ],
        "privacy": "My DB metadata only. File contents and external source paths were not read.",
    }, ensure_ascii=False)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone()


def _history_start(period: str, now: datetime) -> datetime | None:
    if period == "all":
        return None
    if period == "week":
        return now - timedelta(days=7)
    if period == "last_24_hours":
        return now - timedelta(hours=24)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ToolError("조회 기간이 올바르지 않습니다.")


async def list_mydb_history(period: str = "today", limit: int | None = None) -> str:
    """Return bounded, source-path-free My DB audit history for reporting."""
    if not isinstance(period, str):
        raise ToolError("조회 기간이 올바르지 않습니다.")
    count = _limit(limit, maximum=_MAX_HISTORY_ITEMS, default=40)
    now = datetime.now().astimezone()
    start = _history_start(period, now)
    snapshot = await _request("/v1/history")
    entries = [entry for entry in snapshot.get("entries", []) if isinstance(entry, dict)]
    if start is not None:
        entries = [entry for entry in entries if (timestamp := _parse_timestamp(entry.get("createdAt"))) is not None and timestamp >= start]
    selected = entries[:count]
    actions = Counter(str(entry.get("action") or "unknown") for entry in entries)
    safe_entries = [
        {
            "id": str(entry.get("id") or ""),
            "action": str(entry.get("action") or "unknown"),
            "subject": str(entry.get("subjectTitle") or ""),
            "subjectKind": str(entry.get("subjectKind") or ""),
            "related": str(entry.get("relatedTitle") or "") or None,
            "detail": str(entry.get("detail") or "") or None,
            "createdAt": str(entry.get("createdAt") or ""),
        }
        for entry in selected
    ]
    return json.dumps({
        "period": period,
        "from": start.isoformat() if start is not None else None,
        "to": now.isoformat(),
        "totalChanges": len(entries),
        "actionCounts": dict(sorted(actions.items())),
        "entries": safe_entries,
        "privacy": "My DB history metadata only. File contents and external source paths were not read.",
    }, ensure_ascii=False)


async def list_mydb_trash() -> str:
    snapshot = await _request("/v1/trash")
    nodes = [node for node in snapshot.get("nodes", []) if isinstance(node, dict)][: _MAX_TRASH_ITEMS]
    return json.dumps({
        "count": len(nodes),
        "nodes": [_node_summary(node) for node in nodes],
        "note": "Only restore is supported through Agent; delete and permanent purge are unavailable.",
    }, ensure_ascii=False)


async def restore_mydb_trash_node(node_id: str) -> str:
    if not isinstance(node_id, str):
        raise ToolError("복구할 My DB 항목 ID가 올바르지 않습니다.")
    result = await _request("/v1/restore-node", method="POST", body={"nodeId": node_id})
    node = result.get("node")
    if not isinstance(node, dict):
        raise ToolError("My DB 항목을 복구하지 못했습니다.")
    return json.dumps({"restored": _node_summary(node)}, ensure_ascii=False)


__all__ = (
    "LIST_MYDB_HISTORY_SCHEMA",
    "LIST_MYDB_LIBRARY_SCHEMA",
    "LIST_MYDB_TRASH_SCHEMA",
    "RESTORE_MYDB_TRASH_NODE_SCHEMA",
    "list_mydb_history",
    "list_mydb_library",
    "list_mydb_trash",
    "restore_mydb_trash_node",
)
