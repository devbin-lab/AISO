"""Crash-surviving, fail-closed execution ledger for provider tool calls."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import sqlite3
import time
from typing import Literal


# v2: status에 'expired'(승인 무응답) 추가. v1 DB는 아래 _migrate_v1_to_v2가 올린다.
LEDGER_SCHEMA_VERSION = 2

_STATUS_CHECK = (
    "'pending','awaiting_approval','running',"
    "'completed','failed','rejected','expired','indeterminate'"
)
MAX_LEDGER_RESULT_CHARS = 16 * 1024

# expired = 승인 요청에 응답이 오지 않아 실행하지 않음. rejected(사용자가 거부)와
# 구분한다 — 일어나지 않은 사용자 결정을 원장에 사실로 남기면 안 된다.
TerminalStatus = Literal["completed", "failed", "rejected", "expired"]
LedgerStatus = Literal[
    "pending", "awaiting_approval", "running",
    "completed", "failed", "rejected", "expired", "indeterminate",
]
# 승인 대기 상태에서 곧바로 끝날 수 있는 종료 상태들 (도구는 실행되지 않았다).
_TERMINAL_FROM_AWAITING_APPROVAL = ("rejected", "expired")


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
        # expired도 종료 상태다. 같은 assistant 응답을 재시도해도 도구를 몰래 다시
        # 실행하지 않고 저장된 '응답 없음' 결과를 그대로 돌려준다 — 파괴적 도구에서
        # 재시도가 조용한 재실행이 되면 안 되기 때문이다(정확히 한 번 계약).
        return self.status in ("completed", "failed", "rejected", "expired")


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

    @staticmethod
    def _create_ledger_sql(table: str) -> str:
        return f"""
            CREATE TABLE {table} (
              session_id TEXT NOT NULL,
              assistant_turn_id TEXT NOT NULL,
              provider_tool_call_id TEXT NOT NULL,
              tool_name TEXT NOT NULL,
              arguments_hash TEXT NOT NULL,
              status TEXT NOT NULL CHECK(status IN ({_STATUS_CHECK})),
              result TEXT NOT NULL DEFAULT '',
              ok INTEGER NOT NULL DEFAULT 0 CHECK(ok IN (0,1)),
              rejected INTEGER NOT NULL DEFAULT 0 CHECK(rejected IN (0,1)),
              approval_id TEXT NOT NULL,
              execution_id TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY(session_id, assistant_turn_id, provider_tool_call_id)
            ) WITHOUT ROWID
        """

    def _migrate_v1_to_v2(self) -> None:
        """CHECK 제약에 'expired'를 더한 테이블로 옮긴다. 이미 열린 트랜잭션 안에서 돈다."""
        self._db.execute(self._create_ledger_sql("tool_execution_ledger_v2"))
        self._db.execute(
            """
            INSERT INTO tool_execution_ledger_v2
            SELECT session_id, assistant_turn_id, provider_tool_call_id, tool_name,
                   arguments_hash, status, result, ok, rejected,
                   approval_id, execution_id, updated_at
              FROM tool_execution_ledger
            """
        )
        self._db.execute("DROP TABLE tool_execution_ledger")
        self._db.execute("ALTER TABLE tool_execution_ledger_v2 RENAME TO tool_execution_ledger")
        self._db.execute(
            "UPDATE ledger_meta SET schema_version=?", (LEDGER_SCHEMA_VERSION,)
        )

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
                self._db.execute(self._create_ledger_sql("tool_execution_ledger"))
            else:
                if tables != {"ledger_meta", "tool_execution_ledger"}:
                    raise LedgerError("Agent 실행 원장 스키마를 신뢰할 수 없습니다.")
                rows = self._db.execute("SELECT schema_version FROM ledger_meta").fetchall()
                if len(rows) != 1:
                    raise LedgerError("Agent 실행 원장 스키마 버전이 호환되지 않습니다.")
                version = rows[0][0]
                if version == 1:
                    # v1 → v2: CHECK 제약에 'expired'를 추가한다. SQLite는 CHECK를
                    # 바꿀 수 없으므로 테이블을 다시 만들어 옮긴다. 기존 행은 그대로
                    # 보존된다 — 원장을 버리면 '정확히 한 번' 보장이 함께 사라진다.
                    self._migrate_v1_to_v2()
                    version = LEDGER_SCHEMA_VERSION
                if version != LEDGER_SCHEMA_VERSION:
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
        if status == "expired" and rejected:
            # 응답 없음은 거절이 아니다. 둘을 섞으면 원장이 다시 거짓을 말한다.
            raise LedgerError("만료 상태에는 rejected 표시를 쓸 수 없습니다.")
        bounded = bound_ledger_result(result)
        # allowed = 이 전이가 허용되는 '현재' 상태. rejected/expired는 도구가 실행되지
        # 않았으므로 running을 거치지 않고 awaiting_approval에서 바로 끝난다.
        allowed = (
            ("awaiting_approval",)
            if status in _TERMINAL_FROM_AWAITING_APPROVAL
            else ("running",)
        )
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
