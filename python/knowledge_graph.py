"""Aiso-owned relationship graph and verified change history.

This module deliberately keeps graph data in its own SQLite file.  It never
copies, moves, or indexes arbitrary user files: nodes are created only from
explicit Aiso actions, and the agent history records a change only after the
underlying tool reports success.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid4, uuid5


_DATABASE_ENV = "AISO_KNOWLEDGE_GRAPH_DB_PATH"
_PATH_OVERRIDE: ContextVar[Path | None] = ContextVar("aiso_knowledge_graph_path", default=None)
_MAX_GRAPH_NODES = 240
_MAX_HISTORY = 100


CHANGE_HISTORY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_change_history",
        "description": (
            "\uc131\uacf5\ud55c \uc2e4\uc81c Aiso \ub3c4\uad6c \uc2e4\ud589\uc73c\ub85c\ub9cc \uae30\ub85d\ub41c \ubcc0\uacbd \uc774\ub825\uc744 \uc870\ud68c\ud569\ub2c8\ub2e4. "
            "\ubaa8\ub378 \uc751\ub2f5\uc774\ub098 \uc2e4\ud328\ud55c \ud638\ucd9c\uc740 \ud3ec\ud568\ud558\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 40,
                    "description": "\uc870\ud68c\ud560 \ubcc0\uacbd \uc774\ub825 \ud56d\ubaa9 \uc218.",
                }
            },
            "additionalProperties": False,
        },
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _database_path() -> Path:
    override = _PATH_OVERRIDE.get()
    if override is not None:
        return override
    configured = os.environ.get(_DATABASE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".aiso" / "knowledge-graph.sqlite3"


@contextmanager
def temporary_knowledge_database(path: str | Path):
    """Use an isolated graph database in a test or one maintenance operation."""
    token = _PATH_OVERRIDE.set(Path(path).expanduser().resolve())
    try:
        yield
    finally:
        _PATH_OVERRIDE.reset(token)


def _open() -> sqlite3.Connection:
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge_nodes (
          id TEXT PRIMARY KEY,
          source_key TEXT NOT NULL UNIQUE,
          kind TEXT NOT NULL,
          title TEXT NOT NULL,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_nodes_kind_updated
          ON knowledge_nodes(kind, updated_at DESC);

        CREATE TABLE IF NOT EXISTS knowledge_edges (
          id TEXT PRIMARY KEY,
          source_id TEXT NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
          target_id TEXT NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
          relation TEXT NOT NULL,
          metadata_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(source_id, target_id, relation)
        );
        CREATE INDEX IF NOT EXISTS idx_knowledge_edges_source ON knowledge_edges(source_id);
        CREATE INDEX IF NOT EXISTS idx_knowledge_edges_target ON knowledge_edges(target_id);

        CREATE TABLE IF NOT EXISTS change_history (
          id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          session_id TEXT NOT NULL,
          assistant_turn_id TEXT NOT NULL,
          tool_name TEXT NOT NULL,
          summary TEXT NOT NULL,
          details_json TEXT NOT NULL DEFAULT '{}',
          actor_node_id TEXT REFERENCES knowledge_nodes(id) ON DELETE SET NULL,
          target_node_id TEXT REFERENCES knowledge_nodes(id) ON DELETE SET NULL
        );
        CREATE INDEX IF NOT EXISTS idx_change_history_created ON change_history(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_change_history_session ON change_history(session_id, created_at DESC);
        """
    )
    return connection


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


def _metadata(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _node_id(source_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"aiso-knowledge:{source_key}"))


def _clean_title(value: Any, fallback: str) -> str:
    text = " ".join(str(value or "").strip().split())
    return (text or fallback)[:160]


def _upsert_node(
    connection: sqlite3.Connection,
    *,
    source_key: str,
    kind: str,
    title: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    node_id = _node_id(source_key)
    now = _now()
    connection.execute(
        """
        INSERT INTO knowledge_nodes (id, source_key, kind, title, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
          kind=excluded.kind, title=excluded.title, metadata_json=excluded.metadata_json,
          updated_at=excluded.updated_at
        """,
        (node_id, source_key, kind, _clean_title(title, kind), _json(metadata or {}), now, now),
    )
    return node_id


def _ensure_edge(
    connection: sqlite3.Connection,
    source_id: str,
    target_id: str,
    relation: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    if source_id == target_id:
        return ""
    now = _now()
    row = connection.execute(
        "SELECT id FROM knowledge_edges WHERE source_id = ? AND target_id = ? AND relation = ?",
        (source_id, target_id, relation),
    ).fetchone()
    edge_id = str(row["id"]) if row else str(uuid4())
    connection.execute(
        """
        INSERT INTO knowledge_edges (id, source_id, target_id, relation, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
          metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
        """,
        (edge_id, source_id, target_id, relation, _json(metadata or {}), now, now),
    )
    return edge_id


_CHANGE_TOOLS = frozenset({
    "create_dir", "write_file", "edit_file", "multi_edit", "write_code_file",
    "edit_code_file", "multi_edit_code_file", "delete_file", "delete_dir", "move",
    "convert_document", "analyze_document_calendar", "create_calendar_event",
    "manage_calendar_event", "create_skill", "run_skill", "generate_image",
    "discord_server_apply", "discord_send", "discord_schedule_add",
    "discord_channel_report_add", "discord_schedule_remove",
})

_TOOL_SUMMARIES = {
    "create_dir": "폴더를 만들었습니다",
    "write_file": "문서를 작성했습니다",
    "edit_file": "문서를 수정했습니다",
    "multi_edit": "문서를 여러 곳 수정했습니다",
    "write_code_file": "파일을 작성했습니다",
    "edit_code_file": "파일을 수정했습니다",
    "multi_edit_code_file": "파일을 여러 곳 수정했습니다",
    "delete_file": "파일을 삭제했습니다",
    "delete_dir": "폴더를 삭제했습니다",
    "move": "파일 또는 폴더를 이동했습니다",
    "convert_document": "문서 읽기 사본을 만들었습니다",
    "analyze_document_calendar": "문서 근거로 캘린더 작업을 등록했습니다",
    "create_calendar_event": "캘린더 일정을 등록했습니다",
    "manage_calendar_event": "캘린더 일정을 변경했습니다",
    "create_skill": "자동화 도구를 만들었습니다",
    "run_skill": "자동화 도구를 실행했습니다",
    "generate_image": "이미지를 생성했습니다",
    "discord_server_apply": "Discord 서버 구성을 변경했습니다",
    "discord_send": "Discord 메시지를 보냈습니다",
    "discord_schedule_add": "Discord 예약을 등록했습니다",
    "discord_channel_report_add": "Discord 채널 보고 예약을 등록했습니다",
    "discord_schedule_remove": "Discord 예약을 제거했습니다",
}


def _path_from_args(tool_name: str, args: dict[str, Any]) -> tuple[str, str] | None:
    if tool_name == "move":
        raw = args.get("dst") or args.get("src")
    elif tool_name == "convert_document":
        raw = args.get("output_path") or args.get("path")
    elif tool_name == "analyze_document_calendar":
        paths = args.get("paths")
        raw = paths[0] if isinstance(paths, list) and paths else None
    else:
        raw = args.get("path") or args.get("file")
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = raw.strip().replace("\\", "/").lstrip("/")[:300]
    name = Path(path).name or path
    kind = "document" if Path(name).suffix.lower() in {
        ".pdf", ".pptx", ".pptm", ".docx", ".xlsx", ".xlsm", ".hwp", ".hwpx", ".txt", ".md", ".csv"
    } else "file"
    return kind, path


def _safe_details(args: dict[str, Any], result: str) -> dict[str, Any]:
    """Persist stable references, never contents, prompts, credentials, or outputs."""
    keep: dict[str, Any] = {}
    for key in ("path", "src", "dst", "output_path", "todo_id", "target_title", "action", "name", "channel"):
        value = args.get(key)
        if isinstance(value, (str, int, float, bool)):
            keep[key] = str(value)[:240]
    return keep


def record_agent_tool_activity(
    *,
    session_id: str,
    assistant_turn_id: str,
    conversation_id: str = "",
    workspace: str = "",
    tool_name: str,
    arguments: dict[str, Any],
    result: str,
    ok: bool,
) -> None:
    """Project actual successful actions into the relation graph.

    This runs behind the real execution boundary.  A model sentence never
    creates history, and a failed/no-op tool call never becomes a change.
    """
    if not ok or tool_name not in _CHANGE_TOOLS:
        return
    if str(result or "").lstrip().startswith("[NO_CHANGE]"):
        return
    session_key = f"agent-session:{session_id or assistant_turn_id}"
    connection = _open()
    try:
        connection.execute("BEGIN")
        session = _upsert_node(
            connection,
            source_key=session_key,
            kind="agent_session",
            title="에이전트 작업 실행",
            metadata={"workspace": Path(workspace).name if workspace else "", "sessionId": session_id[:80]},
        )
        if conversation_id:
            conversation = _upsert_node(
                connection,
                source_key=f"conversation:{conversation_id}",
                kind="conversation",
                title="에이전트 대화",
                metadata={"conversationId": conversation_id},
            )
            _ensure_edge(connection, conversation, session, "contains")

        target: str | None = None
        path_target = _path_from_args(tool_name, arguments)
        if path_target:
            kind, path = path_target
            workspace_key = Path(workspace).as_posix().casefold() if workspace else "local"
            target = _upsert_node(
                connection,
                source_key=f"{kind}:{workspace_key}:{path.casefold()}",
                kind=kind,
                title=Path(path).name,
                metadata={"path": path, "workspace": Path(workspace).name if workspace else ""},
            )
        elif tool_name in {"create_calendar_event", "manage_calendar_event", "analyze_document_calendar"}:
            target = _upsert_node(
                connection,
                source_key="calendar:aiso",
                kind="calendar",
                title="Aiso 캘린더",
            )
        elif tool_name == "generate_image":
            target = _upsert_node(
                connection,
                source_key=f"image:{assistant_turn_id}:{tool_name}",
                kind="image",
                title="생성 이미지",
            )
        elif tool_name.startswith("discord_"):
            target = _upsert_node(
                connection,
                source_key="integration:discord",
                kind="integration",
                title="Discord",
            )
        elif tool_name in {"create_skill", "run_skill"}:
            target = _upsert_node(
                connection,
                source_key=f"skill:{str(arguments.get('name') or 'automation').strip().casefold()}",
                kind="skill",
                title=_clean_title(arguments.get("name"), "Aiso 자동화"),
            )
        if target:
            _ensure_edge(connection, session, target, "changed", {"tool": tool_name})

        connection.execute(
            """
            INSERT INTO change_history (id, created_at, session_id, assistant_turn_id, tool_name, summary, details_json, actor_node_id, target_node_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()), _now(), session_id[:160], assistant_turn_id[:160], tool_name,
                _TOOL_SUMMARIES.get(tool_name, "에이전트 작업을 완료했습니다"),
                _json(_safe_details(arguments, result)), session, target,
            ),
        )
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
    finally:
        connection.close()


def _node_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "kind": row["kind"], "title": row["title"],
        "metadata": _metadata(row["metadata_json"]), "createdAt": row["created_at"], "updatedAt": row["updated_at"],
    }


def graph_snapshot(limit: int = _MAX_GRAPH_NODES) -> dict[str, Any]:
    cap = max(1, min(int(limit or _MAX_GRAPH_NODES), _MAX_GRAPH_NODES))
    connection = _open()
    try:
        rows = connection.execute(
            "SELECT * FROM knowledge_nodes ORDER BY updated_at DESC LIMIT ?", (cap,)
        ).fetchall()
        node_ids = {str(row["id"]) for row in rows}
        if node_ids:
            marks = ",".join("?" for _ in node_ids)
            edge_rows = connection.execute(
                f"SELECT * FROM knowledge_edges WHERE source_id IN ({marks}) AND target_id IN ({marks}) ORDER BY updated_at DESC",
                tuple(node_ids) * 2,
            ).fetchall()
        else:
            edge_rows = []
        changes = connection.execute(
            "SELECT * FROM change_history ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        return {
            "nodes": [_node_dict(row) for row in rows],
            "edges": [
                {"id": row["id"], "sourceId": row["source_id"], "targetId": row["target_id"], "relation": row["relation"], "metadata": _metadata(row["metadata_json"]), "updatedAt": row["updated_at"]}
                for row in edge_rows
            ],
            "changes": [
                {"id": row["id"], "createdAt": row["created_at"], "toolName": row["tool_name"], "summary": row["summary"], "details": _metadata(row["details_json"]), "targetNodeId": row["target_node_id"]}
                for row in changes
            ],
        }
    finally:
        connection.close()


def add_manual_relation(source_id: str, target_id: str, relation: str = "related") -> dict[str, Any]:
    relation = relation.strip().lower()
    if relation not in {"related", "references", "contains", "depends_on"}:
        raise ValueError("Unsupported relation")
    connection = _open()
    try:
        source = connection.execute("SELECT id FROM knowledge_nodes WHERE id = ?", (source_id,)).fetchone()
        target = connection.execute("SELECT id FROM knowledge_nodes WHERE id = ?", (target_id,)).fetchone()
        if source is None or target is None:
            raise ValueError("Unknown graph node")
        connection.execute("BEGIN")
        edge_id = _ensure_edge(connection, source_id, target_id, relation, {"origin": "user"})
        connection.commit()
        return {"id": edge_id, "sourceId": source_id, "targetId": target_id, "relation": relation}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_manual_topic(title: str) -> dict[str, Any]:
    clean = _clean_title(title, "새 주제")
    if not clean:
        raise ValueError("A topic title is required")
    connection = _open()
    try:
        connection.execute("BEGIN")
        source_key = f"topic:{uuid4()}"
        node_id = _upsert_node(connection, source_key=source_key, kind="topic", title=clean, metadata={"origin": "user"})
        row = connection.execute("SELECT * FROM knowledge_nodes WHERE id = ?", (node_id,)).fetchone()
        connection.commit()
        return _node_dict(row)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def format_change_history(limit: int = 20) -> str:
    cap = max(1, min(int(limit or 20), 40))
    connection = _open()
    try:
        rows = connection.execute(
            "SELECT created_at, tool_name, summary, details_json FROM change_history ORDER BY created_at DESC LIMIT ?", (cap,)
        ).fetchall()
        if not rows:
            return "Verified Aiso change history is empty."
        entry_label = "entry" if len(rows) == 1 else "entries"
        lines = [
            f"Verified Aiso change history ({len(rows)} {entry_label}; newest first). "
            "Every entry below came from a successful tool execution."
        ]
        for index, row in enumerate(rows, start=1):
            details = _metadata(row["details_json"])
            target = next(
                (str(details[key]) for key in ("path", "dst", "output_path", "name", "target_title", "todo_id") if details.get(key)),
                "",
            )
            suffix = f" — {target}" if target else ""
            lines.append(f"{index}. [{row['created_at']}] {row['summary']}{suffix}")
        return "\n".join(lines)
    finally:
        connection.close()


async def list_change_history(limit: int = 20) -> str:
    return format_change_history(limit)


__all__ = (
    "add_manual_relation", "create_manual_topic", "format_change_history", "graph_snapshot",
    "list_change_history", "record_agent_tool_activity", "temporary_knowledge_database",
)
