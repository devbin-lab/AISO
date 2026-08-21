from __future__ import annotations

import sqlite3

import pytest

from agent_ledger import (
    AgentExecutionLedger,
    LedgerError,
    LedgerInProgress,
    LedgerIndeterminate,
    LedgerKey,
    LedgerProtocolConflict,
    MAX_LEDGER_RESULT_CHARS,
)


KEY = LedgerKey("session-1", "turn-1", "provider-call-1")


def reserve(ledger, canonical='{"value":1}', tool_name="safe_tool"):
    return ledger.reserve(
        KEY,
        canonical,
        tool_name=tool_name,
        approval_id="approval-1",
        execution_id="execution-1",
    )


def test_wal_atomic_transitions_and_terminal_result_reuse(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with AgentExecutionLedger(path) as ledger:
        assert ledger.journal_mode() == "wal"
        pending = reserve(ledger)
        assert pending.status == "pending"
        assert pending.approval_id != pending.execution_id
        ledger.mark_running(KEY)
        complete = ledger.finish(KEY, status="completed", result="safe-result", ok=True)
        assert complete.status == "completed"
        reused = reserve(ledger)
        assert reused.result == "safe-result"
        assert reused.execution_id == "execution-1"

    with AgentExecutionLedger(path) as reopened:
        assert reserve(reopened).result == "safe-result"


def test_same_id_different_arguments_is_protocol_conflict(tmp_path):
    with AgentExecutionLedger(tmp_path / "ledger.sqlite3") as ledger:
        reserve(ledger)
        ledger.mark_running(KEY)
        ledger.finish(KEY, status="completed", result="ok", ok=True)
        with pytest.raises(LedgerProtocolConflict):
            reserve(ledger, '{"value":2}')


def test_same_id_different_tool_name_is_protocol_conflict(tmp_path):
    with AgentExecutionLedger(tmp_path / "ledger.sqlite3") as ledger:
        reserve(ledger, "{}", tool_name="tool_a")
        ledger.mark_running(KEY)
        ledger.finish(KEY, status="completed", result="ok", ok=True)
        with pytest.raises(LedgerProtocolConflict):
            reserve(ledger, "{}", tool_name="tool_b")


def test_concurrent_duplicate_cannot_be_reserved_twice(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with AgentExecutionLedger(path) as first, AgentExecutionLedger(path) as second:
        reserve(first)
        with pytest.raises(LedgerInProgress):
            reserve(second)


@pytest.mark.parametrize("state", ["pending", "awaiting_approval", "running"])
def test_crash_recovery_marks_nonterminal_state_indeterminate(tmp_path, state):
    path = tmp_path / "ledger.sqlite3"
    with AgentExecutionLedger(path) as ledger:
        reserve(ledger)
        if state == "awaiting_approval":
            ledger.mark_awaiting_approval(KEY)
        elif state == "running":
            ledger.mark_running(KEY)
        assert ledger.recover_incomplete() == 1
        assert ledger.get(KEY).status == "indeterminate"
        with pytest.raises(LedgerIndeterminate):
            reserve(ledger)


def test_rejection_is_terminal_and_reused_without_execution(tmp_path):
    with AgentExecutionLedger(tmp_path / "ledger.sqlite3") as ledger:
        reserve(ledger)
        ledger.mark_awaiting_approval(KEY)
        record = ledger.finish(
            KEY, status="rejected", result="rejected", ok=False, rejected=True
        )
        assert record.rejected is True
        assert reserve(ledger).status == "rejected"


def test_result_is_bounded_and_schema_contains_no_prompt_or_raw_arguments(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with AgentExecutionLedger(path) as ledger:
        reserve(ledger)
        ledger.mark_running(KEY)
        record = ledger.finish(
            KEY, status="completed", result="x" * (MAX_LEDGER_RESULT_CHARS + 500), ok=True
        )
        assert len(record.result) == MAX_LEDGER_RESULT_CHARS
    raw = path.read_bytes()
    assert b"prompt" not in raw.lower()
    assert b'\"value\":1' not in raw
    assert b"arguments_hash" in raw


def test_future_unknown_and_corrupt_schema_fail_closed(tmp_path):
    future = tmp_path / "future.sqlite3"
    db = sqlite3.connect(future)
    db.execute("CREATE TABLE ledger_meta(schema_version INTEGER NOT NULL)")
    db.execute("INSERT INTO ledger_meta VALUES (99)")
    db.execute("CREATE TABLE tool_execution_ledger(dummy TEXT)")
    db.commit()
    db.close()
    with pytest.raises(LedgerError):
        AgentExecutionLedger(future)

    unknown = tmp_path / "unknown.sqlite3"
    db = sqlite3.connect(unknown)
    db.execute("CREATE TABLE something_else(value TEXT)")
    db.commit()
    db.close()
    with pytest.raises(LedgerError):
        AgentExecutionLedger(unknown)

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not-a-sqlite-database\x00CANARY-PROMPT")
    with pytest.raises(LedgerError):
        AgentExecutionLedger(corrupt)


# ── v1 → v2 마이그레이션 (status에 'expired' 추가) ──────────────────────────

_V1_CREATE = """
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


def _write_v1_ledger(path, rows):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE ledger_meta(schema_version INTEGER NOT NULL)")
    db.execute("INSERT INTO ledger_meta VALUES (1)")
    db.execute(_V1_CREATE)
    for row in rows:
        db.execute(
            "INSERT INTO tool_execution_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row
        )
    db.commit()
    db.close()


def test_v1_ledger_is_migrated_without_losing_records(tmp_path):
    """기존 사용자의 원장은 보존된다 — 버리면 '정확히 한 번' 보장이 함께 사라진다."""
    path = tmp_path / "v1.sqlite3"
    _write_v1_ledger(path, [
        ("session-1", "turn-1", "provider-call-1", "safe_tool", "hash-1",
         "completed", "이전 결과", 1, 0, "approval-1", "execution-1", 111),
        ("session-1", "turn-1", "provider-call-2", "delete_file", "hash-2",
         "rejected", "거부됨", 0, 1, "approval-2", "execution-2", 222),
    ])

    with AgentExecutionLedger(path) as ledger:
        preserved = ledger.get(LedgerKey("session-1", "turn-1", "provider-call-1"))
        assert preserved is not None
        assert preserved.status == "completed"
        assert preserved.result == "이전 결과"

        denial = ledger.get(LedgerKey("session-1", "turn-1", "provider-call-2"))
        assert denial is not None and denial.rejected is True

        # 마이그레이션 뒤에는 새 상태를 받아들인다.
        fresh = LedgerKey("session-1", "turn-2", "provider-call-3")
        ledger.reserve(fresh, '{"v":1}', tool_name="delete_file",
                       approval_id="a-3", execution_id="e-3")
        ledger.mark_awaiting_approval(fresh)
        record = ledger.finish(fresh, status="expired", result="[응답 없음]", ok=False)
        assert record.status == "expired"
        assert record.rejected is False

    db = sqlite3.connect(path)
    version = db.execute("SELECT schema_version FROM ledger_meta").fetchone()[0]
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    db.close()
    assert version == 2
    assert tables == {"ledger_meta", "tool_execution_ledger"}, "임시 테이블이 남았다"


def test_expired_cannot_be_recorded_as_a_user_rejection(tmp_path):
    """무응답과 거부를 섞으면 원장이 다시 거짓을 말한다."""
    with AgentExecutionLedger(tmp_path / "l.sqlite3") as ledger:
        reserve(ledger)
        ledger.mark_awaiting_approval(KEY)
        with pytest.raises(LedgerError):
            ledger.finish(KEY, status="expired", result="x", ok=False, rejected=True)


def test_expired_record_is_replayed_instead_of_re_executing(tmp_path):
    """만료도 종료 상태다 — 재시도가 파괴적 도구를 조용히 다시 실행하면 안 된다."""
    with AgentExecutionLedger(tmp_path / "l.sqlite3") as ledger:
        reserve(ledger, tool_name="delete_file")
        ledger.mark_awaiting_approval(KEY)
        ledger.finish(KEY, status="expired", result="[응답 없음]", ok=False)

        replayed = reserve(ledger, tool_name="delete_file")
        assert replayed.status == "expired"
        assert replayed.reusable is True
        assert replayed.result == "[응답 없음]"
