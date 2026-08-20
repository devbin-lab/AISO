# -*- coding: utf-8 -*-
"""종료 경로별 RAG 재색인 계약.

agent_runner에는 `{"type": "done"}` yield가 50곳이 넘지만 루프 안에서
`_maybe_reindex`를 부르는 곳은 15곳뿐이다. 예전에는 run_agent의 finally 백스톱이
`if not completed_normally:` 안에 갇혀 있어서, done을 내고 정상 종료한 경로 중
루프에서 재색인을 부르지 않은 것들은 색인이 조용히 낡은 채로 끝났다.

여기서 고정하는 계약은 두 가지다.
  - 워크스페이스가 변경됐고 색인이 있으면, **어떤 종료 경로든** 재색인이 발화한다.
  - 그 발화는 한 런에 **정확히 한 번**이다(루프 호출 + 백스톱 중복 금지).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import agent
from agent_ledger import AgentExecutionLedger, LedgerError
from conftest import FakeChat, types

WRITE_MD = {"calls": [("write_file", {"path": "a.md", "content": "hi"})]}


def _with_index(env) -> None:
    """색인이 존재하는 워크스페이스로 만든다(재색인 조건 충족)."""
    env.mp.setattr(
        agent, "rag_status", lambda root: {"indexed": True, "embed_model": "e", "count": 1}
    )

    async def fake_search(root, host, q, k):
        return []

    env.mp.setattr(agent, "rag_search", fake_search)
    env.mp.setattr(agent, "format_context", lambda results: "")


def test_reindex_fires_exactly_once_on_normal_completion(env):
    """정상 종료: 루프 호출과 finally 백스톱이 겹쳐도 재색인은 한 번뿐이다."""
    _with_index(env)
    events = env.run(
        FakeChat([WRITE_MD, {"content": "작성 완료."}]),
        approval_mode="auto",
    )
    assert types(events)[-1] == "done"
    assert len(env.reindex_calls) == 1, (
        f"재색인이 {len(env.reindex_calls)}회 발화했다. 루프 호출과 백스톱이 중복된 것."
    )


def test_no_reindex_when_workspace_is_unchanged(env):
    """읽기만 한 런은 색인이 있어도 재색인하지 않는다(음성 대조군)."""
    _with_index(env)
    events = env.run(
        FakeChat([
            {"calls": [("list_dir", {"path": "."})]},
            {"content": "확인했습니다."},
        ]),
        approval_mode="auto",
    )
    assert types(events)[-1] == "done"
    assert env.reindex_calls == []


def test_reindex_runs_when_ledger_finalization_fails_after_mutation(env):
    """원장 확정 실패로 중단해도 재색인은 발화한다.

    이 경로(agent_runner의 `except LedgerError:` → error → done → return)는 툴이
    **이미 파일을 바꾼 뒤** 종료하는데, 루프 안에서 `_maybe_reindex`를 부르지 않는다.
    수정 전에는 여기서 색인이 낡은 채로 남았다.
    """
    _with_index(env)
    ledger_dir = Path(tempfile.mkdtemp())
    ledger = AgentExecutionLedger(ledger_dir / "ledger.sqlite3")

    def _explode(*_args, **_kwargs):
        raise LedgerError("확정 실패(주입)")

    env.mp.setattr(ledger, "finish", _explode)
    try:
        events = env.run(
            FakeChat([WRITE_MD, {"content": "작성 완료."}]),
            approval_mode="auto",
            execution_ledger=ledger,
        )
    finally:
        ledger.close()

    assert types(events)[-1] == "done"
    assert "error" in types(events)
    # 실제로 파일이 쓰였는지 — 그래야 '변경됐는데 재색인 안 함'을 검증하는 의미가 있다.
    assert (env.ws / "a.md").exists()
    assert len(env.reindex_calls) == 1, (
        "원장 확정 실패로 중단한 런에서 재색인이 발화하지 않았다. "
        "run_agent의 finally 백스톱이 정상 종료 경로를 덮지 못하는 상태."
    )


def test_backstop_covers_uncalled_exit_paths(env):
    """구조 계약: 백스톱은 completed_normally와 무관하게 항상 통과해야 한다.

    루프 쪽 호출을 전부 무력화해도 재색인이 발화하면, 보장의 출처가 개별 호출지가
    아니라 finally라는 뜻이다. (예전 코드는 여기서 0회가 나왔다.)
    """
    _with_index(env)
    fired: list[str] = []
    real = agent._maybe_reindex

    def only_backstop(root, host, dirty, rag_available, state=None):
        # state를 넘기는 호출 = 루프 안(수정 후 15곳) / 백스톱도 state를 넘기므로
        # 구분을 위해 호출 순서를 기록하고, 마지막 호출만 실제로 태운다.
        fired.append("call")
        return real(root, host, dirty, rag_available, state)

    env.mp.setattr(agent, "_maybe_reindex", only_backstop)
    events = env.run(FakeChat([WRITE_MD, {"content": "완료."}]), approval_mode="auto")

    assert types(events)[-1] == "done"
    # 루프 호출 + 백스톱 = 최소 2회 시도되지만, 실제 발화는 1회여야 한다.
    assert len(fired) >= 2, "백스톱이 호출되지 않았다 — finally 경로가 끊겼다."
    assert len(env.reindex_calls) == 1


@pytest.mark.parametrize("dirty", [True, False])
def test_maybe_reindex_fires_once_per_run_state(dirty):
    """_maybe_reindex 단위 계약: state를 주면 런당 1회만 발화."""
    calls: list[str] = []
    state: dict = {}
    root = Path(tempfile.mkdtemp())

    original = agent._fire_reindex
    agent._fire_reindex = lambda r, h: calls.append(str(r))
    try:
        for _ in range(5):
            agent._maybe_reindex(root, "h", dirty, True, state)
    finally:
        agent._fire_reindex = original

    assert len(calls) == (1 if dirty else 0)
