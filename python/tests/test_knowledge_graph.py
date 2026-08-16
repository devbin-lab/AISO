from __future__ import annotations

import asyncio

import agent
import agent_routing as routing
from conftest import FakeChat
from knowledge_graph import (
    add_manual_relation,
    create_manual_topic,
    graph_snapshot,
    list_change_history,
    record_agent_tool_activity,
    temporary_knowledge_database,
)


def test_verified_mutations_create_graph_history_but_reads_failures_and_noops_do_not(tmp_path) -> None:
    with temporary_knowledge_database(tmp_path / "knowledge.sqlite3"):
        record_agent_tool_activity(
            session_id="session-1",
            assistant_turn_id="turn-1",
            conversation_id="conversation-1",
            workspace=str(tmp_path),
            tool_name="write_file",
            arguments={"path": "docs/plan.md", "content": "must not be stored"},
            result="Created docs/plan.md",
            ok=True,
        )
        record_agent_tool_activity(
            session_id="session-1",
            assistant_turn_id="turn-1",
            conversation_id="conversation-1",
            workspace=str(tmp_path),
            tool_name="read_file",
            arguments={"path": "docs/plan.md"},
            result="secret document contents",
            ok=True,
        )
        record_agent_tool_activity(
            session_id="session-1",
            assistant_turn_id="turn-1",
            conversation_id="conversation-1",
            workspace=str(tmp_path),
            tool_name="edit_file",
            arguments={"path": "docs/plan.md"},
            result="[NO_CHANGE] already matched",
            ok=True,
        )
        record_agent_tool_activity(
            session_id="session-1",
            assistant_turn_id="turn-1",
            conversation_id="conversation-1",
            workspace=str(tmp_path),
            tool_name="delete_file",
            arguments={"path": "docs/plan.md"},
            result="failed",
            ok=False,
        )

        snapshot = graph_snapshot()
        assert len(snapshot["changes"]) == 1
        assert snapshot["changes"][0]["toolName"] == "write_file"
        assert snapshot["changes"][0]["details"] == {"path": "docs/plan.md"}
        assert {node["kind"] for node in snapshot["nodes"]} == {"agent_session", "conversation", "document"}
        assert len(snapshot["edges"]) == 2
        assert "secret document contents" not in str(snapshot)


def test_manual_topics_and_relations_are_independent_from_agent_history(tmp_path) -> None:
    with temporary_knowledge_database(tmp_path / "knowledge.sqlite3"):
        left = create_manual_topic("Character lore")
        right = create_manual_topic("Launch plan")
        edge = add_manual_relation(left["id"], right["id"], "references")

        snapshot = graph_snapshot()
        assert {node["id"] for node in snapshot["nodes"]} == {left["id"], right["id"]}
        assert snapshot["edges"] == [
            {
                "id": edge["id"],
                "sourceId": left["id"],
                "targetId": right["id"],
                "relation": "references",
                "metadata": {"origin": "user"},
                "updatedAt": snapshot["edges"][0]["updatedAt"],
            }
        ]
        assert snapshot["changes"] == []


def test_change_history_is_not_available_to_the_agent_after_mydb_decoupling() -> None:
    for request in (
        "Aiso\uac00 \ucd5c\uadfc\uc5d0 \ubcc0\uacbd\ud55c \ub0b4\uc6a9 \ubcf4\uace0\ud574\uc918",
        "Show the recent Aiso change history.",
    ):
        decision = routing.classify_request(
            request,
            ("list_calendar_events",),
            no_workspace=True,
        )
        assert decision.name != "change_history"
        assert all("list_change_history" not in phase.tool_names for phase in decision.phases)


def test_change_history_tool_returns_only_verified_entries(tmp_path) -> None:
    with temporary_knowledge_database(tmp_path / "knowledge.sqlite3"):
        record_agent_tool_activity(
            session_id="s", assistant_turn_id="t", tool_name="create_calendar_event",
            arguments={"instruction": "private instruction"}, result="Created event", ok=True,
        )
        payload = asyncio.run(list_change_history())
        assert "Verified Aiso change history (1 entry" in payload
        assert "private instruction" not in payload


def test_agent_execution_does_not_write_into_the_legacy_knowledge_history(env, monkeypatch, tmp_path) -> None:
    async def fake_execute(spec, root, host, args):
        assert spec.name == "write_file"
        assert args == {"path": "notes/status.md", "content": "not persisted in history"}
        return "Created notes/status.md", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    chat = FakeChat([
        {"calls": [("write_file", {"path": "notes/status.md", "content": "not persisted in history"})]},
        {"content": "The status note was created."},
    ])

    with temporary_knowledge_database(tmp_path / "knowledge.sqlite3"):
        events = env.run(
            chat,
            messages=[{"role": "user", "content": "Create notes/status.md."}],
            user_request_text="Create notes/status.md.",
            conversation_id="conversation-verified",
            approval_mode="auto",
            rag_enabled=False,
            enabled_tools=["write_file"],
        )
        snapshot = graph_snapshot()

    assert any(event.get("type") == "tool_result" and event.get("ok") is True for event in events)
    assert snapshot["changes"] == []
    assert "not persisted in history" not in str(snapshot)
