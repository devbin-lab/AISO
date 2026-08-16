"""통합 툴 레지스트리 — 툴의 모든 성질을 한 곳에서 선언한다.

기존엔 툴 하나를 추가하려면 3~5곳(_DISPATCH·TOOL_SCHEMAS·DESTRUCTIVE·DELETE_TOOLS·
ALWAYS_APPROVE·_META_TOOLS·if/elif 디스패치)을 손봐야 했다. 이제 여기 REGISTRY에 한 줄
등록하면 스키마·실행·승인등급·파일변경여부·스크린샷여부가 전부 따라온다.

- `AGENT_TOOLS`: 한국어 UI와 호환되는 원본 스키마 배열(순서 고정).
- `MODEL_AGENT_TOOLS`: 원본을 바꾸지 않은 영문 LLM 요청용 스키마 배열.
- `needs_approval(name, mode)`: 승인 필요 여부.
- `execute(spec, root, host, args) -> (result, shot|None)`: 통일된 실행.
- `is_meta(name)`: update_plan 같은 '실질 작업 아님' 판정.

search_docs는 색인이 있을 때만 조건부로 노출되므로 AGENT_TOOLS에서는 제외하되,
REGISTRY에는 넣어 실행 디스패치에 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

from discordops import APPLY_SCHEMA as DISCORD_APPLY_SCHEMA
from discordops import MAP_SCHEMA as DISCORD_MAP_SCHEMA
from discordops import SEND_SCHEMA as DISCORD_SEND_SCHEMA
from discordops import server_apply, server_map, server_send
from discordsched import (
    CHANNEL_REPORT_ADD_SCHEMA,
    SCHEDULE_ADD_SCHEMA,
    SCHEDULE_LIST_SCHEMA,
    SCHEDULE_REMOVE_SCHEMA,
    channel_report_add,
    schedule_add,
    schedule_list,
    schedule_remove,
)
from document_todos import CREATE_TODO_EVENT_SCHEMA, MANAGE_TODO_SCHEMA, create_todo_event, manage_todo
from mydb_agent import (
    LIST_MYDB_HISTORY_SCHEMA,
    LIST_MYDB_LIBRARY_SCHEMA,
    LIST_MYDB_TRASH_SCHEMA,
    RESTORE_MYDB_TRASH_NODE_SCHEMA,
    list_mydb_history,
    list_mydb_library,
    list_mydb_trash,
    restore_mydb_trash_node,
)
from rag import SEARCH_DOCS_SCHEMA, search_docs_tool
from runcmd import RUN_COMMAND_SCHEMA, run_command
from runcode import RUN_CODE_SCHEMA, run_code
from runskill import CREATE_SKILL_SCHEMA, RUN_SKILL_SCHEMA, create_skill, run_skill
from tool_schema_language import model_schema_for, model_schemas_for
from tools import TOOL_SCHEMAS, list_saved_todos, run_tool
from webcheck import RUN_WEB_SCHEMA, run_web
from webfetch import WEB_FETCH_SCHEMA, web_fetch
from websearch import WEB_SEARCH_SCHEMA, web_search


class Approval(Enum):
    SAFE = 0         # 읽기·조회 — read 모드에선 통과, manual 모드에선 승인
    DESTRUCTIVE = 1  # 쓰기·편집·이동 — read·manual 모드에서 승인
    DELETE = 2       # 삭제 — read·manual 모드에서 승인
    ALWAYS = 3       # 임의 명령 실행 — read·manual 모드에서 승인
    # (DESTRUCTIVE·DELETE·ALWAYS는 현재 승인 로직에선 동일하게 동작하나,
    #  삭제/쓰기/명령이라는 성질 구분을 위해 등급은 유지한다.)


class CallKind(Enum):
    FILE = 0             # 동기, run_tool(root, name, args)
    ASYNC_ROOT = 1       # await handler(root, **args)
    ASYNC_ROOT_HOST = 2  # await handler(root, host, **args)
    ASYNC_PLAIN = 3      # await handler(**args)
    META = 4             # update_plan — 루프에서 직접 처리(여기선 실행 안 함)


Handler = Callable[..., Awaitable[str] | Awaitable[tuple]] | None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict
    kind: CallKind
    approval: Approval = Approval.SAFE
    mutates: bool = False           # 실행 성공 시 파일이 바뀔 수 있음 → 재색인 트리거
    returns_screenshot: bool = False  # handler가 (report, shot) 튜플을 돌려줌
    handler: Handler = None         # FILE/META는 None (run_tool/루프가 처리)


# UPDATE_PLAN 스키마 — 루프가 normalize_plan/render_plan으로 직접 처리(META)
UPDATE_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "다단계 작업의 계획(할 일 목록)을 만들거나 갱신한다. 복잡한 작업을 시작할 때 먼저 "
            "이 툴로 단계를 나누고, 각 단계를 시작할 때 in_progress, 끝내면 completed로 상태를 갱신하라. "
            "전체 steps 배열을 매번 통째로 전달한다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "작업 단계 목록 (순서대로).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "단계 설명"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "진행 상태",
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["steps"],
        },
    },
}

GET_SYSTEM_TIME_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_system_time",
        "description": "작업 폴더나 사용자 파일을 읽지 않고 현재 로컬 날짜와 시각을 확인한다.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


async def get_system_time() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")

# 파일 툴의 성질 — 쓰기/삭제 판정 (테스트가 승인 매트릭스로 고정)
_FILE_MUTATES = {
    "write_file", "edit_file", "multi_edit",
    "convert_document", "analyze_document_calendar",
    "write_code_file", "edit_code_file", "multi_edit_code_file",
    "delete_file", "delete_dir", "move",
}
_FILE_DELETE = {"delete_file", "delete_dir"}


def _file_approval(name: str) -> Approval:
    if name in _FILE_DELETE:
        return Approval.DELETE
    if name in _FILE_MUTATES:
        return Approval.DESTRUCTIVE
    return Approval.SAFE


def _build_registry() -> dict[str, ToolSpec]:
    reg: dict[str, ToolSpec] = {}
    # 1) update_plan (메타, 맨 앞) — AGENT_TOOLS 순서 보존
    reg["update_plan"] = ToolSpec("update_plan", UPDATE_PLAN_SCHEMA, CallKind.META)
    # Gate 5의 외부 provider 안전 왕복에 사용할 수 있는 합성 read-only 도구.
    reg["get_system_time"] = ToolSpec(
        "get_system_time", GET_SYSTEM_TIME_SCHEMA, CallKind.ASYNC_PLAIN, handler=get_system_time
    )
    # Saved document ToDos are indexed by Aiso's private app data. They remain
    # available after a workspace-specific agent session is closed.
    calendar_schema = next(schema for schema in TOOL_SCHEMAS if schema["function"]["name"] == "list_calendar_events")
    reg["list_calendar_events"] = ToolSpec(
        "list_calendar_events", calendar_schema, CallKind.ASYNC_PLAIN, handler=list_saved_todos
    )
    # Personal calendar entries are stored in Aiso's central ToDo database,
    # not in a workspace and never in Discord.  Keep this conditional so the
    # frozen base schema prefix stays stable while Settings can still govern it.
    reg["create_calendar_event"] = ToolSpec(
        "create_calendar_event", CREATE_TODO_EVENT_SCHEMA, CallKind.ASYNC_PLAIN,
        approval=Approval.DESTRUCTIVE, mutates=True, handler=create_todo_event,
    )
    reg["manage_calendar_event"] = ToolSpec(
        "manage_calendar_event", MANAGE_TODO_SCHEMA, CallKind.ASYNC_PLAIN,
        approval=Approval.DESTRUCTIVE, mutates=True, handler=manage_todo,
    )
    # My DB is a separate personal library.  The Agent receives metadata only;
    # it may inspect library/history/trash and restore a trashed node, but it
    # is deliberately never given create/edit/delete/link/export access.
    reg["list_mydb_library"] = ToolSpec(
        "list_mydb_library", LIST_MYDB_LIBRARY_SCHEMA, CallKind.ASYNC_PLAIN, handler=list_mydb_library,
    )
    reg["list_mydb_history"] = ToolSpec(
        "list_mydb_history", LIST_MYDB_HISTORY_SCHEMA, CallKind.ASYNC_PLAIN, handler=list_mydb_history,
    )
    reg["list_mydb_trash"] = ToolSpec(
        "list_mydb_trash", LIST_MYDB_TRASH_SCHEMA, CallKind.ASYNC_PLAIN, handler=list_mydb_trash,
    )
    reg["restore_mydb_trash_node"] = ToolSpec(
        "restore_mydb_trash_node", RESTORE_MYDB_TRASH_NODE_SCHEMA, CallKind.ASYNC_PLAIN,
        approval=Approval.DESTRUCTIVE, handler=restore_mydb_trash_node,
    )
    # 2) 파일 툴 12개 — TOOL_SCHEMAS 순서 그대로
    for sch in TOOL_SCHEMAS:
        name = sch["function"]["name"]
        if name == "list_calendar_events":
            continue
        reg[name] = ToolSpec(
            name, sch, CallKind.FILE,
            approval=_file_approval(name),
            mutates=name in _FILE_MUTATES,
        )
    # 3) 비동기 툴 — 기존 AGENT_TOOLS 순서(run_web, run_code, run_command, web_fetch)
    # Existing workspace code/HTML is not trusted merely because it is already on disk.
    # Read/manual modes retain their approval boundary; auto mode is the user's explicit
    # opt-in to run every exposed tool without an approval card.
    reg["run_web"] = ToolSpec("run_web", RUN_WEB_SCHEMA, CallKind.ASYNC_ROOT,
                              approval=Approval.ALWAYS, returns_screenshot=True, handler=run_web)
    reg["run_code"] = ToolSpec("run_code", RUN_CODE_SCHEMA, CallKind.ASYNC_ROOT,
                               approval=Approval.ALWAYS, handler=run_code)
    reg["run_command"] = ToolSpec("run_command", RUN_COMMAND_SCHEMA, CallKind.ASYNC_ROOT,
                                  approval=Approval.ALWAYS, mutates=True, handler=run_command)
    reg["web_fetch"] = ToolSpec("web_fetch", WEB_FETCH_SCHEMA, CallKind.ASYNC_PLAIN, handler=web_fetch)
    reg["web_search"] = ToolSpec("web_search", WEB_SEARCH_SCHEMA, CallKind.ASYNC_PLAIN, handler=web_search)
    # 3b) 스킬 — create_skill(재사용 자동화 저장, 쓰기 등급) / run_skill(임의 실행, 명령 등급)
    #     스킬은 workspace 밖(앱 skills 폴더)에 저장되므로 mutates=False(재색인 불필요).
    # Skills persist executable code outside the selected workspace. Treat creation
    # and execution alike in read/manual modes; auto is the user's explicit
    # full-autonomy choice for enabled tools.
    reg["create_skill"] = ToolSpec("create_skill", CREATE_SKILL_SCHEMA, CallKind.ASYNC_PLAIN,
                                   approval=Approval.ALWAYS, handler=create_skill)
    reg["run_skill"] = ToolSpec("run_skill", RUN_SKILL_SCHEMA, CallKind.ASYNC_PLAIN,
                                approval=Approval.ALWAYS, handler=run_skill)
    # 4) search_docs — 색인 있을 때만 노출(AGENT_TOOLS 제외)이지만 실행 디스패치엔 필요
    reg["search_docs"] = ToolSpec("search_docs", SEARCH_DOCS_SCHEMA, CallKind.ASYNC_ROOT_HOST,
                                  handler=search_docs_tool)
    # 5) 디스코드 서버 구성 — 봇이 연결돼 있을 때만 조건부 노출(search_docs와 동일 패턴, AGENT_TOOLS 제외).
    #    apply는 외부 공유 서버를 즉시 바꾸고 삭제는 복구 불가 → DELETE 등급.
    #    자동 모드는 사용자의 명시적 무승인 실행 선택을 따른다.
    reg["discord_server_map"] = ToolSpec("discord_server_map", DISCORD_MAP_SCHEMA,
                                         CallKind.ASYNC_PLAIN, handler=server_map)
    reg["discord_server_apply"] = ToolSpec("discord_server_apply", DISCORD_APPLY_SCHEMA,
                                           CallKind.ASYNC_PLAIN, approval=Approval.DELETE,
                                           handler=server_apply)
    # 메시지 전송·예약 — 외부(공유 서버)로 발신되므로 쓰기 등급이다.
    # 자동 모드는 사용자의 명시적 무승인 실행 선택을 따른다.
    reg["discord_send"] = ToolSpec("discord_send", DISCORD_SEND_SCHEMA,
                                   CallKind.ASYNC_PLAIN, approval=Approval.DESTRUCTIVE,
                                   handler=server_send)
    reg["discord_schedule_add"] = ToolSpec("discord_schedule_add", SCHEDULE_ADD_SCHEMA,
                                           CallKind.ASYNC_PLAIN, approval=Approval.DESTRUCTIVE,
                                           handler=schedule_add)
    reg["discord_channel_report_add"] = ToolSpec(
        "discord_channel_report_add", CHANNEL_REPORT_ADD_SCHEMA,
        CallKind.ASYNC_PLAIN, approval=Approval.DESTRUCTIVE,
        handler=channel_report_add,
    )
    reg["discord_schedule_list"] = ToolSpec("discord_schedule_list", SCHEDULE_LIST_SCHEMA,
                                            CallKind.ASYNC_PLAIN, handler=schedule_list)
    reg["discord_schedule_remove"] = ToolSpec("discord_schedule_remove", SCHEDULE_REMOVE_SCHEMA,
                                              CallKind.ASYNC_PLAIN, approval=Approval.DESTRUCTIVE,
                                              handler=schedule_remove)
    return reg


REGISTRY: dict[str, ToolSpec] = _build_registry()

PROGRAMMING_TOOLS = frozenset({
    "write_code_file", "edit_code_file", "multi_edit_code_file",
    "run_web", "run_code", "run_command",
})
BUILTIN_TOOL_NAMES = tuple([*REGISTRY, "generate_image"])
DEFAULT_ENABLED_TOOLS = tuple(name for name in BUILTIN_TOOL_NAMES if name not in PROGRAMMING_TOOLS)
NVIDIA_AGENT_SUPPORTED_TOOLS = frozenset({
    "update_plan", "get_system_time", "list_calendar_events", "create_calendar_event", "manage_calendar_event",
    "list_mydb_library", "list_mydb_history", "list_mydb_trash", "restore_mydb_trash_node",
    "list_dir", "list_tree", "read_file", "grep", "glob", "create_dir", "move", "convert_document", "analyze_document_calendar",
    "write_file", "edit_file", "multi_edit",
    "write_code_file", "edit_code_file", "multi_edit_code_file",
    "delete_file", "delete_dir", "run_web", "run_code", "run_command",
    "search_docs", "generate_image",
})


def normalize_enabled_tool_names(names: list[str] | tuple[str, ...] | None) -> frozenset[str]:
    """저장 정책의 명시적 허용 목록을 카탈로그 순서와 무관한 실행 집합으로 검증한다."""
    from tools import ToolError

    aliases = {
        "list_saved_todos": "list_calendar_events",
        "create_todo_event": "create_calendar_event",
        "manage_todo": "manage_calendar_event",
        "analyze_document_todos": "analyze_document_calendar",
    }
    values = [aliases.get(name, name) for name in (DEFAULT_ENABLED_TOOLS if names is None else names)]
    unknown = [name for name in values if not isinstance(name, str) or name not in BUILTIN_TOOL_NAMES]
    if unknown:
        raise ToolError(f"지원하지 않는 Agent 도구입니다: {unknown[0]}")
    if len(values) > len(BUILTIN_TOOL_NAMES) or len(values) != len(set(values)):
        raise ToolError("Agent 활성 도구 목록이 중복되었거나 허용 크기를 넘었습니다.")
    return frozenset(values)

# 조건부 노출 툴 — 상황이 갖춰졌을 때만 agent 루프가 tools에 얹는다(KV 프리픽스 스냅샷 불변).
_CONDITIONAL_TOOLS = {
    "search_docs", "create_calendar_event", "manage_calendar_event",
    "discord_server_map", "discord_server_apply", "discord_send",
    "discord_schedule_add", "discord_channel_report_add",
    "discord_schedule_list", "discord_schedule_remove",
}

# Korean source schema array — conditional tools are omitted and registry order
# stays frozen for Settings/catalog compatibility.
AGENT_TOOLS: list[dict] = [spec.schema for name, spec in REGISTRY.items() if name not in _CONDITIONAL_TOOLS]

# The Korean source schemas above are also rendered in Settings > Tools.  Keep
# that UI contract untouched and expose a separate English deep-copy for LLM
# requests.  Model callers must use this collection (or ``model_tool_schemas``
# for conditional tools), never mutate ``ToolSpec.schema`` in place.
MODEL_AGENT_TOOLS: list[dict] = model_schemas_for(AGENT_TOOLS)


def model_tool_schemas(names: tuple[str, ...] | list[str] | frozenset[str]) -> list[dict]:
    """Return English LLM schemas for registered tools in the requested order.

    This supports research and Discord's conditional tool sets without leaking
    English-only schema descriptions back into the Korean Settings catalog.
    Unknown names are skipped deliberately: the caller's execution boundary
    remains responsible for rejecting unknown tool calls.
    """
    return [model_schema_for(REGISTRY[name].schema) for name in names if name in REGISTRY]

# 호환용 이름이다. 자동 모드는 모든 노출 도구를 승인 없이 실행하므로 강제 예외는 없다.
FORCE_APPROVAL_IN_AUTO = frozenset()


def is_meta(name: str) -> bool:
    spec = REGISTRY.get(name)
    return spec is not None and spec.kind is CallKind.META


def needs_approval(name: str, mode: str) -> bool:
    """승인 모드별로 이 툴이 사용자 승인을 요구하는지.

    - auto(자동):   사용자가 노출·허용한 모든 도구를 승인 없이 실행.
    - read(읽기):   읽기(SAFE)는 통과, 쓰기·편집·삭제·명령은 승인.
    - manual(수동): 읽기 포함 모든 실질 행위를 승인(계획 갱신 같은 메타는 제외).
    """
    # generate_image는 런타임 조건부 도구라 REGISTRY에는 없지만 ComfyUI output을
    # 새로 만든다. 읽기 모드에서는 쓰기 작업처럼 승인하고 자동 모드만 바로 실행한다.
    if name == "generate_image":
        return mode in ("manual", "read")
    spec = REGISTRY.get(name)
    if mode == "auto":
        # Auto is the explicit full-autonomy mode. The renderer must never receive
        # an approval_request for a tool that the user deliberately enabled.
        return False
    if spec is None:
        return mode == "manual"  # 미등록 툴 → 수동에서만 승인(그 외 기존처럼 통과)
    if spec.kind is CallKind.META:
        return False  # update_plan 등 메타는 실제 행위가 아니므로 승인 불필요
    if mode == "manual":
        return True  # 수동: 읽기까지 전부 승인
    return spec.approval is not Approval.SAFE  # read: 읽기 제외 전부 승인


def _catalog_classification(name: str, spec: ToolSpec) -> tuple[str, str, tuple[str, ...]]:
    """툴 목록 UI에 필요한 분류·노출 조건을 실제 실행 경계에 맞춰 만든다."""
    if spec.kind is CallKind.META:
        return "plan", "always", ()
    if name == "analyze_document_calendar":
        return "plan", "workspace", ("작업 폴더 선택",)
    if name in {"create_calendar_event", "manage_calendar_event"}:
        return "plan", "always", ("Aiso ToDo 중앙 저장소",)
    if name in {"list_mydb_library", "list_mydb_history", "list_mydb_trash", "restore_mydb_trash_node"}:
        return "mydb", "always", ("My DB 저장소",)
    if name == "search_docs":
        return "rag", "rag", ("작업 폴더 선택", "RAG 사용", "색인 완료")
    if name.startswith("discord_"):
        return "discord", "discord", ("디스코드 봇 연결",)
    if name in PROGRAMMING_TOOLS:
        return "programming", "workspace", ("작업 폴더 선택", "설정에서 프로그래밍 도구 사용",)
    if spec.kind is CallKind.FILE:
        category = "files"
        return category, "workspace", ("작업 폴더 선택",)
    if name in {"web_search", "web_fetch"}:
        return "research", "always", ()
    if name in {"create_skill", "run_skill"}:
        return "automation", "always", ()
    return "automation", "always", ()


def _catalog_entry(
    *,
    name: str,
    schema: dict,
    category: str,
    availability: str,
    requirements: tuple[str, ...],
    mutates: bool,
) -> dict[str, Any]:
    """Ollama 함수 스키마를 안전한 읽기 전용 툴 카탈로그 항목으로 축약한다."""
    function = schema.get("function") if isinstance(schema, dict) else None
    function = function if isinstance(function, dict) else {}
    raw_parameters = function.get("parameters")
    properties = raw_parameters.get("properties", {}) if isinstance(raw_parameters, dict) else {}
    parameters = [
        {
            "name": str(param_name),
            "description": str(param.get("description") or ""),
        }
        for param_name, param in properties.items()
        if isinstance(param, dict)
    ] if isinstance(properties, dict) else []
    approval = {mode: needs_approval(name, mode) for mode in ("manual", "read", "auto")}
    return {
        "name": name,
        "description": str(function.get("description") or ""),
        "category": category,
        "parameters": parameters,
        "mutates": mutates,
        "approval": approval,
        "availability": availability,
        "requirements": list(requirements),
    }


def get_builtin_tool_catalog() -> list[dict[str, Any]]:
    """현재 빌드에 내장된 Agent 툴 목록을 반환한다.

    이 함수는 ``REGISTRY``를 그대로 순회하므로 툴 추가·승인 등급 변경이 설정 UI와
    어긋나지 않는다. 사용자 스킬은 런타임에 달라지는 별도 확장 기능이라 포함하지 않는다.
    """
    catalog: list[dict[str, Any]] = []
    for name, spec in REGISTRY.items():
        category, availability, requirements = _catalog_classification(name, spec)
        catalog.append(
            _catalog_entry(
                name=name,
                schema=spec.schema,
                category=category,
                availability=availability,
                requirements=requirements,
                mutates=spec.mutates,
            )
        )

    # 이미지는 REGISTRY가 아닌 Agent 런타임에서 요청 의도와 ComfyUI 준비 상태에 따라
    # 동적으로 추가된다. 기본 도구 목록에는 포함하되, 항상 사용할 수 있는 도구처럼 보이지
    # 않도록 별도 조건을 명시한다.
    from comfy_generation import GENERATE_IMAGE_SCHEMA

    catalog.append(
        _catalog_entry(
            name="generate_image",
            schema=GENERATE_IMAGE_SCHEMA,
            category="image",
            availability="image",
            requirements=("명시적 이미지 생성 요청", "ComfyUI 연결", "등록 모델 준비"),
            mutates=True,
        )
    )
    return catalog


async def execute(spec: ToolSpec, root: Path, host: str, args: dict) -> tuple[str, str | None]:
    """툴을 성질(kind)에 맞게 실행하고 (결과문자열, 스크린샷|None)을 돌려준다."""
    if spec.kind is CallKind.FILE:
        return run_tool(root, spec.name, args), None
    if spec.kind is CallKind.ASYNC_ROOT:
        res = await spec.handler(root, **args)  # type: ignore[misc]
        if spec.returns_screenshot:
            report, shot = res
            return report, shot
        return res, None
    if spec.kind is CallKind.ASYNC_ROOT_HOST:
        return await spec.handler(root, host, **args), None  # type: ignore[misc]
    if spec.kind is CallKind.ASYNC_PLAIN:
        return await spec.handler(**args), None  # type: ignore[misc]
    # META는 루프에서 처리하므로 여기 오면 안 됨
    from tools import ToolError
    raise ToolError(f"실행할 수 없는 메타 툴: {spec.name}")
