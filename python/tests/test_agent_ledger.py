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
