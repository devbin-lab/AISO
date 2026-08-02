"""Crash-surviving, fail-closed execution ledger for provider tool calls."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
import time
from typing import Literal


LEDGER_SCHEMA_VERSION = 1
MAX_LEDGER_RESULT_CHARS = 16 * 1024

TerminalStatus = Literal["completed", "failed", "rejected"]
LedgerStatus = Literal[
    "pending", "awaiting_approval", "running", "completed", "failed", "rejected", "indeterminate"
]


class LedgerError(RuntimeError):
    pass


class LedgerProtocolConflict(LedgerError):
    pass


class LedgerInProgress(LedgerError):
    pass


class LedgerIndeterminate(LedgerError):
    pass


@dataclass(frozen=True)
class LedgerKey:
    session_id: str
    assistant_turn_id: str
    provider_tool_call_id: str


@dataclass(frozen=True)
class LedgerRecord:
    key: LedgerKey
    tool_name: str
    arguments_hash: str
    status: LedgerStatus
    result: str
    ok: bool
    rejected: bool
    approval_id: str
    execution_id: str

    @property
    def reusable(self) -> bool:
        return self.status in ("completed", "failed", "rejected")


def canonical_arguments_hash(canonical_arguments: str) -> str:
    return hashlib.sha256(canonical_arguments.encode("utf-8")).hexdigest()


def bound_ledger_result(result: str) -> str:
    text = str(result)
    if len(text) <= MAX_LEDGER_RESULT_CHARS:
        return text
    marker = "\n[결과가 원장 허용 길이에서 잘렸습니다.]"
    return text[: MAX_LEDGER_RESULT_CHARS - len(marker)] + marker


def _validate_key(key: LedgerKey) -> None:
    for label, value, limit in (
        ("sessionId", key.session_id, 256),
        # Main grants a request base up to 256 chars; the Agent appends
        # ":<model-step>" to scope each assistant response deterministically.
        ("assistantTurnId", key.assistant_turn_id, 272),
        ("providerToolCallId", key.provider_tool_call_id, 512),
    ):
        if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
            raise LedgerError(f"{label} 형식이 올바르지 않습니다.")


class AgentExecutionLedger:
    """SQLite ledger with durable state transitions and no prompt storage."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(
                self.path,
                timeout=5,
                isolation_level=None,
                check_same_thread=False,
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA busy_timeout=5000")
            self._db.execute("PRAGMA synchronous=FULL")
            if self.path != ":memory:":
                mode = str(self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
                if mode != "wal":
                    raise LedgerError("Agent 실행 원장을 WAL 모드로 열 수 없습니다.")
            self._initialize_schema()
        except LedgerError:
            self.close()
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            self.close()
            raise LedgerError("Agent 실행 원장을 안전하게 열 수 없습니다.") from exc

    def close(self) -> None:
        db = getattr(self, "_db", None)
        if db is not None:
            try:
                db.close()
            finally:
                self._db = None

    def __enter__(self) -> "AgentExecutionLedger":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def _initialize_schema(self) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            tables = {
                row[0]
                for row in self._db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not tables:
                self._db.execute(
                    "CREATE TABLE ledger_meta (schema_version INTEGER NOT NULL)"
                )
                self._db.execute(
                    "INSERT INTO ledger_meta(schema_version) VALUES (?)",
                    (LEDGER_SCHEMA_VERSION,),
                )
                self._db.execute(
                    """
                    CREATE TABLE tool_execution_ledger (
                      session_id TEXT NOT NULL,
                      assistant_turn_id TEXT NOT NULL,
                      provider_tool_call_id TEXT NOT NULL,
                      tool_name TEXT NOT NULL,
                      arguments_hash TEXT NOT NULL,
                      status TEXT NOT NULL CHECK(status IN (
                        'pending','awaiting_approval','running','completed','failed','rejected','indeterminate'
                      )),
                      result TEXT NOT NULL DEFAULT '',
                      ok INTEGER NOT NULL DEFAULT 0 CHECK(ok IN (0,1)),
                      rejected INTEGER NOT NULL DEFAULT 0 CHECK(rejected IN (0,1)),
                      approval_id TEXT NOT NULL,
                      execution_id TEXT NOT NULL,
                      updated_at INTEGER NOT NULL,
                      PRIMARY KEY(session_id, assistant_turn_id, provider_tool_call_id)
                    ) WITHOUT ROWID
                    """
                )
            else:
                if tables != {"ledger_meta", "tool_execution_ledger"}:
                    raise LedgerError("Agent 실행 원장 스키마를 신뢰할 수 없습니다.")
                rows = self._db.execute("SELECT schema_version FROM ledger_meta").fetchall()
                if len(rows) != 1 or rows[0][0] != LEDGER_SCHEMA_VERSION:
                    raise LedgerError("Agent 실행 원장 스키마 버전이 호환되지 않습니다.")
                columns = {
                    row[1]
                    for row in self._db.execute("PRAGMA table_info(tool_execution_ledger)").fetchall()
                }
                expected = {
                    "session_id", "assistant_turn_id", "provider_tool_call_id", "tool_name",
                    "arguments_hash",
                    "status", "result", "ok", "rejected", "approval_id", "execution_id", "updated_at",
                }
                if columns != expected:
                    raise LedgerError("Agent 실행 원장 열 구성이 올바르지 않습니다.")
            self._db.execute("COMMIT")
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    def journal_mode(self) -> str:
        return str(self._db.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def recover_incomplete(self) -> int:
        """Mark pre-crash nonterminal records indeterminate; never rerun them."""
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._db.execute(
                """
                UPDATE tool_execution_ledger
                   SET status='indeterminate', updated_at=?
                 WHERE status IN ('pending','awaiting_approval','running')
                """,
                (int(time.time() * 1000),),
            )
            self._db.execute("COMMIT")
            return cursor.rowcount
        except Exception:
            self._db.execute("ROLLBACK")
            raise

    @staticmethod
    def _record(row: sqlite3.Row) -> LedgerRecord:
        return LedgerRecord(
            key=LedgerKey(row["session_id"], row["assistant_turn_id"], row["provider_tool_call_id"]),
            tool_name=row["tool_name"],
            arguments_hash=row["arguments_hash"],
            status=row["status"],
            result=row["result"],
            ok=bool(row["ok"]),
            rejected=bool(row["rejected"]),
            approval_id=row["approval_id"],
            execution_id=row["execution_id"],
        )

    def get(self, key: LedgerKey) -> LedgerRecord | None:
        _validate_key(key)
        row = self._db.execute(
            """
            SELECT * FROM tool_execution_ledger
             WHERE session_id=? AND assistant_turn_id=? AND provider_tool_call_id=?
            """,
            (key.session_id, key.assistant_turn_id, key.provider_tool_call_id),
        ).fetchone()
        return self._record(row) if row is not None else None

    def reserve(
        self,
        key: LedgerKey,
        canonical_arguments: str,
        *,
        tool_name: str,
        approval_id: str,
        execution_id: str,
    ) -> LedgerRecord:
        _validate_key(key)
        if not isinstance(tool_name, str) or not tool_name or len(tool_name) > 256:
            raise LedgerError("도구 함수명 형식이 올바르지 않습니다.")
        if not approval_id or not execution_id or approval_id == execution_id:
            raise LedgerError("승인 ID와 실행 ID는 서로 다른 값이어야 합니다.")
        arguments_hash = canonical_arguments_hash(canonical_arguments)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                """
                SELECT * FROM tool_execution_ledger
                 WHERE session_id=? AND assistant_turn_id=? AND provider_tool_call_id=?
                """,
                (key.session_id, key.assistant_turn_id, key.provider_tool_call_id),
            ).fetchone()
            if row is None:
                self._db.execute(
                    """
                    INSERT INTO tool_execution_ledger(
                      session_id, assistant_turn_id, provider_tool_call_id, tool_name,
                      arguments_hash, status,
                      result, ok, rejected, approval_id, execution_id, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', '', 0, 0, ?, ?, ?)
                    """,
                    (
                        key.session_id, key.assistant_turn_id, key.provider_tool_call_id,
                        tool_name, arguments_hash, approval_id, execution_id,
                        int(time.time() * 1000),
                    ),
                )
                row = self._db.execute(
                    """
                    SELECT * FROM tool_execution_ledger
                     WHERE session_id=? AND assistant_turn_id=? AND provider_tool_call_id=?
                    """,
                    (key.session_id, key.assistant_turn_id, key.provider_tool_call_id),
                ).fetchone()
                self._db.execute("COMMIT")
                return self._record(row)

            record = self._record(row)
            if record.tool_name != tool_name or record.arguments_hash != arguments_hash:
                raise LedgerProtocolConflict(
                    "같은 provider 도구 호출 ID가 서로 다른 함수명 또는 인자로 재사용되었습니다."
                )
            if record.status == "indeterminate" or record.status == "running":
                raise LedgerIndeterminate(
                    "이 도구 호출은 이전 실행 결과를 확정할 수 없어 자동 재실행하지 않습니다."
                )
            if not record.reusable:
                raise LedgerInProgress("같은 도구 호출이 이미 진행 중입니다.")
            self._db.execute("COMMIT")
            return record
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _transition(self, key: LedgerKey, expected: tuple[str, ...], status: LedgerStatus) -> None:
        placeholders = ",".join("?" for _ in expected)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._db.execute(
                f"""
                UPDATE tool_execution_ledger SET status=?, updated_at=?
                 WHERE session_id=? AND assistant_turn_id=? AND provider_tool_call_id=?
                   AND status IN ({placeholders})
                """,
                (
                    status, int(time.time() * 1000), key.session_id, key.assistant_turn_id,
                    key.provider_tool_call_id, *expected,
                ),
            )
            if cursor.rowcount != 1:
                raise LedgerError("Agent 실행 원장 상태 전이가 거부되었습니다.")
            self._db.execute("COMMIT")
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def mark_awaiting_approval(self, key: LedgerKey) -> None:
        self._transition(key, ("pending",), "awaiting_approval")

    def mark_running(self, key: LedgerKey) -> None:
        self._transition(key, ("pending", "awaiting_approval"), "running")

    def finish(
        self,
        key: LedgerKey,
        *,
        status: TerminalStatus,
        result: str,
        ok: bool,
        rejected: bool = False,
    ) -> LedgerRecord:
        if status == "rejected" and not rejected:
            raise LedgerError("거절 상태에는 rejected 표시가 필요합니다.")
        bounded = bound_ledger_result(result)
        allowed = ("awaiting_approval",) if status == "rejected" else ("running",)
        placeholders = ",".join("?" for _ in allowed)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._db.execute(
                f"""
                UPDATE tool_execution_ledger
                   SET status=?, result=?, ok=?, rejected=?, updated_at=?
                 WHERE session_id=? AND assistant_turn_id=? AND provider_tool_call_id=?
                   AND status IN ({placeholders})
                """,
                (
                    status, bounded, int(ok), int(rejected), int(time.time() * 1000),
                    key.session_id, key.assistant_turn_id, key.provider_tool_call_id, *allowed,
                ),
            )
            if cursor.rowcount != 1:
                raise LedgerError("Agent 실행 결과를 원장에 확정할 수 없습니다.")
            row = self._db.execute(
                """
                SELECT * FROM tool_execution_ledger
                 WHERE session_id=? AND assistant_turn_id=? AND provider_tool_call_id=?
                """,
                (key.session_id, key.assistant_turn_id, key.provider_tool_call_id),
            ).fetchone()
            self._db.execute("COMMIT")
            return self._record(row)
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
