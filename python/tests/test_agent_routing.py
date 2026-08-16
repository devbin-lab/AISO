"""Regression contracts for deterministic, high-confidence Agent routing.

These cases deliberately exercise the raw user-authored request boundary.
They do not feed RAG excerpts, attachment text, tool results, or a model
completion into the router: those inputs must never change the selected tool.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import agent
import agent_routing as routing
from conftest import FakeChat
from llm import LlmEvent, LlmModelRuntime, LlmRequest


AVAILABLE = (
    "list_tree",
    "read_file",
    "analyze_document_calendar",
    "list_calendar_events",
    "convert_document",
    "web_search",
    "web_fetch",
    "create_calendar_event",
    "manage_calendar_event",
    "list_mydb_library",
    "list_mydb_history",
    "list_mydb_trash",
    "restore_mydb_trash_node",
    "discord_schedule_add",
)


def _route(request: str, *, no_workspace: bool = False) -> routing.RouteDecision:
    return routing.classify_request(
        request,
        AVAILABLE,
        no_workspace=no_workspace,
    )


def _phase_tools(decision: routing.RouteDecision) -> list[tuple[str, ...]]:
    return [phase.tool_names for phase in decision.phases]


def test_file_tree_requests_expose_only_list_tree_in_korean_and_english() -> None:
    requests = (
        "\ud604\uc7ac \uc791\uc5c5 \ud3f4\ub354\uc758 \ud30c\uc77c\uad6c\uc870 \ubcf4\uc5ec\uc918",
        "Show the current workspace file tree.",
    )

    for request in requests:
        decision = _route(request)
        assert decision.name == "workspace_tree"
        assert _phase_tools(decision) == [("list_tree",)]
        assert decision.phases[0].required_tool == "list_tree"
        schemas = [{"function": {"name": name}} for name in AVAILABLE]
        assert [
            schema["function"]["name"]
            for schema in routing.filter_tool_schemas(schemas, decision.phases[0])
        ] == ["list_tree"]


def test_document_summary_routes_to_read_file_not_document_todos_in_korean_and_english() -> None:
    requests = (
        "\uae30\ub9d0.pdf\ub97c \uc77d\uace0 \uc694\uc57d\ud574\uc918",
        "Read and summarize brief.pdf.",
    )

    for request in requests:
        decision = _route(request)
        assert decision.name == "document_read"
        assert _phase_tools(decision) == [("read_file",)]
        assert decision.phases[0].required_tool != "analyze_document_calendar"


def test_document_todo_request_routes_to_source_backed_todo_tool_in_korean_and_english() -> None:
    requests = (
        "\uae30\ub9d0.pdf\ub97c \uc77d\uace0 \ud574\uc57c\ud560 \uc77c\uc744 ToDo \ub9ac\uc2a4\ud2b8\ub85c \ub9cc\ub4e4\uc5b4\uc918",
        "Create a ToDo list from brief.pdf.",
    )

    for request in requests:
        decision = _route(request)
        assert decision.name == "document_calendar"
        assert _phase_tools(decision) == [("analyze_document_calendar",)]


def test_saved_todos_are_workspace_independent() -> None:
    requests = (
        "\ud604\uc7ac \uc800\uc7a5\ub41c ToDo \ub9ac\uc2a4\ud2b8 \ubcf4\uc5ec\uc918",
        "Show my saved ToDo list.",
    )

    for request in requests:
        decision = _route(request, no_workspace=True)
        assert decision.name == "saved_calendar"
        assert _phase_tools(decision) == [("list_calendar_events",)]
        assert decision.unavailable_tool is None


def test_mydb_metadata_and_daily_history_are_workspace_independent() -> None:
    library = _route("My DB에 어떤 코어와 파일이 있는지 보여줘", no_workspace=True)
    assert library.name == "mydb_library"
    assert _phase_tools(library) == [("list_mydb_library",)]

    history = _route("My DB 히스토리를 읽고 오늘 무엇이 바뀌었는지 보고서로 정리해줘", no_workspace=True)
    assert history.name == "mydb_history"
    assert _phase_tools(history) == [("list_mydb_history",)]

    saved_today = _route("오늘 DB에 저장된 것들이 뭐가 있는지 보고서 작성해줘", no_workspace=True)
    assert saved_today.name == "mydb_today_inventory"
    assert _phase_tools(saved_today) == [
        ("list_mydb_history",),
        ("list_mydb_library",),
    ]

    core_names = _route("생성된 코어 이름 전부 뭔지 알려줘", no_workspace=True)
    assert core_names.name == "mydb_core_inventory"
    assert _phase_tools(core_names) == [("list_mydb_library",)]


def test_mydb_restore_requires_trash_lookup_before_one_exact_restore() -> None:
    decision = _route("My DB 휴지통에서 삭제한 파일을 복구해줘", no_workspace=True)
    assert decision.name == "mydb_restore"
    assert _phase_tools(decision) == [
        ("list_mydb_trash",),
        ("restore_mydb_trash_node",),
    ]


def test_today_calendar_questions_use_the_saved_calendar_route() -> None:
    requests = (
        "오늘 할일 뭐가 있지?",
        "오늘 일정 보여줘.",
        "이번 주 해야 할 일 확인해줘.",
        "What do I need to do today?",
        "Show my schedule for this week.",
    )

    for request in requests:
        decision = _route(request, no_workspace=True)
        assert decision.name == "saved_calendar"
        assert _phase_tools(decision) == [("list_calendar_events",)]
        assert decision.unavailable_tool is None


def test_personal_repeating_calendar_requests_never_route_to_discord() -> None:
    requests = (
        "매주 일요일 오전 10시부터 오후 8시30분까지 알바 일정 등록해줘",
        "매일 오전 9시 운동 일정을 Aiso 캘린더에 추가해줘",
        "매년 12월 25일 가족 모임 일정 등록해줘",
        "Add my weekly Sunday work shift from 10:00 to 20:30 to the Aiso calendar.",
    )

    for request in requests:
        decision = _route(request, no_workspace=True)
        assert decision.name == "calendar_add"
        assert _phase_tools(decision) == [("create_calendar_event",)]
        assert "discord_schedule_add" not in decision.phases[0].tool_names


def test_personal_calendar_request_fails_closed_when_the_calendar_tool_is_disabled() -> None:
    decision = routing.classify_request(
        "매주 일요일 오전 10시부터 오후 8시30분까지 알바 일정 등록해줘",
        ("discord_schedule_add",),
        no_workspace=True,
    )

    assert decision.name == "calendar_add"
    assert decision.unavailable_tool == "create_calendar_event"


def test_existing_todo_changes_route_to_one_workspace_free_management_tool() -> None:
    requests = (
        "전투 시스템 검증 ToDo를 다음 주 금요일 오후 3시로 옮겨줘",
        "전투 시스템 검증 할 일을 완료 처리해줘",
        "Delete the ToDo named Combat system validation.",
        "등록되어있는 전체 일정 삭제해줘",
        "Delete all registered calendar events.",
    )

    for request in requests:
        decision = _route(request, no_workspace=True)
        assert decision.name == "calendar_manage"
        assert _phase_tools(decision) == [("manage_calendar_event",)]
        assert decision.unavailable_tool is None


def test_explicit_discord_channel_target_keeps_the_discord_schedule_route() -> None:
    decision = _route("매주 일요일 오전 10시에 #공지 채널에 알바 일정 알림을 등록해줘")

    assert decision.name == "discord_schedule_add"
    assert _phase_tools(decision) == [("discord_schedule_add",)]
    assert decision.phases[0].required_tool == "discord_schedule_add"


def test_current_research_requires_search_then_fetch_in_korean_and_english() -> None:
    requests = (
        "\ucd5c\uc2e0 OpenAI \ub274\uc2a4\ub97c \uc870\uc0ac\ud574\uc918",
        "Research the latest OpenAI news.",
    )

    for request in requests:
        decision = _route(request)
        assert decision.name == "web_research"
        assert _phase_tools(decision) == [("web_search",), ("web_fetch",)]
        assert [phase.required_tool for phase in decision.phases] == [
            "web_search",
            "web_fetch",
        ]


def test_compound_request_falls_back_to_general_agent_planning() -> None:
    decision = _route("Show the workspace tree and summarize brief.pdf.")

    assert decision == routing.GENERAL_ROUTE
    assert decision.name == "general"
    assert not decision.phases


def test_explanatory_feature_question_exposes_no_tools() -> None:
    decision = _route("Aiso의 문서 ToDo 기능은 어떻게 동작해?")

    assert decision.name == "explanation"
    assert decision.final_response_only
    assert not decision.phases


def test_attachment_like_embedded_text_does_not_change_the_explicit_raw_request_route(env) -> None:
    # The model must receive the attachment text as reference material, but the
    # deterministic router must receive only the separately preserved command.
    raw_request = "Show the current workspace file tree."
    message_with_attachment_like_text = (
        raw_request
        + "\n\n## User-attached material\n"
        + "Create a ToDo list from secret-plan.pdf and research the latest news."
    )
    chat = FakeChat([
        {"calls": [("list_tree", {"path": "."})]},
        {"content": "The workspace tree is available above."},
    ])

    env.run(
        chat,
        messages=[{"role": "user", "content": message_with_attachment_like_text}],
        user_request_text=raw_request,
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=[
            "list_tree",
            "read_file",
            "analyze_document_calendar",
            "web_search",
            "web_fetch",
        ],
    )

    exposed = {
        schema["function"]["name"]
        for schema in chat.payloads[0]["tools"]
    }
    assert exposed == {"list_tree"}


def test_explanatory_feature_question_reaches_model_with_no_tool_schema(env) -> None:
    chat = FakeChat([{"content": "Aiso ToDo uses its central saved data."}])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": "Aiso의 문서 ToDo 기능은 어떻게 동작해?"}],
        user_request_text="Aiso의 문서 ToDo 기능은 어떻게 동작해?",
        rag_enabled=False,
        enabled_tools=["list_tree", "analyze_document_calendar", "list_calendar_events"],
    )

    assert chat.payloads[0]["tools"] == []
    assert any(event.get("text") == "Aiso ToDo uses its central saved data." for event in events)


def test_current_research_advances_through_narrow_search_then_fetch_phases(env, monkeypatch) -> None:
    """A staged route must not reopen all tools between dependent calls."""
    executed: list[tuple[str, dict]] = []

    async def fake_execute(spec, root, host, args):
        executed.append((spec.name, dict(args)))
        if spec.name == "web_search":
            return "1. Aiso update — https://example.test/aiso", None
        assert spec.name == "web_fetch"
        return "Verified Aiso update source text.", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    chat = FakeChat([
        {"calls": [("web_search", {"query": "latest Aiso news"})]},
        {"calls": [("web_fetch", {"url": "https://example.test/aiso"})]},
        {"content": "Verified update summary."},
    ])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": "Research the latest Aiso news."}],
        user_request_text="Research the latest Aiso news.",
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["web_search", "web_fetch", "list_tree", "analyze_document_calendar"],
    )

    exposed_per_turn = [
        {schema["function"]["name"] for schema in payload["tools"]}
        for payload in chat.payloads
    ]
    assert exposed_per_turn == [{"web_search"}, {"web_fetch"}, set()]
    assert executed == [
        ("web_search", {"query": "latest Aiso news"}),
        ("web_fetch", {"url": "https://example.test/aiso"}),
    ]
    assert any(event.get("text") == "Verified update summary." for event in events)


def test_personal_calendar_route_exposes_only_a_central_todo_tool(env, monkeypatch) -> None:
    executed: list[tuple[str, dict]] = []

    async def fake_execute(spec, root, host, args):
        executed.append((spec.name, dict(args)))
        return "Aiso ToDo 캘린더 등록 완료", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    request = "Add my weekly Sunday work shift from 10:00 to 20:30 to the Aiso calendar."
    chat = FakeChat([
        {"calls": [("create_calendar_event", {"instruction": request})]},
        {"content": "등록했습니다."},
    ])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": request}],
        user_request_text=request,
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["create_calendar_event", "discord_schedule_add", "list_calendar_events"],
    )

    assert [{schema["function"]["name"] for schema in payload["tools"]} for payload in chat.payloads] == [
        {"create_calendar_event"}, set(),
    ]
    assert executed == [("create_calendar_event", {"instruction": request})]
    assert not any(event.get("name") == "discord_schedule_add" for event in events)


def test_today_calendar_lookup_falls_back_to_the_central_read_tool(env, monkeypatch) -> None:
    """A weak model must not answer “no schedule” without reading saved data."""
    executed: list[tuple[str, dict]] = []

    async def fake_execute(spec, root, host, args):
        executed.append((spec.name, dict(args)))
        return (
            "저장된 일정 1개 (완료 포함)\n"
            "1. [진행 전] 오늘 신규 일정 (우선순위 high, 2026-08-14 14:00, Aiso Calendar)\n"
            "   일정 ID: today-event",
            None,
        )

    monkeypatch.setattr(agent, "execute", fake_execute)
    request = "오늘 할일 뭐가 있지?"
    chat = FakeChat([
        {"content": "현재 저장된 일정이 없으므로, 오늘 예정된 할 일은 없습니다."},
        {"content": "오늘 일정이 없는 것 같습니다."},
        {"content": "오늘 14:00에 ‘오늘 신규 일정’이 있습니다."},
    ])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": request}],
        user_request_text=request,
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["list_calendar_events", "list_tree", "analyze_document_calendar"],
    )

    assert [{schema["function"]["name"] for schema in payload["tools"]} for payload in chat.payloads] == [
        {"list_calendar_events"}, {"list_calendar_events"}, set(),
    ]
    assert executed == [("list_calendar_events", {})]
    assert any(event.get("text") == "오늘 14:00에 ‘오늘 신규 일정’이 있습니다." for event in events)
    assert any(event.get("transient") is True for event in events if event.get("type") == "notice")


def test_personal_calendar_route_falls_back_after_two_prose_turns(env, monkeypatch) -> None:
    """A weak model must not turn an explicit calendar request into a no-op."""
    executed: list[tuple[str, dict]] = []

    async def fake_execute(spec, root, host, args):
        executed.append((spec.name, dict(args)))
        return "Aiso ToDo 캘린더 등록 완료", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    request = "Add my weekly Sunday work shift from 10:00 to 20:30 to the Aiso calendar."
    chat = FakeChat([
        {"content": "I will add that schedule."},
        {"content": "Understood."},
        {"content": "The event has been saved."},
    ])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": request}],
        user_request_text=request,
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["create_calendar_event", "discord_schedule_add"],
    )

    assert [{schema["function"]["name"] for schema in payload["tools"]} for payload in chat.payloads] == [
        {"create_calendar_event"}, {"create_calendar_event"}, set(),
    ]
    assert executed == [("create_calendar_event", {"instruction": request})]
    assert any(event.get("type") == "notice" for event in events)
    assert not any(event.get("name") == "discord_schedule_add" for event in events)


def test_explicit_all_calendar_delete_falls_back_to_management_tool(env, monkeypatch) -> None:
    """A weak model must not turn an explicit whole-calendar deletion into a no-op."""
    executed: list[tuple[str, dict]] = []

    async def fake_execute(spec, root, host, args):
        executed.append((spec.name, dict(args)))
        return "Aiso 캘린더 전체 일정 삭제 완료\n- 삭제한 일정: 3개", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    request = "등록되어있는 전체 일정 삭제해줘"
    chat = FakeChat([
        {"content": "요청을 확인했습니다."},
        {"content": "일정을 정리하겠습니다."},
        {"content": "삭제를 완료했습니다."},
    ])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": request}],
        user_request_text=request,
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["manage_calendar_event", "list_calendar_events"],
    )

    assert [{schema["function"]["name"] for schema in payload["tools"]} for payload in chat.payloads] == [
        {"manage_calendar_event"}, {"manage_calendar_event"}, set(),
    ]
    assert executed == [("manage_calendar_event", {"instruction": request})]
    assert any(event.get("type") == "notice" for event in events)


def test_nvidia_personal_calendar_route_forces_only_the_calendar_tool(monkeypatch) -> None:
    """NVIDIA must not spend a turn deciding between Discord and Aiso calendar."""

    class _Runtime:
        def __init__(self) -> None:
            self.requests: list[LlmRequest] = []
            self.turns = [
                [
                    LlmEvent(kind="tool_call_delta", tool_calls=[{
                        "index": 0,
                        "id": "calendar-1",
                        "function": {
                                "name": "create_calendar_event",
                            "arguments": '{"instruction":"매주 일요일 오전 10시부터 오후 8시30분까지 알바 일정 등록해줘"}',
                        },
                    }]),
                    LlmEvent(kind="done", done_reason="tool_calls"),
                ],
                [
                    LlmEvent(kind="content", text="Aiso ToDo에 등록했습니다."),
                    LlmEvent(kind="done", done_reason="stop"),
                ],
            ]

        async def prepare_model(self, model: str) -> LlmModelRuntime:
            return LlmModelRuntime(model=model)

        def prepare_attempts(self, request, _reasoning_effort, _model_runtime):
            return [request]

        async def chat_stream(self, request: LlmRequest):
            self.requests.append(request)
            for event in self.turns.pop(0):
                yield event

    async def fake_execute(spec, root, host, args):
        assert spec.name == "create_calendar_event"
        return "Aiso ToDo 캘린더 등록 완료", None

    monkeypatch.setattr(agent, "execute", fake_execute)
    runtime = _Runtime()
    request = "매주 일요일 오전 10시부터 오후 8시30분까지 알바 일정 등록해줘"

    async def drive() -> list[dict]:
        return [
            event
            async for event in agent.run_agent(
                host="unused",
                workspace="",
                model="nvidia/test-model",
                messages=[{"role": "user", "content": request}],
                user_request_text=request,
                session_id="calendar-nvidia",
                approval_mode="auto",
                provider="nvidia",
                runtime=runtime,
                nvidia_allowed_tools=["create_calendar_event"],
                enabled_tools=["create_calendar_event"],
                rag_enabled=False,
            )
        ]

    events = asyncio.run(drive())
    assert runtime.requests[0].provider_options["tool_choice"] == {
        "type": "function",
        "function": {"name": "create_calendar_event"},
    }
    assert [
        {tool["function"]["name"] for tool in sent.tools}
        for sent in runtime.requests
    ] == [{"create_calendar_event"}, set()]
    assert [event.get("name") for event in events if event.get("type") == "tool_call"] == [
        "create_calendar_event"
    ]


def test_wrong_tool_for_a_narrow_route_is_not_executed_and_recovers_once(env) -> None:
    """A small-model selection error must not mutate central ToDos."""
    chat = FakeChat([
        {"calls": [("analyze_document_calendar", {"paths": ["brief.pdf"]})]},
        {"calls": [("list_tree", {"path": "."})]},
        {"content": "The workspace tree is available above."},
    ])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": "현재 작업 폴더의 파일구조 보여줘"}],
        user_request_text="현재 작업 폴더의 파일구조 보여줘",
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["list_tree", "analyze_document_calendar"],
    )

    executed_names = [event["name"] for event in events if event.get("type") == "tool_call"]
    assert executed_names == ["list_tree"]
    assert any(
        event.get("type") == "notice" and event.get("transient") is True
        for event in events
    )
    assert any("다른 도구 호출은 실행하지 않고" in event.get("text", "") for event in events)
    assert [
        {schema["function"]["name"] for schema in payload["tools"]}
        for payload in chat.payloads
    ] == [{"list_tree"}, {"list_tree"}, set()]


def test_malformed_tool_arguments_are_not_dispatched_and_recover_once(env) -> None:
    chat = FakeChat([
        {"calls": [("list_tree", '{"path":')]},
        {"calls": [("list_tree", {"path": "."})]},
        {"content": "Verified workspace tree."},
    ])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": "Show the current workspace file tree."}],
        user_request_text="Show the current workspace file tree.",
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["list_tree"],
    )

    assert [event["name"] for event in events if event.get("type") == "tool_call"] == ["list_tree"]
    assert any(
        event.get("type") == "notice" and event.get("transient") is True
        for event in events
    )
    assert any("도구 인자 형식이 올바르지 않아" in event.get("text", "") for event in events)
    assert not any(event.get("type") == "error" for event in events)


def test_missing_required_tool_call_retry_notice_is_run_scoped(env) -> None:
    """A routing retry is progress feedback, never a post-response warning."""
    chat = FakeChat([
        {"content": "I will inspect the workspace tree."},
        {"calls": [("list_tree", {"path": "."})]},
        {"content": "The workspace tree is available above."},
    ])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": "Show the current workspace file tree."}],
        user_request_text="Show the current workspace file tree.",
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["list_tree"],
    )

    notices = [event for event in events if event.get("type") == "notice"]
    assert len(notices) == 1
    assert notices[0]["transient"] is True
    assert [event["name"] for event in events if event.get("type") == "tool_call"] == ["list_tree"]


def test_mydb_core_inventory_never_accepts_a_hallucinated_list_without_lookup(env) -> None:
    fake_cores = "강의_실습_자료\n프로젝트_기획서\n연구_논문_아카이브"
    chat = FakeChat([
        {"content": fake_cores},
        {"content": fake_cores},
    ])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": "생성된 코어 이름 전부 뭔지 알려줘"}],
        user_request_text="생성된 코어 이름 전부 뭔지 알려줘",
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["list_mydb_library"],
    )

    assert not [event for event in events if event.get("type") == "tool_call"]
    assert fake_cores not in "\n".join(str(event.get("text") or "") for event in events)
    assert any("list_mydb_library" in str(event.get("text") or "") for event in events)


def test_mydb_core_inventory_renders_only_exact_lookup_titles(env) -> None:
    async def exact_library(**_kwargs) -> str:
        return json.dumps({
            "totalMatches": 2,
            "returned": 2,
            "nodes": [
                {"id": "core-a", "kind": "core", "title": "유니티 학습"},
                {"id": "core-b", "kind": "core", "title": "게임데이터의설계"},
            ],
        }, ensure_ascii=False)

    spec = agent.REGISTRY["list_mydb_library"]
    env.mp.setitem(agent.REGISTRY, "list_mydb_library", replace(spec, handler=exact_library))
    chat = FakeChat([{"calls": [("list_mydb_library", {"kind": "core", "limit": 240})]}])

    events = env.run(
        chat,
        messages=[{"role": "user", "content": "생성된 코어 이름 전부 뭔지 알려줘"}],
        user_request_text="생성된 코어 이름 전부 뭔지 알려줘",
        rag_enabled=False,
        approval_mode="auto",
        enabled_tools=["list_mydb_library"],
    )

    content = "\n".join(str(event.get("text") or "") for event in events if event.get("type") == "content")
    assert "유니티 학습" in content
    assert "게임데이터의설계" in content
    assert "강의_실습_자료" not in content
    assert chat.calls == 1
