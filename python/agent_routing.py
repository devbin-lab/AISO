"""Conservative, deterministic request-to-tool routing for Aiso Agent.

This module is deliberately narrow.  It only classifies requests whose first
operation is unambiguous, then exposes the smallest tool set needed for that
operation.  Everything ambiguous, compound, explanatory, or dependent stays
on the normal Agent path so a rule never silently removes a capability the
user actually requested.

The router consumes only the raw user-authored request.  Attachment text, RAG
context, previous tool output, and model output are untrusted data and must
never affect this decision.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable, Sequence


_DOCUMENT_SUFFIXES = "pdf|pptx|pptm|docx|xlsx|xls|hwp|hwpx|md|txt|csv"
_DOCUMENT_PATH_RE = re.compile(
    rf"(?<![\w./\\-])([^\s\n\r\t,;:()\[\]{{}}<>\"']+?\.(?:{_DOCUMENT_SUFFIXES}))"
    r"(?=$|[\s,;:()\[\]{}<>\"'.!?]|(?:을|를|은|는|이|가|에|의|와|과|으로|로|도|만|에서|에게|부터))",
    re.IGNORECASE,
)
_QUOTED_DOCUMENT_PATH_RE = re.compile(
    rf"[\"'“”‘’]([^\"'“”‘’\n\r]+\.(?:{_DOCUMENT_SUFFIXES}))[\"'“”‘’]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RoutePhase:
    """One bounded, model-visible phase of an otherwise simple request."""

    tool_names: tuple[str, ...]
    required_tool: str
    complete_after_success: bool = False
    max_successes: int = 1


@dataclass(frozen=True)
class RouteDecision:
    """A high-confidence routing decision, or ``general`` for normal Agent flow."""

    name: str = "general"
    phases: tuple[RoutePhase, ...] = ()
    reason: str = ""
    unavailable_tool: str | None = None
    final_response_only: bool = False

    @property
    def constrained(self) -> bool:
        return bool(self.phases) or self.final_response_only

    @property
    def skips_automatic_rag(self) -> bool:
        return self.constrained

    def phase(self, index: int) -> RoutePhase | None:
        return self.phases[index] if 0 <= index < len(self.phases) else None


GENERAL_ROUTE = RouteDecision()


def _normalise(text: str) -> str:
    return " ".join(str(text or "").casefold().split())


def _document_paths(text: str) -> tuple[str, ...]:
    """Extract only explicitly typed document paths, never inferred file names."""
    seen: set[str] = set()
    paths: list[str] = []
    for match in [*_QUOTED_DOCUMENT_PATH_RE.finditer(text), *_DOCUMENT_PATH_RE.finditer(text)]:
        raw = match.group(1).strip().replace("\\", "/")
        if not raw or raw.casefold() in seen:
            continue
        seen.add(raw.casefold())
        paths.append(raw)
    return tuple(paths)


def _contains_any(text: str, markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers)


def _is_explanatory_question(text: str) -> bool:
    """Do not route a question *about* a feature as a request to run it."""
    return bool(
        re.search(
            r"(?:왜|무엇|뭐야|어떻게\s*(?:동작|작동|사용)|(?:사용)?방법(?:을)?\s*(?:알려|설명)|사용법|가능(?:한가|해)|설명|원인|what is|why does|how does|can (?:it|aiso)|explain)",
            text,
        )
    )


def _is_compound(text: str, categories: Sequence[bool]) -> bool:
    """A rule must not steal a request containing two independent goals."""
    return sum(bool(value) for value in categories) > 1 or bool(
        re.search(r"(?:그리고|및|또한|그 다음|after that|and then|as well as)\s+.*(?:보여|읽|요약|todo|할 일|조사|send|create)", text)
    )


def _requests_research_before_image(
    text: str,
    *,
    image_generation_requested: bool,
) -> bool:
    """Recognise an explicit ``research -> generate image`` instruction.

    This must stay narrower than ordinary image detection.  A request merely
    mentioning ComfyUI, the web, or a character name is not enough: it needs a
    research verb *and* a generation action, plus either an online-source cue
    or an explicit sequencing cue.  That preserves the fast single-tool path
    for normal illustration requests while preventing a source-grounded request
    from silently becoming a generic portrait.
    """
    if not image_generation_requested:
        return False

    has_research_verb = _contains_any(text, (
        "검색", "조사", "찾아", "찾고", "알아", "search", "research", "look up", "look-up", "find",
    ))
    has_image_action = _contains_any(text, (
        "그려", "그림", "이미지", "일러스트", "생성", "draw", "generate", "illustration", "image",
    ))
    if not (has_research_verb and has_image_action):
        return False

    has_online_source_cue = _contains_any(text, (
        "인터넷", "웹", "온라인", "internet", "web", "online",
    ))
    has_sequence_cue = _contains_any(text, (
        "한뒤", "한 뒤", "후", "다음", "해서", "하여", "then", "after", "and then",
    ))
    return has_online_source_cue or has_sequence_cue


def _route(
    name: str,
    *,
    tool_names: Sequence[str],
    required: str,
    reason: str,
    complete_after_success: bool = True,
    max_successes: int = 1,
) -> RouteDecision:
    available = frozenset(tool_names)
    if required not in available:
        return RouteDecision(name=name, reason=reason, unavailable_tool=required)
    return RouteDecision(
        name=name,
        reason=reason,
        phases=(
            RoutePhase(
                tool_names=(required,),
                required_tool=required,
                complete_after_success=complete_after_success,
                max_successes=max_successes,
            ),
        ),
    )


def classify_request(
    raw_user_text: str,
    available_tool_names: Iterable[str],
    *,
    no_workspace: bool,
    image_generation_requested: bool = False,
) -> RouteDecision:
    """Return a constrained route only when the first operation is obvious.

    ``no_workspace`` is part of the decision because document/file requests
    must never be misrepresented as successful conversational answers when the
    required local tool is unavailable.
    """
    raw = str(raw_user_text or "").strip()
    text = _normalise(raw)
    available = tuple(dict.fromkeys(str(name) for name in available_tool_names))
    if not text:
        return GENERAL_ROUTE
    explanatory_question = _is_explanatory_question(text)
    research_before_image = _requests_research_before_image(
        text,
        image_generation_requested=image_generation_requested,
    )

    # This is intentionally checked before the generic compound-request
    # fallback.  "Search for a character, identify its traits, then draw it"
    # is one ordered operation, not two independent requests.  Each phase is
    # least-privilege and fail-closed: if research is unavailable, Aiso must not
    # substitute a generic image.
    if research_before_image:
        required_tools = ("web_search", "web_fetch", "generate_image")
        available_set = frozenset(available)
        missing = next((name for name in required_tools if name not in available_set), None)
        if missing is not None:
            return RouteDecision(
                name="research_image",
                reason="The user explicitly requested source-grounded web research before image generation.",
                unavailable_tool=missing,
            )
        return RouteDecision(
            name="research_image",
            reason="The user explicitly requested source-grounded web research before image generation.",
            phases=(
                RoutePhase(("web_search",), "web_search"),
                RoutePhase(("web_fetch",), "web_fetch"),
                RoutePhase(("generate_image",), "generate_image", complete_after_success=True),
            ),
        )

    # The direct-image path is intentionally handled by Agent Runner after
    # routing.  Do not let incidental words such as "latest" or "research"
    # repurpose an ordinary image prompt into a research-only request; only the
    # explicit ordered route above is allowed to retain web tools.
    if image_generation_requested:
        return GENERAL_ROUTE

    documents = _document_paths(raw)
    has_document = bool(documents)
    todo_words = _contains_any(text, (
        "todo", "to-do", "할 일", "할일", "해야 할 일", "해야할일", "작업 목록", "업무 목록", "task list",
    ))
    calendar_lookup_words = todo_words or _contains_any(text, (
        "일정", "스케줄", "캘린더", "calendar", "schedule", "agenda", "appointment",
    ))
    calendar_event_action = _contains_any(text, (
        "등록", "추가", "잡아", "넣어", "만들어", "schedule", "add", "create",
    ))
    calendar_explicit_create_verb = _contains_any(text, (
        "등록", "추가", "잡아", "넣어", "만들어", "add", "create",
    ))
    todo_action = _contains_any(text, (
        "만들", "생성", "추출", "정리", "저장", "등록", "목록화", "extract", "create", "save", "turn",
    ))
    todo_manage = not has_document and _contains_any(text, (
        "todo", "to-do", "할 일", "할일", "일정", "스케줄", "calendar", "task",
    )) and _contains_any(text, (
        "수정", "변경", "옮겨", "이동", "미뤄", "당겨", "완료", "끝냈", "다시 열", "미완료",
        "삭제", "지워", "제거", "이름", "우선순위", "시간 바꿔", "날짜 바꿔",
        "update", "change", "move", "reschedule", "complete", "reopen", "delete", "remove", "rename", "priority",
    ))
    folder_tree = _contains_any(text, (
        "파일구조", "파일 구조", "폴더구조", "폴더 구조", "디렉터리 구조", "폴더 트리", "파일 트리",
        "file structure", "folder structure", "directory structure", "file tree", "folder tree", "workspace tree",
    ))
    # Calendar questions often omit the words “stored” or “list”.  In
    # particular, “오늘 할 일 뭐가 있지?” is a direct query against Aiso's
    # central calendar, not a conversational planning request.  Keep this
    # narrow: an explicit task/calendar noun is still required, so unrelated
    # questions such as “오늘 날씨가 어때?” cannot reach the local calendar.
    calendar_lookup_time = _contains_any(text, (
        "오늘", "내일", "모레", "이번 주", "이번주", "다음 주", "다음주", "이번 달", "이번달",
        "today", "tomorrow", "this week", "next week", "this month",
    ))
    implicit_today_work_question = bool(re.search(
        r"(?:오늘\s*(?:뭐|무엇).{0,20}(?:해야|할)|\bwhat\b.{0,32}\b(?:need to do|tasks?)\b)",
        text,
    ))
    saved_todos = (calendar_lookup_words or implicit_today_work_question) and (
        calendar_lookup_time or _contains_any(text, (
            "현재", "저장", "내 ", "나의", "목록 보여", "확인", "show", "list", "current", "saved", "my ",
        ))
    ) and not has_document and not todo_manage and not calendar_explicit_create_verb
    document_todos = has_document and todo_words and todo_action
    document_read = has_document and _contains_any(text, (
        "읽", "요약", "분석", "내용", "정리", "read", "summar", "analy", "review",
    )) and not document_todos
    document_convert = has_document and (
        _contains_any(text, (
            "html로 변환", "html 변환", "브라우저", "browser-readable",
        ))
        or bool(re.search(r"\bconvert\b.{0,40}\bhtml\b", text))
    )
    current_research = _contains_any(text, (
        "최신", "오늘 기준", "현재 뉴스", "뉴스 조사", "웹 조사", "웹검색", "인터넷 조사",
        "search the latest", "latest news", "current news", "research the latest", "research current",
    ))
    discord_map = _contains_any(text, (
        "디스코드 서버", "discord server", "채널 구조", "channel structure",
    )) and _contains_any(text, ("구조", "목록", "보여", "확인", "map", "show", "list"))
    discord_schedule_list = _contains_any(text, ("예약 목록", "등록된 예약", "scheduled messages", "list schedules"))
    discord_channel_report = _contains_any(text, (
        "대화 내역을 정리", "대화내역을 정리", "새 메시지만", "채널 보고서", "channel report", "only new messages",
    )) and _contains_any(text, ("시간", "매일", "hour", "daily", "마다", "interval"))
    discord_schedule_add = _contains_any(text, ("예약", "매일", "매주", "schedule", "daily")) and _contains_any(text, ("채널", "공지", "discord", "보내", "send")) and not discord_channel_report and not discord_schedule_list
    discord_send = _contains_any(text, ("#", "채널", "공지", "discord")) and _contains_any(text, ("보내", "전송", "send", "post")) and not discord_schedule_add
    # A personal "알바 일정 등록" is an Aiso calendar write, not a Discord
    # message schedule.  Require an event noun, a create verb, and a concrete
    # time/date/recurrence cue so a vague planning sentence still reaches the
    # normal Agent path.  Explicit Discord targeting always wins.
    calendar_event_noun = _contains_any(text, (
        "일정", "스케줄", "캘린더", "calendar", "appointment", "shift",
    ))
    calendar_event_time = _contains_any(text, (
        "매일", "매주", "매월", "매년", "매해", "오늘", "내일", "모레", "오전", "오후",
        "every day", "every week", "every month", "every year", "daily", "weekly", "monthly", "yearly",
        "am", "pm",
    )) or bool(re.search(r"(?<!\d)(?:[01]?\d|2[0-3])\s*(?::|시)", text))
    calendar_event = (
        not has_document and calendar_event_noun and calendar_event_action and calendar_event_time
        and not (discord_map or discord_schedule_list or discord_channel_report or discord_schedule_add or discord_send)
    )
    # My DB is a separate personal library.  Do not route a generic database
    # programming question here; Korean users often abbreviate it to “DB”, so
    # accept that only with unmistakable library/history/trash language.
    mydb_explicit = _contains_any(text, ("my db", "mydb", "마이db", "마이 db", "마이디비"))
    mydb_context = _contains_any(text, (
        "코어", "휴지통", "복구", "히스토리", "변경 이력", "변경내역", "변경 내역", "변경점",
        "저장소", "저장된", "저장한", "보관", "라이브러리", "내 db", "내db", "db 내용", "db목록", "db 목록",
        "library", "trash", "restore", "change history", "change report",
    ))
    mydb_core_inventory = not _contains_any(text, ("파일", "file", "files")) and _contains_any(text, ("코어", "core")) and _contains_any(text, (
        "이름", "목록", "전부", "전체", "몇 개", "몇개", "알려", "보여", "list", "names", "all",
    ))
    mydb_requested = mydb_explicit or ("db" in text and mydb_context) or mydb_core_inventory
    mydb_restore = mydb_requested and _contains_any(text, ("복구", "되돌", "restore"))
    mydb_trash = mydb_requested and _contains_any(text, ("휴지통", "trash"))
    mydb_history = mydb_requested and _contains_any(text, (
        "히스토리", "변경", "바뀐", "변화", "하루", "오늘", "보고서", "보고", "history", "changed", "report",
    ))
    mydb_today_inventory = mydb_history and _contains_any(text, ("오늘", "하루", "today")) and _contains_any(text, (
        "저장된", "저장한", "보관", "등록된", "뭐가 있는", "무엇이 있는", "what was saved", "saved today",
    ))
    mydb_library = mydb_requested and not (mydb_history or mydb_trash or mydb_restore)

    # Explicit state lookups and named-document/current-research actions take
    # precedence over a conversational word such as "what".  Conversely,
    # questions *about* a feature (for example "How does ToDo work?") must
    # not expose a mutating or workspace tool merely because they mention it.
    if explanatory_question and not (
        saved_todos or todo_manage or document_todos or document_read or current_research or calendar_event
        or mydb_library or mydb_history or mydb_today_inventory or mydb_trash or mydb_restore
    ):
        return RouteDecision(
            name="explanation",
            reason="The user asked for an explanation rather than an external or local operation.",
            final_response_only=True,
        )

    # If a request contains more than one actionable category, normal Agent
    # planning remains more useful than an overconfident one-tool route.
    if _is_compound(text, (
        folder_tree,
        saved_todos,
        todo_manage,
        document_todos,
        document_read,
        current_research,
        calendar_event,
        mydb_library or mydb_history or mydb_today_inventory or mydb_trash or mydb_restore,
        discord_map or discord_schedule_list or discord_channel_report or discord_schedule_add or discord_send,
    )):
        return GENERAL_ROUTE

    if folder_tree:
        return _route(
            "workspace_tree", tool_names=available, required="list_tree",
            reason="The user explicitly requested a complete workspace file tree.",
        )
    if saved_todos:
        return _route(
            "saved_calendar", tool_names=available, required="list_calendar_events",
            reason="The user explicitly requested Aiso's saved calendar items.",
        )
    if todo_manage:
        return _route(
            "calendar_manage", tool_names=available, required="manage_calendar_event",
            reason="The user explicitly requested a change to existing Aiso calendar events.",
        )
    if calendar_event:
        return _route(
            "calendar_add", tool_names=available, required="create_calendar_event",
            reason="The user explicitly requested a personal Aiso calendar event, not a Discord schedule.",
        )
    if mydb_restore:
        required = ("list_mydb_trash", "restore_mydb_trash_node")
        available_set = frozenset(available)
        missing = next((name for name in required if name not in available_set), None)
        if missing is not None:
            return RouteDecision(
                name="mydb_restore", reason="The user explicitly requested restoring a My DB trashed item.", unavailable_tool=missing,
            )
        return RouteDecision(
            name="mydb_restore",
            reason="The user explicitly requested restoring a My DB trashed item.",
            phases=(
                RoutePhase(("list_mydb_trash",), "list_mydb_trash"),
                RoutePhase(("restore_mydb_trash_node",), "restore_mydb_trash_node", complete_after_success=True),
            ),
        )
    if mydb_trash:
        return _route(
            "mydb_trash", tool_names=available, required="list_mydb_trash",
            reason="The user explicitly requested the My DB trash list.",
        )
    if mydb_history:
        if mydb_today_inventory:
            required = ("list_mydb_history", "list_mydb_library")
            available_set = frozenset(available)
            missing = next((name for name in required if name not in available_set), None)
            if missing is not None:
                return RouteDecision(
                    name="mydb_today_inventory", reason="The user explicitly requested today's stored My DB items.", unavailable_tool=missing,
                )
            return RouteDecision(
                name="mydb_today_inventory",
                reason="The user explicitly requested today's stored My DB items and an evidence-based report.",
                phases=(
                    RoutePhase(("list_mydb_history",), "list_mydb_history"),
                    RoutePhase(("list_mydb_library",), "list_mydb_library", complete_after_success=True),
                ),
            )
        return _route(
            "mydb_history", tool_names=available, required="list_mydb_history",
            reason="The user explicitly requested a My DB change-history lookup or report.",
        )
    if mydb_library:
        if mydb_core_inventory:
            return _route(
                "mydb_core_inventory", tool_names=available, required="list_mydb_library",
                reason="The user explicitly requested the names of My DB cores.",
            )
        return _route(
            "mydb_library", tool_names=available, required="list_mydb_library",
            reason="The user explicitly requested a My DB library lookup.",
        )
    if document_convert:
        return _route(
            "document_convert", tool_names=available, required="convert_document",
            reason="The user explicitly requested a browser-readable document conversion.",
        )
    if document_todos:
        return _route(
            "document_calendar", tool_names=available, required="analyze_document_calendar",
            reason="The user explicitly requested source-backed calendar items from a named document.",
        )
    if document_read:
        return _route(
            "document_read", tool_names=available, required="read_file",
            reason="The user explicitly requested reading or summarising a named document.",
        )
    if current_research:
        if "web_search" not in set(available) or "web_fetch" not in set(available):
            missing = "web_search" if "web_search" not in set(available) else "web_fetch"
            return RouteDecision(
                name="web_research", reason="The user explicitly requested current external research.", unavailable_tool=missing
            )
        return RouteDecision(
            name="web_research",
            reason="The user explicitly requested current external research.",
            phases=(
                RoutePhase(("web_search",), "web_search"),
                RoutePhase(("web_fetch",), "web_fetch", complete_after_success=True),
            ),
        )
    if discord_map:
        return _route("discord_map", tool_names=available, required="discord_server_map", reason="The user requested the current Discord server structure.")
    if discord_schedule_list:
        return _route("discord_schedule_list", tool_names=available, required="discord_schedule_list", reason="The user requested existing Discord schedules.")
    if discord_channel_report:
        return _route("discord_channel_report", tool_names=available, required="discord_channel_report_add", reason="The user requested a periodic new-message channel report.")
    if discord_schedule_add:
        return _route("discord_schedule_add", tool_names=available, required="discord_schedule_add", reason="The user requested a Discord schedule.")
    if discord_send:
        return _route("discord_send", tool_names=available, required="discord_send", reason="The user requested a Discord message send.")
    return GENERAL_ROUTE


def filter_tool_schemas(schemas: Sequence[dict], phase: RoutePhase | None) -> list[dict]:
    """Return a fresh ordered schema subset for the current route phase."""
    if phase is None:
        return []
    allowed = frozenset(phase.tool_names)
    return [
        schema for schema in schemas
        if str(schema.get("function", {}).get("name") or "") in allowed
    ]


def route_policy_prompt(decision: RouteDecision, phase: RoutePhase | None) -> str:
    """Short English policy sent only to the model, not the desktop UI."""
    if not decision.constrained or phase is None:
        return ""
    names = ", ".join(phase.tool_names)
    if decision.name == "calendar_add":
        return (
            "\n\n## Deterministic calendar route\n"
            "- This is a personal Aiso calendar registration, never a Discord schedule or message.\n"
            "- Call create_calendar_event now with exactly one JSON field: `instruction`, containing the user's complete schedule request verbatim.\n"
            "- Do not infer a channel, inspect Discord, call another tool, or answer before the registration result.\n"
        )
    if decision.name == "calendar_manage":
        return (
            "\n\n## Deterministic calendar management route\n"
            "- This request changes calendar events already stored in Aiso.\n"
            "- Call manage_calendar_event now. Pass the complete original user request in `instruction`.\n"
            "- For a single event, include todo_id or target_title only when it is known from the conversation; never guess a target.\n"
            "- A whole-calendar deletion is allowed only when the original instruction explicitly says to delete every registered/saved calendar event.\n"
            "- Do not call Discord, workspace, or document tools. Ambiguity must fail without mutation.\n"
        )
    if decision.name == "mydb_restore":
        return (
            "\n\n## Deterministic My DB restore route\n"
            "- First call list_mydb_trash. Then use only the exact node ID returned by that result with restore_mydb_trash_node.\n"
            "- Do not create, edit, delete, link, export, or read file contents. If no matching trashed item exists, report that and do not call restore.\n"
        )
    if decision.name == "mydb_today_inventory":
        if phase.required_tool == "list_mydb_history":
            return (
                "\n\n## Deterministic My DB daily report route\n"
                "- Call list_mydb_history now with period='today'. Do not answer before its result.\n"
            )
        return (
            "\n\n## Deterministic My DB daily report route\n"
            "- Call list_mydb_library now with updated_period='today' and limit=240.\n"
            "- Then report only evidence from both results: time range, change counts, cores, file categories, and notable files.\n"
            "- Use the returned hierarchy as the report outline: start at each top-level core, then descend through child cores and their matched files. Put unlinked items in a separate final section.\n"
            "- Do not present the entire library as today's additions, invent folders, or claim file contents were read.\n"
        )
    if decision.name == "mydb_core_inventory":
        return (
            "\n\n## Deterministic My DB core inventory route\n"
            "- Call list_mydb_library now with kind='core' and limit=240. Do not answer before its result.\n"
            "- The application renders the final core list directly from that result. Do not invent, rename, sort differently, or summarize core names.\n"
        )
    if decision.name == "mydb_library":
        return (
            "\n\n## Deterministic My DB library route\n"
            "- Call list_mydb_library now. Do not answer before its result.\n"
            "- When reporting the library, use its hierarchy strictly top-down: top-level core, child cores, then files. Keep unlinked items separate and never infer a folder relationship from a title.\n"
        )
    return (
        "\n\n## Deterministic tool route\n"
        f"- This request was classified as {decision.name}: {decision.reason}\n"
        f"- Call {phase.required_tool} now. The only tool available in this phase is: {names}.\n"
        "- Do not answer from memory, invent a result, call another tool, or ask a question before the required tool result.\n"
    )


def final_response_only_prompt(decision: RouteDecision) -> str:
    if not decision.final_response_only:
        return ""
    return (
        "\n\n## Deterministic response route\n"
        f"- This request was classified as {decision.name}: {decision.reason}\n"
        "- Answer directly from the conversation and established product knowledge. Do not call a tool, claim a local inspection, or invent current external facts.\n"
    )


def route_recovery_prompt(decision: RouteDecision, phase: RoutePhase) -> str:
    return (
        "Aiso routing correction: no tool was executed. "
        f"For this {decision.name} request, call {phase.required_tool} now with valid JSON arguments. "
        "Do not explain, use another tool, or invent a result."
    )


def route_completion_prompt(decision: RouteDecision) -> str:
    if decision.name == "research_image":
        return (
            "Aiso routing state: web research and source-grounded image generation are complete. "
            "Do not call another tool. State the source title or URL actually fetched, briefly identify the "
            "source-backed character traits reflected in the image prompt, and disclose any unresolved ambiguity. "
            "Do not claim traits, sources, or visual details that were not present in the fetched result."
        )
    if decision.name == "mydb_today_inventory":
        return (
            "Aiso routing state: today's My DB history and metadata are complete. Do not call another tool. "
            "Write a concise report in the user's requested response language with the checked time range, total changes, action counts, cores, file categories, "
            "and representative file names. Use the My DB hierarchy strictly top-down: each top-level core, then its child cores, then the matched files. "
            "Keep unlinked items separate. State clearly when the result is based on metadata rather than file contents."
        )
    return (
        "Aiso routing state: the required tool result is now available. "
        "Do not call another tool. Give a concise, evidence-based answer using only that result."
    )


def route_direct_result_message(
    decision: RouteDecision,
    result: str,
    response_language: str,
) -> str | None:
    """Render exact, low-ambiguity inventories without another model turn."""
    if decision.name != "mydb_core_inventory":
        return None
    try:
        value = json.loads(result)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    nodes = value.get("nodes")
    if not isinstance(nodes, list):
        return None
    titles = [
        str(node.get("title") or "").strip()
        for node in nodes
        if isinstance(node, dict) and node.get("kind") == "core" and str(node.get("title") or "").strip()
    ]
    returned = value.get("returned")
    total = value.get("totalMatches")
    complete = isinstance(returned, int) and isinstance(total, int) and returned >= total
    language = str(response_language or "").casefold()
    if language == "ko":
        if not titles:
            return "현재 My DB에 저장된 활성 코어가 없습니다."
        suffix = "" if complete else "\n\n표시된 목록은 조회 한도 내 결과입니다."
        return f"현재 My DB에 저장된 코어 {len(titles)}개입니다.\n\n" + "\n".join(f"- {title}" for title in titles) + suffix
    if not titles:
        return "There are no active cores in My DB."
    suffix = "" if complete else "\n\nThis is the bounded result returned by the lookup."
    return f"My DB currently contains {len(titles)} core(s).\n\n" + "\n".join(f"- {title}" for title in titles) + suffix


def route_next_phase_prompt(decision: RouteDecision, phase: RoutePhase) -> str:
    """Tell the model that one deterministic phase completed safely."""
    if decision.name == "research_image" and phase.required_tool == "web_fetch":
        return (
            "Aiso routing state: search results are available. Open one result that clearly identifies the requested "
            "character and describes its appearance or franchise. Do not generate an image or infer traits from a search snippet."
        )
    if decision.name == "research_image" and phase.required_tool == "generate_image":
        return (
            "Aiso routing state: a source page was fetched. Create the image now using only the character identity and "
            "visible traits supported by that fetched source. Write an English, comma-separated prompt with the character "
            "name and several specific source-backed appearance details. Do not replace unknown details with a generic anime character."
        )
    return (
        "Aiso routing state: the previous required tool completed successfully. "
        f"Continue with the next required tool, `{phase.required_tool}`, using valid JSON arguments only. "
        "Do not answer from memory or call any other tool before this result is available."
    )


def route_phase_result_is_usable(
    decision: RouteDecision,
    phase: RoutePhase | None,
    *,
    tool_name: str,
    arguments: dict,
    result: str,
) -> bool:
    """Reject empty/blocked web evidence before a research-image route advances.

    Tool handlers deliberately return explanatory strings for safe network
    blocks and headless-browser failures.  Those strings are useful to the
    model, but are not evidence and must never unlock image generation.
    """
    if decision.name != "research_image" or phase is None or tool_name != phase.required_tool:
        return True

    text = str(result or "").strip()
    if tool_name == "web_search":
        return bool(re.search(r"https?://[^\s\])}>]+", text, re.IGNORECASE))
    if tool_name == "web_fetch":
        url = str(arguments.get("url") or "").strip()
        if not url.startswith(("https://", "http://")):
            return False
        lowered = text.casefold()
        failed_markers = (
            "[blocked]", "[error]", "[fetch failed]", "[fetch unavailable]",
            "[차단]", "[오류]", "[가져오기 실패]", "[가져오기 불가]", "본문 텍스트를 추출하지 못했습니다",
            "could not extract page text",
        )
        return len(text) >= 80 and not any(marker in lowered for marker in failed_markers)
    return True


def route_insufficient_evidence_result(phase: RoutePhase, result: str) -> str:
    """Provide a model-visible failure without treating tool error text as evidence."""
    if phase.required_tool == "web_search":
        reason = "The search result did not contain a usable public URL."
    else:
        reason = "The fetched page did not provide usable character evidence."
    return (
        "[SOURCE_EVIDENCE_INSUFFICIENT] " + reason + " "
        "Do not continue to image generation. Retry the current research phase with a different valid query or URL.\n\n"
        + str(result or "")[:1200]
    )


def route_unavailable_message(decision: RouteDecision, response_language: str) -> str:
    language = str(response_language or "").casefold()
    if language == "ko":
        return (
            f"요청을 처리하는 데 필요한 `{decision.unavailable_tool}` 도구가 현재 설정 또는 실행 조건에서 사용할 수 없습니다. "
            "도구 설정과 작업 폴더·연결 상태를 확인한 뒤 다시 시도해 주세요."
        )
    return (
        f"The `{decision.unavailable_tool}` tool required for this request is unavailable under the current settings or runtime conditions. "
        "Check the tool setting and workspace or connection state, then try again."
    )


def route_required_call_failure_message(
    decision: RouteDecision,
    phase: RoutePhase,
    response_language: str,
) -> str:
    language = str(response_language or "").casefold()
    if decision.name == "research_image":
        if language == "ko":
            return (
                "웹 검색·원문 확인·이미지 생성의 필수 단계가 완료되지 않아, 근거 없는 일반 캐릭터 이미지로 "
                "대체 생성하지 않고 중단했습니다. 웹 도구 연결과 검색어를 확인한 뒤 다시 시도해 주세요."
            )
        return (
            "Aiso stopped because the required research or generation phase did not complete. "
            "It did not substitute a generic character image without source evidence."
        )
    if language == "ko":
        return (
            f"실제 결과를 확인하기 위해 필요한 `{phase.required_tool}` 도구를 호출하지 않아 "
            "추측으로 답변하지 않고 중단했습니다. 다시 시도해도 같은 문제가 반복되면 모델을 바꾸거나 요청을 더 구체적으로 입력해 주세요."
        )
    return (
        f"The required `{phase.required_tool}` tool was not called, so Aiso stopped rather than inventing a result. "
        "If this repeats, try another model or make the request more specific."
    )


__all__ = [
    "GENERAL_ROUTE",
    "RouteDecision",
    "RoutePhase",
    "classify_request",
    "final_response_only_prompt",
    "filter_tool_schemas",
    "route_completion_prompt",
    "route_direct_result_message",
    "route_insufficient_evidence_result",
    "route_next_phase_prompt",
    "route_phase_result_is_usable",
    "route_policy_prompt",
    "route_recovery_prompt",
    "route_required_call_failure_message",
    "route_unavailable_message",
]
