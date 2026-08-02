"""통합 툴 레지스트리 — 툴의 모든 성질을 한 곳에서 선언한다.

기존엔 툴 하나를 추가하려면 3~5곳(_DISPATCH·TOOL_SCHEMAS·DESTRUCTIVE·DELETE_TOOLS·
ALWAYS_APPROVE·_META_TOOLS·if/elif 디스패치)을 손봐야 했다. 이제 여기 REGISTRY에 한 줄
등록하면 스키마·실행·승인등급·파일변경여부·스크린샷여부가 전부 따라온다.

- `AGENT_TOOLS`: 모델에게 넘길 스키마 배열(순서 = KV 캐시 프리픽스에 결정적, 고정).
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
    SCHEDULE_ADD_SCHEMA,
    SCHEDULE_LIST_SCHEMA,
    SCHEDULE_REMOVE_SCHEMA,
    schedule_add,
    schedule_list,
    schedule_remove,
)
from rag import SEARCH_DOCS_SCHEMA, search_docs_tool
from runcmd import RUN_COMMAND_SCHEMA, run_command
from runcode import RUN_CODE_SCHEMA, run_code
from runskill import CREATE_SKILL_SCHEMA, RUN_SKILL_SCHEMA, create_skill, run_skill
from tools import TOOL_SCHEMAS, run_tool
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
_FILE_MUTATES = {"write_file", "edit_file", "multi_edit", "delete_file", "delete_dir", "move"}
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
    # 2) 파일 툴 12개 — TOOL_SCHEMAS 순서 그대로
    for sch in TOOL_SCHEMAS:
        name = sch["function"]["name"]
        reg[name] = ToolSpec(
            name, sch, CallKind.FILE,
            approval=_file_approval(name),
            mutates=name in _FILE_MUTATES,
        )
    # 3) 비동기 툴 — 기존 AGENT_TOOLS 순서(run_web, run_code, run_command, web_fetch)
    # Existing workspace code/HTML is not trusted merely because it is already on disk.
    # These tools execute it with the user's browser/process permissions, so neither a
    # read-only session nor auto mode may bypass the approval boundary.
    reg["run_web"] = ToolSpec("run_web", RUN_WEB_SCHEMA, CallKind.ASYNC_ROOT,
                              approval=Approval.ALWAYS, returns_screenshot=True, handler=run_web)
    reg["run_code"] = ToolSpec("run_code", RUN_CODE_SCHEMA, CallKind.ASYNC_ROOT,
                               approval=Approval.ALWAYS, handler=run_code)
    reg["run_command"] = ToolSpec("run_command", RUN_COMMAND_SCHEMA, CallKind.ASYNC_ROOT,
                                  approval=Approval.ALWAYS, mutates=True, handler=run_command)
    reg["web_fetch"] = ToolSpec("web_fetch", WEB_FETCH_SCHEMA, CallKind.ASYNC_PLAIN, handler=web_fetch)
    reg["web_search"] = ToolSpec("web_search", WEB_SEARCH_SCHEMA, CallKind.ASYNC_PLAIN, handler=web_search)
    # 3b) 스킬 — create_skill(코드 산출의 유일한 경로, 쓰기 등급) / run_skill(임의 실행, 명령 등급)
    #     스킬은 workspace 밖(앱 skills 폴더)에 저장되므로 mutates=False(재색인 불필요).
    # Skills persist executable code outside the selected workspace.  Treat creation
    # and execution alike so a prompt-injected repository cannot plant a future
    # executable in an unattended auto run.
    reg["create_skill"] = ToolSpec("create_skill", CREATE_SKILL_SCHEMA, CallKind.ASYNC_PLAIN,
                                   approval=Approval.ALWAYS, handler=create_skill)
    reg["run_skill"] = ToolSpec("run_skill", RUN_SKILL_SCHEMA, CallKind.ASYNC_PLAIN,
                                approval=Approval.ALWAYS, handler=run_skill)
    # 4) search_docs — 색인 있을 때만 노출(AGENT_TOOLS 제외)이지만 실행 디스패치엔 필요
    reg["search_docs"] = ToolSpec("search_docs", SEARCH_DOCS_SCHEMA, CallKind.ASYNC_ROOT_HOST,
                                  handler=search_docs_tool)
    # 5) 디스코드 서버 구성 — 봇이 연결돼 있을 때만 조건부 노출(search_docs와 동일 패턴, AGENT_TOOLS 제외).
    #    apply는 외부 공유 서버를 즉시 바꾸고 삭제는 복구 불가 → DELETE 등급
    #    (+ agent 루프가 자동(auto) 모드에서도 승인을 강제한다).
    reg["discord_server_map"] = ToolSpec("discord_server_map", DISCORD_MAP_SCHEMA,
                                         CallKind.ASYNC_PLAIN, handler=server_map)
    reg["discord_server_apply"] = ToolSpec("discord_server_apply", DISCORD_APPLY_SCHEMA,
                                           CallKind.ASYNC_PLAIN, approval=Approval.DELETE,
                                           handler=server_apply)
    # 메시지 전송·예약 — 외부(공유 서버)로 발신되므로 쓰기 등급(send·add는 agent 루프가 auto에서도 승인 강제).
    reg["discord_send"] = ToolSpec("discord_send", DISCORD_SEND_SCHEMA,
                                   CallKind.ASYNC_PLAIN, approval=Approval.DESTRUCTIVE,
                                   handler=server_send)
    reg["discord_schedule_add"] = ToolSpec("discord_schedule_add", SCHEDULE_ADD_SCHEMA,
                                           CallKind.ASYNC_PLAIN, approval=Approval.DESTRUCTIVE,
                                           handler=schedule_add)
    reg["discord_schedule_list"] = ToolSpec("discord_schedule_list", SCHEDULE_LIST_SCHEMA,
                                            CallKind.ASYNC_PLAIN, handler=schedule_list)
    reg["discord_schedule_remove"] = ToolSpec("discord_schedule_remove", SCHEDULE_REMOVE_SCHEMA,
                                              CallKind.ASYNC_PLAIN, approval=Approval.DESTRUCTIVE,
                                              handler=schedule_remove)
    return reg


REGISTRY: dict[str, ToolSpec] = _build_registry()

# 조건부 노출 툴 — 상황이 갖춰졌을 때만 agent 루프가 tools에 얹는다(KV 프리픽스 스냅샷 불변).
_CONDITIONAL_TOOLS = {
    "search_docs",
    "discord_server_map", "discord_server_apply", "discord_send",
    "discord_schedule_add", "discord_schedule_list", "discord_schedule_remove",
}

# 모델에게 넘길 스키마 배열 — 조건부 툴 제외, 등록 순서 유지 (기존 AGENT_TOOLS와 바이트 동일)
AGENT_TOOLS: list[dict] = [spec.schema for name, spec in REGISTRY.items() if name not in _CONDITIONAL_TOOLS]

# 외부 공유 서버에 영향을 주는 행위는 auto 모드여도 승인한다. Agent 실행 루프와 툴 목록이
# 같은 정책을 보도록 여기에서 단일 상수로 관리한다.
FORCE_APPROVAL_IN_AUTO = frozenset({
    "discord_server_apply", "discord_send", "discord_schedule_add",
})


def is_meta(name: str) -> bool:
    spec = REGISTRY.get(name)
    return spec is not None and spec.kind is CallKind.META


def needs_approval(name: str, mode: str) -> bool:
    """승인 모드별로 이 툴이 사용자 승인을 요구하는지.

    - auto(자동):   SAFE와 되돌릴 수 있는 작업만 무승인 실행. 코드·명령·브라우저
                    실행과 삭제는 항상 승인.
    - read(읽기):   읽기(SAFE)는 통과, 쓰기·편집·삭제·명령은 승인.
    - manual(수동): 읽기 포함 모든 실질 행위를 승인(계획 갱신 같은 메타는 제외).
    """
    # generate_image는 런타임 조건부 도구라 REGISTRY에는 없지만 ComfyUI output을
    # 새로 만든다. 읽기 모드에서는 쓰기 작업처럼 승인하고 자동 모드만 바로 실행한다.
    if name == "generate_image":
        return mode in ("manual", "read")
    spec = REGISTRY.get(name)
    if mode == "auto":
        # Auto is deliberately not a blanket bypass. Shell/code/browser execution,
        # skill execution/creation, and deletion retain an explicit user gate.
        return spec is not None and spec.approval in (Approval.ALWAYS, Approval.DELETE)
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
    if name == "search_docs":
        return "rag", "rag", ("작업 폴더 선택", "RAG 사용", "색인 완료")
    if name.startswith("discord_"):
        return "discord", "discord", ("디스코드 봇 연결",)
    if spec.kind is CallKind.FILE or name in {"run_web", "run_code", "run_command"}:
        category = "files" if spec.kind is CallKind.FILE else "execution"
        return category, "workspace", ("작업 폴더 선택",)
    if name in {"web_search", "web_fetch"}:
        return "research", "always", (
            "작업 폴더 내용을 읽은 뒤 웹으로 전송할 때는 자동 모드도 승인 필요",
        )
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
    approval = {
        mode: needs_approval(name, mode) or (mode == "auto" and name in FORCE_APPROVAL_IN_AUTO)
        for mode in ("manual", "read", "auto")
    }
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
