from __future__ import annotations

import asyncio
import ast
from collections.abc import AsyncIterator
import inspect
import textwrap

import pytest

import agent
from agent_ledger import AgentExecutionLedger, LedgerKey
from llm import LlmEvent, LlmModelRuntime, LlmRequest
from llm.providers.nvidia import NvidiaAdapter


class FakeNvidiaRuntime:
    def __init__(self, turns: list[list[LlmEvent]]):
        self.turns = list(turns)
        self.requests: list[LlmRequest] = []

    async def prepare_model(self, model: str) -> LlmModelRuntime:
        return LlmModelRuntime(model=model)

    def prepare_attempts(self, request, _reasoning_effort, _model_runtime):
        return [request]

    async def chat_stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        self.requests.append(request)
        for event in self.turns.pop(0):
            yield event


def delta(index: int, *, call_id: str = "", name: str = "", arguments: str = "") -> LlmEvent:
    call: dict = {"index": index, "function": {}}
    if call_id:
        call["id"] = call_id
    if name:
        call["function"]["name"] = name
    if arguments:
        call["function"]["arguments"] = arguments
    return LlmEvent(kind="tool_call_delta", tool_calls=[call])


def tool_turn(call_id: str, name: str = "get_system_time", arguments: str = "{}"):
    return [
        delta(0, call_id=call_id, name=name, arguments=arguments),
        LlmEvent(kind="done", done_reason="tool_calls"),
    ]


def final_turn(text: str = "done"):
    return [LlmEvent(kind="content", text=text), LlmEvent(kind="done", done_reason="stop")]


async def collect(
    runtime,
    ledger,
    *,
    base="assistant-request-0001",
    workspace="",
    approval_mode="read",
):
    return [
        event
        async for event in agent.run_agent(
            host="unused",
            workspace=workspace,
            model="nvidia/test-model",
            messages=[{"role": "user", "content": "safe request"}],
            session_id="session-gate5-0001",
            approval_mode=approval_mode,
            provider="nvidia",
            runtime=runtime,
            assistant_turn_id=base,
            execution_ledger=ledger,
            rag_enabled=True,
            comfy_base_url="http://127.0.0.1:8188",
            comfy_profiles=[{"id": "private-model"}],
        )
    ]


def test_split_safe_call_executes_once_and_preserves_three_distinct_ids(tmp_path, monkeypatch):
    executed: list[str] = []

    async def fake_execute(spec, root, host, args):
        executed.append(spec.name)
        return "safe-result", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    runtime = FakeNvidiaRuntime([
        [
            delta(0, call_id="provider-", name="get_", arguments="{"),
            delta(0, call_id="call-01", name="system_time", arguments="}"),
            LlmEvent(kind="done", done_reason="tool_calls"),
        ],
        final_turn(),
    ])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with AgentExecutionLedger(tmp_path / "ledger.sqlite3") as ledger:
        events = asyncio.run(collect(runtime, ledger, workspace=str(workspace)))

    call = next(event for event in events if event["type"] == "tool_call")
    result = next(event for event in events if event["type"] == "tool_result")
    assert executed == ["get_system_time"]
    assert call["providerToolCallId"] == "provider-call-01"
    assert len({call["providerToolCallId"], call["approvalId"], call["executionId"]}) == 3
    assert result["executionId"] == call["executionId"]
    second_messages = list(runtime.requests[1].messages)
    assert second_messages[-2]["tool_calls"][0]["id"] == "provider-call-01"
    assert second_messages[-1] == {
        "role": "tool", "tool_call_id": "provider-call-01", "content": "safe-result"
    }
    wire_messages = NvidiaAdapter.serialize_request(runtime.requests[1])["messages"]
    assert wire_messages[-2]["tool_calls"][0]["id"] == "provider-call-01"
    assert wire_messages[-1]["tool_call_id"] == "provider-call-01"


def test_multiple_calls_execute_sequentially_and_later_turn_may_reuse_provider_id(tmp_path, monkeypatch):
    order: list[str] = []

    async def fake_execute(spec, root, host, args):
        order.append(str(args.get("label", "none")))
        await asyncio.sleep(0)
        return order[-1], None

    monkeypatch.setattr(agent, "execute", fake_execute)
    runtime = FakeNvidiaRuntime([
        [
            delta(1, call_id="provider-b", name="get_system_time", arguments='{"label":"b"}'),
            delta(0, call_id="provider-a", name="get_system_time", arguments='{"label":"a"}'),
            LlmEvent(kind="done", done_reason="tool_calls"),
        ],
        tool_turn("provider-a", arguments='{"label":"later"}'),
        final_turn(),
    ])
    with AgentExecutionLedger(tmp_path / "ledger.sqlite3") as ledger:
        events = asyncio.run(collect(runtime, ledger))

    assert order == ["a", "b", "later"]
    scopes = [event["assistantTurnId"] for event in events if event["type"] == "tool_call"]
    assert scopes == ["assistant-request-0001:0", "assistant-request-0001:0", "assistant-request-0001:1"]


@pytest.mark.parametrize(
    "bad_turn",
    [
        [delta(0, call_id="provider-bad", name="get_system_time", arguments="{}")],
        [delta(0, call_id="provider-bad", name="get_system_time", arguments="{"),
         LlmEvent(kind="done", done_reason="tool_calls")],
        [delta(0, call_id="provider-bad", name="get_system_time", arguments="{}"),
         LlmEvent(kind="cancelled", error="cancelled")],
        [delta(0, call_id="provider-bad", name="get_system_time", arguments="{}"),
         LlmEvent(kind="error", error="upstream error")],
        [delta(0, call_id="provider-bad", name="get_system_time", arguments="{}"),
         LlmEvent(kind="done", done_reason="length")],
    ],
)
def test_malformed_truncated_cancelled_or_wrong_finish_executes_nothing(
    tmp_path, monkeypatch, bad_turn
):
    executed = 0

    async def fake_execute(*_args):
        nonlocal executed
        executed += 1
        return "unexpected", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    runtime = FakeNvidiaRuntime([bad_turn])
    with AgentExecutionLedger(tmp_path / "ledger.sqlite3") as ledger:
        events = asyncio.run(collect(runtime, ledger))
    assert executed == 0
    assert any(event["type"] == "error" for event in events)
    assert not any(event["type"] == "tool_call" for event in events)


def test_same_request_retry_reuses_terminal_result_and_argument_change_conflicts(tmp_path, monkeypatch):
    executions = 0

    async def fake_execute(*_args):
        nonlocal executions
        executions += 1
        return "durable-result", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    path = tmp_path / "ledger.sqlite3"
    with AgentExecutionLedger(path) as ledger:
        first = asyncio.run(collect(FakeNvidiaRuntime([tool_turn("provider-once"), final_turn()]), ledger))
        second = asyncio.run(collect(FakeNvidiaRuntime([tool_turn("provider-once"), final_turn()]), ledger))
        conflict = asyncio.run(collect(
            FakeNvidiaRuntime([tool_turn("provider-once", arguments='{"changed":true}')]), ledger
        ))

    assert executions == 1
    assert any(event.get("reused") is True for event in second)
    assert any(event["type"] == "error" for event in conflict)
    assert not any(event["type"] == "tool_result" for event in conflict)
    assert any(event["type"] == "tool_result" for event in first)


def test_same_id_and_arguments_with_a_different_tool_name_conflicts(tmp_path, monkeypatch):
    executions = 0

    async def fake_execute(*_args):
        nonlocal executions
        executions += 1
        return "first-result", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    with AgentExecutionLedger(tmp_path / "ledger.sqlite3") as ledger:
        asyncio.run(collect(FakeNvidiaRuntime([tool_turn("provider-name"), final_turn()]), ledger))
        conflict = asyncio.run(collect(
            FakeNvidiaRuntime([tool_turn("provider-name", name="update_plan")]), ledger
        ))
    assert executions == 1
    assert any(event["type"] == "error" for event in conflict)
    assert not any(event["type"] in {"tool_call", "tool_result", "plan"} for event in conflict)


def test_recovered_running_call_is_indeterminate_and_never_reexecuted(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    key = LedgerKey("session-gate5-0001", "assistant-request-0001:0", "provider-crash")
    with AgentExecutionLedger(path) as ledger:
        ledger.reserve(
            key,
            "{}",
            tool_name="get_system_time",
            approval_id="approval-crash",
            execution_id="execution-crash",
        )
        ledger.mark_running(key)
    executions = 0

    async def fake_execute(*_args):
        nonlocal executions
        executions += 1
        return "unexpected", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    with AgentExecutionLedger(path) as ledger:
        assert ledger.recover_incomplete() == 1
        events = asyncio.run(collect(FakeNvidiaRuntime([tool_turn("provider-crash")]), ledger))
    assert executions == 0
    assert any("자동" in event.get("error", "") or "상태" in event.get("error", "")
               for event in events if event["type"] == "error")


def test_consumer_cancellation_after_tool_call_never_executes_and_recovers_indeterminate(
    tmp_path, monkeypatch
):
    executions = 0

    async def fake_execute(*_args):
        nonlocal executions
        executions += 1
        return "unexpected", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    path = tmp_path / "ledger.sqlite3"
    key = LedgerKey("session-gate5-0001", "assistant-request-0001:0", "provider-cancel")

    async def cancel_after_call(ledger):
        stream = agent.run_agent(
            host="unused",
            workspace="",
            model="nvidia/test-model",
            messages=[{"role": "user", "content": "safe request"}],
            approval_mode="manual",
            session_id="session-gate5-0001",
            provider="nvidia",
            runtime=FakeNvidiaRuntime([tool_turn("provider-cancel")]),
            assistant_turn_id="assistant-request-0001",
            execution_ledger=ledger,
        )
        while True:
            event = await anext(stream)
            if event["type"] == "tool_call":
                break
        await stream.aclose()

    with AgentExecutionLedger(path) as ledger:
        asyncio.run(cancel_after_call(ledger))
        assert ledger.get(key).status == "pending"
    with AgentExecutionLedger(path) as ledger:
        assert ledger.recover_incomplete() == 1
        retry = asyncio.run(collect(
            FakeNvidiaRuntime([tool_turn("provider-cancel")]), ledger
        ))
    assert executions == 0
    assert any(event["type"] == "error" for event in retry)
    assert not any(event["type"] in {"tool_call", "tool_result"} for event in retry)


def test_approval_denial_is_ledgered_without_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "APPROVAL_TIMEOUT", 0.001)
    executions = 0

    async def fake_execute(*_args):
        nonlocal executions
        executions += 1
        return "unexpected", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    runtime = FakeNvidiaRuntime([
        tool_turn("provider-denied"),
        final_turn(),
    ])
    with AgentExecutionLedger(tmp_path / "ledger.sqlite3") as ledger:
        events = asyncio.run(collect(runtime, ledger, approval_mode="manual"))
        retry = asyncio.run(collect(
            FakeNvidiaRuntime([tool_turn("provider-denied"), final_turn()]),
            ledger,
            approval_mode="manual",
        ))
    request = next(event for event in events if event["type"] == "approval_request")
    result = next(event for event in events if event["type"] == "tool_result")
    assert executions == 0
    assert request["approvalId"] != request["executionId"]
    assert result["rejected"] is True
    assert not any(event["type"] == "approval_request" for event in retry)
    assert any(event.get("reused") is True and event.get("rejected") is True for event in retry)


def test_nvidia_model_payload_never_contains_workspace_rag_or_file_metadata(tmp_path):
    secret_path = tmp_path / "private-workspace-canary"
    secret_path.mkdir()
    runtime = FakeNvidiaRuntime([final_turn()])
    with AgentExecutionLedger(tmp_path / "ledger.sqlite3") as ledger:
        asyncio.run(collect(runtime, ledger, workspace=str(secret_path)))
    request = runtime.requests[0]
    serialized = repr({"messages": list(request.messages), "tools": list(request.tools or [])})
    assert str(secret_path) not in serialized
    assert "private-model" not in serialized
    tool_names = {tool["function"]["name"] for tool in request.tools or []}
    assert tool_names == {"update_plan", "get_system_time"}


def test_update_plan_arguments_are_hashed_not_persisted_in_the_ledger(tmp_path):
    canary = "CANARY-PLAN-ARGUMENT-MUST-NOT-PERSIST-91827"
    arguments = '{"steps":[{"content":"' + canary + '","status":"pending"}]}'
    path = tmp_path / "ledger.sqlite3"
    with AgentExecutionLedger(path) as ledger:
        events = asyncio.run(collect(
            FakeNvidiaRuntime([
                tool_turn("provider-plan", name="update_plan", arguments=arguments),
                final_turn(),
            ]),
            ledger,
        ))
        retry = asyncio.run(collect(
            FakeNvidiaRuntime([
                tool_turn("provider-plan", name="update_plan", arguments=arguments),
                final_turn(),
            ]),
            ledger,
        ))
    assert any(event["type"] == "plan" for event in events)
    assert canary in next(event["output"] for event in events if event["type"] == "tool_result")
    assert any(event["type"] == "plan" for event in retry)
    assert canary in next(event["output"] for event in retry if event["type"] == "tool_result")
    assert any(event.get("reused") is True for event in retry)
    raw = path.read_bytes()
    assert canary.encode() not in raw
    assert b"arguments_hash" in raw


def test_every_agent_tool_event_uses_the_complete_identity_contract():
    tree = ast.parse(textwrap.dedent(inspect.getsource(agent._run_agent_impl)))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        event_type = next(
            (
                value.value
                for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and key.value == "type"
                and isinstance(value, ast.Constant)
            ),
            None,
        )
        if event_type not in {"tool_call", "approval_request", "tool_result"}:
            continue
        checked += 1
        assert any(
            key is None and isinstance(value, ast.Name) and value.id == "event_ids"
            for key, value in zip(node.keys, node.values)
        ), f"{event_type} at line {node.lineno} omits event_ids"
    assert checked >= 6
