from __future__ import annotations

import asyncio
from datetime import datetime
import json

import mydb_agent


def test_library_result_exposes_metadata_without_private_paths(monkeypatch) -> None:
    async def fake_request(path: str, *, method: str = "GET", body=None):
        assert path == "/v1/library"
        return {
            "nodes": [{
                "id": "one", "kind": "file", "title": "brief.md", "fileType": "markdown",
                "size": 12, "tags": ["plan"], "updatedAt": "2026-08-16T08:00:00Z",
                "relativePath": "files/secret/brief.md", "sourcePath": "C:/private/source/brief.md",
            }],
            "edges": [],
        }

    monkeypatch.setattr(mydb_agent, "_request", fake_request)
    result = json.loads(asyncio.run(mydb_agent.list_mydb_library()))
    wire = json.dumps(result)
    assert result["nodes"][0]["title"] == "brief.md"
    assert "relativePath" not in wire
    assert "sourcePath" not in wire
    assert "C:/private" not in wire


def test_library_can_limit_a_daily_report_to_todays_updated_nodes(monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return cls(2026, 8, 16, 12, tzinfo=tz)

    async def fake_request(path: str, *, method: str = "GET", body=None):
        assert path == "/v1/library"
        return {
            "nodes": [
                {"id": "today", "kind": "file", "title": "today.xlsx", "fileType": "spreadsheet", "size": 12, "updatedAt": "2026-08-16T08:00:00Z"},
                {"id": "older", "kind": "file", "title": "older.xlsx", "fileType": "spreadsheet", "size": 12, "updatedAt": "2026-08-15T08:00:00Z"},
            ],
            "edges": [],
        }

    monkeypatch.setattr(mydb_agent, "_request", fake_request)
    monkeypatch.setattr(mydb_agent, "datetime", FixedDateTime)
    result = json.loads(asyncio.run(mydb_agent.list_mydb_library(updated_period="today")))
    assert result["updatedPeriod"] == "today"
    assert result["totalMatches"] == 1
    assert [node["id"] for node in result["nodes"]] == ["today"]


def test_library_daily_report_keeps_changed_files_under_top_level_core(monkeypatch) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return cls(2026, 8, 16, 12, tzinfo=tz)

    async def fake_request(path: str, *, method: str = "GET", body=None):
        assert path == "/v1/library"
        return {
            "nodes": [
                {"id": "root", "kind": "core", "title": "게임데이터의설계", "updatedAt": "2026-08-01T08:00:00Z"},
                {"id": "child", "kind": "core", "title": "수업 중 실습", "updatedAt": "2026-08-01T08:00:00Z"},
                {"id": "today", "kind": "file", "title": "6강 실습.xlsx", "fileType": "spreadsheet", "size": 12, "updatedAt": "2026-08-16T08:00:00Z"},
                {"id": "loose", "kind": "file", "title": "분류 전.md", "fileType": "markdown", "size": 4, "updatedAt": "2026-08-16T08:00:00Z"},
            ],
            "edges": [
                {"sourceId": "root", "targetId": "child", "relation": "contains"},
                {"sourceId": "child", "targetId": "today", "relation": "contains"},
            ],
        }

    monkeypatch.setattr(mydb_agent, "_request", fake_request)
    monkeypatch.setattr(mydb_agent, "datetime", FixedDateTime)
    result = json.loads(asyncio.run(mydb_agent.list_mydb_library(updated_period="today")))

    root = result["hierarchy"]["roots"][0]
    assert root["core"]["title"] == "게임데이터의설계"
    assert root["children"][0]["core"]["title"] == "수업 중 실습"
    assert root["children"][0]["files"] == [{
        "id": "today", "kind": "file", "title": "6강 실습.xlsx", "updatedAt": "2026-08-16T08:00:00Z",
        "fileType": "spreadsheet", "size": 12, "tags": [],
    }]
    assert [node["title"] for node in result["hierarchy"]["unlinked"]] == ["분류 전.md"]


def test_restore_calls_only_the_exact_bridge_endpoint(monkeypatch) -> None:
    node_id = "123e4567-e89b-12d3-a456-426614174000"

    async def fake_request(path: str, *, method: str = "GET", body=None):
        assert path == "/v1/restore-node"
        assert method == "POST"
        assert body == {"nodeId": node_id}
        return {"node": {"id": node_id, "kind": "core", "title": "복구됨", "updatedAt": "2026-08-16T08:00:00Z"}}

    monkeypatch.setattr(mydb_agent, "_request", fake_request)
    result = json.loads(asyncio.run(mydb_agent.restore_mydb_trash_node(node_id)))
    assert result["restored"]["id"] == node_id
