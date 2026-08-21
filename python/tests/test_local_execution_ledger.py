# -*- coding: utf-8 -*-
"""'정확히 한 번' 실행 보장은 기본 경로(로컬 Ollama)에도 적용된다.

실행 원장(WAL SQLite)은 도구가 두 번 실행되지 않도록 지키는 장치다. 크래시로
결과를 확정하지 못한 호출은 재기동 시 `indeterminate`로 승격되고, 그 뒤로는
자동 재실행을 거부한다(fail-closed).

그런데 원장은 `/agent` 엔드포인트의 `if req.provider == "nvidia":` 블록 안에서만
만들어졌다. 사용자의 99%가 쓰는 **기본 경로인 로컬 Ollama에는 원장이 아예 없었다.**
파괴적 도구(delete_dir·run_command 등)가 취소·재시도·크래시에서 중복 실행돼도
막을 것이 없고, 남는 방어선은 반복 감지 휴리스틱뿐이다.

적대 검토는 이것을 "Ollama 툴콜에 provider ID가 없어서 키로 쓸 데이터가 없다"고
진단하고 요청 스키마 마이그레이션이 필요하다고 봤다. 실제로는 세 요소가 이미 전부
있다 — 화면이 로컬 실행에도 session_id·assistant_turn_id를 UUID로 보내고,
`_normalize_tool_calls`가 `ollama-{turn}-{index}` 형태로 툴콜 ID를 합성한다.
빠진 것은 엔드포인트의 배선 하나뿐이었다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from conftest import FakeChat, types  # noqa: E402

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None

pytestmark = pytest.mark.skipif(TestClient is None, reason="TestClient(httpx) 미설치")

TOKEN = "t" * 32
SESSION = "11111111-2222-3333-4444-555555555555"
TURN = "66666666-7777-8888-9999-000000000000"


@pytest.fixture
def client(monkeypatch):
    auth_middleware = next(
        middleware
        for middleware in main.app.user_middleware
        if middleware.cls is main.TokenAuthMiddleware
    )
    monkeypatch.setattr(main, "AUTH_TOKEN", TOKEN)
    monkeypatch.setitem(auth_middleware.kwargs, "token", TOKEN)
    main.app.middleware_stack = None
    try:
        yield TestClient(main.app)
    finally:
        main.app.middleware_stack = None


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """run_agent가 실제로 받은 execution_ledger를 잡아 둔다."""
    seen: dict = {}

    async def fake_run_agent(**kwargs):
        seen.update(kwargs)
        yield {"type": "done"}

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    monkeypatch.setattr(main, "AGENT_LEDGER_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setattr(main, "_agent_ledger_startup_error", None)
    return seen


def _post(client, workspace, **overrides):
    body = {
        "messages": [{"role": "user", "content": "안녕"}],
        "provider": "ollama",
        "workspace": str(workspace),
        "session_id": SESSION,
        "assistant_turn_id": TURN,
    }
    body.update(overrides)
    return client.post("/agent", headers={"X-Aiso-Token": TOKEN}, json=body)


def test_local_run_receives_an_execution_ledger(client, captured, tmp_path):
    """기본 경로에도 '정확히 한 번' 보장이 붙는다."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    response = _post(client, workspace)
    assert response.status_code == 200, response.text

    ledger = captured.get("execution_ledger")
    assert ledger is not None, (
        "로컬 실행에 원장이 붙지 않았다 — 파괴적 도구가 재시도에서 중복 실행될 수 있다."
    )


def test_local_run_passes_the_ids_the_ledger_key_needs(client, captured, tmp_path):
    """세 요소가 모두 유효해야 LedgerKey를 만들 수 있다."""
    from agent_ledger import LedgerKey, _validate_key

    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert _post(client, workspace).status_code == 200

    # 화면이 보내는 값이 그대로 러너로 간다.
    assert captured["session_id"] == SESSION
    assert captured["assistant_turn_id"] == TURN
    # 툴콜 ID는 하네스가 합성한다 — 이 형태로 키가 성립하는지 확인한다.
    _validate_key(LedgerKey(SESSION, f"{TURN}:0", f"ollama-{TURN}:0-0"))


def test_missing_ids_do_not_get_a_ledger_instead_of_crashing(client, captured, tmp_path):
    """구버전 화면처럼 ID 없이 오는 요청은 원장 없이 계속 동작해야 한다.

    원장을 붙일 수 없다고 기본 경로 실행을 막으면, 오늘 잘 돌던 앱이 멈춘다.
    보장을 얻지 못할 뿐 가용성은 유지한다.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    response = _post(client, workspace, session_id="", assistant_turn_id="")
    assert response.status_code == 200, response.text
    assert captured.get("execution_ledger") is None


def test_unavailable_ledger_does_not_break_local_runs(client, captured, tmp_path):
    """원장을 열 수 없어도 로컬 실행은 계속된다(NVIDIA는 503으로 실패하는 것과 다르다).

    NVIDIA는 선택 기능이라 fail-closed가 맞지만, 로컬은 제품의 기본 경로다.
    원장 파일 문제로 앱 전체가 멈추는 편이 더 나쁘다.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # 디렉터리를 경로로 줘서 열기 실패를 유도한다.
    broken = tmp_path / "not-a-db"
    broken.mkdir()
    captured.clear()
    import main as m
    m.AGENT_LEDGER_PATH = str(broken)

    response = _post(client, workspace)
    assert response.status_code == 200, response.text
    assert captured.get("execution_ledger") is None


# ── 실제 중복 실행 차단 (통합) ─────────────────────────────────────────

def test_local_destructive_tool_is_not_executed_twice_on_retry(env, tmp_path):
    """같은 assistant 응답을 재시도해도 파괴적 도구가 다시 실행되지 않는다.

    엔드포인트 배선만 확인하면 "원장 객체가 전달됐다"까지밖에 모른다.
    실제로 재실행이 막히는지가 계약이다.
    """
    import agent
    from agent_ledger import AgentExecutionLedger

    executions: list[str] = []

    async def counting_execute(spec, root, host, args):
        executions.append(spec.name)
        return "삭제했습니다.", None

    env.mp.setattr(agent, "execute", counting_execute)

    ledger = AgentExecutionLedger(tmp_path / "ledger.sqlite3")
    try:
        script = [
            {"calls": [("delete_file", {"path": "victim.md"})]},
            {"content": "완료."},
        ]
        first = env.run(
            FakeChat(list(script)),
            approval_mode="auto",
            execution_ledger=ledger,
            assistant_turn_id="turn-fixed-0001",
        )
        assert types(first)[-1] == "done"
        assert executions == ["delete_file"], f"첫 실행이 되지 않았다: {executions}"

        # 같은 세션·턴으로 재시도 — 원장이 저장된 결과를 재생해야 한다.
        second = env.run(
            FakeChat(list(script)),
            approval_mode="auto",
            execution_ledger=ledger,
            assistant_turn_id="turn-fixed-0001",
        )
        assert types(second)[-1] == "done"
        assert executions == ["delete_file"], (
            f"재시도가 도구를 다시 실행했다: {executions} — '정확히 한 번'이 깨졌다"
        )
        assert any(event.get("reused") is True for event in second), (
            "재생 표식(reused)이 없다 — 원장을 거치지 않았다는 뜻"
        )
    finally:
        ledger.close()


def test_local_crash_recovery_refuses_automatic_reexecution(env, tmp_path):
    """크래시로 결과를 확정하지 못한 호출은 재기동 뒤 자동 재실행하지 않는다."""
    import agent
    from agent_ledger import AgentExecutionLedger

    executions: list[str] = []

    async def counting_execute(spec, root, host, args):
        executions.append(spec.name)
        return "삭제했습니다.", None

    env.mp.setattr(agent, "execute", counting_execute)

    path = tmp_path / "ledger.sqlite3"
    ledger = AgentExecutionLedger(path)
    try:
        env.run(
            FakeChat([{"calls": [("delete_file", {"path": "v.md"})]}, {"content": "완료."}]),
            approval_mode="auto",
            execution_ledger=ledger,
            assistant_turn_id="turn-crash-0001",
        )
        assert executions == ["delete_file"]
        # 크래시 흉내: 확정된 행을 실행 중 상태로 되돌린 뒤 재기동 복구를 돌린다.
        ledger._db.execute("UPDATE tool_execution_ledger SET status='running'")
        ledger._db.commit()
    finally:
        ledger.close()

    reopened = AgentExecutionLedger(path)
    try:
        assert reopened.recover_incomplete() == 1
        events = env.run(
            FakeChat([{"calls": [("delete_file", {"path": "v.md"})]}, {"content": "완료."}]),
            approval_mode="auto",
            execution_ledger=reopened,
            assistant_turn_id="turn-crash-0001",
        )
        assert executions == ["delete_file"], (
            f"불확정 상태인데 자동 재실행했다: {executions}"
        )
        assert any(event.get("type") == "error" for event in events)
        assert types(events)[-1] == "done"
    finally:
        reopened.close()
