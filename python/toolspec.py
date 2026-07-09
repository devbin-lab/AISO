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
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

from rag import SEARCH_DOCS_SCHEMA, search_docs_tool
from runcmd import RUN_COMMAND_SCHEMA, run_command
from runcode import RUN_CODE_SCHEMA, run_code
from tools import TOOL_SCHEMAS, run_tool
from webcheck import RUN_WEB_SCHEMA, run_web
from webfetch import WEB_FETCH_SCHEMA, web_fetch


class Approval(Enum):
    SAFE = 0         # 승인 불필요 (읽기·조회)
    DESTRUCTIVE = 1  # require 모드에서 승인 (쓰기·편집·이동)
    DELETE = 2       # require·partial 모드에서 승인 (삭제)
    ALWAYS = 3       # auto 외 모든 모드에서 승인 (임의 명령 실행)


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

# 파일 툴의 성질 (tools.py의 DESTRUCTIVE와 일치해야 함; 테스트가 매트릭스로 고정)
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
    # 2) 파일 툴 12개 — TOOL_SCHEMAS 순서 그대로
    for sch in TOOL_SCHEMAS:
        name = sch["function"]["name"]
        reg[name] = ToolSpec(
            name, sch, CallKind.FILE,
            approval=_file_approval(name),
            mutates=name in _FILE_MUTATES,
        )
    # 3) 비동기 툴 — 기존 AGENT_TOOLS 순서(run_web, run_code, run_command, web_fetch)
    reg["run_web"] = ToolSpec("run_web", RUN_WEB_SCHEMA, CallKind.ASYNC_ROOT,
                              returns_screenshot=True, handler=run_web)
    reg["run_code"] = ToolSpec("run_code", RUN_CODE_SCHEMA, CallKind.ASYNC_ROOT, handler=run_code)
    reg["run_command"] = ToolSpec("run_command", RUN_COMMAND_SCHEMA, CallKind.ASYNC_ROOT,
                                  approval=Approval.ALWAYS, mutates=True, handler=run_command)
    reg["web_fetch"] = ToolSpec("web_fetch", WEB_FETCH_SCHEMA, CallKind.ASYNC_PLAIN, handler=web_fetch)
    # 4) search_docs — 색인 있을 때만 노출(AGENT_TOOLS 제외)이지만 실행 디스패치엔 필요
    reg["search_docs"] = ToolSpec("search_docs", SEARCH_DOCS_SCHEMA, CallKind.ASYNC_ROOT_HOST,
                                  handler=search_docs_tool)
    return reg


REGISTRY: dict[str, ToolSpec] = _build_registry()

# 모델에게 넘길 스키마 배열 — search_docs 제외, 등록 순서 유지 (기존 AGENT_TOOLS와 바이트 동일)
AGENT_TOOLS: list[dict] = [spec.schema for name, spec in REGISTRY.items() if name != "search_docs"]


def is_meta(name: str) -> bool:
    spec = REGISTRY.get(name)
    return spec is not None and spec.kind is CallKind.META


def needs_approval(name: str, mode: str) -> bool:
    """승인 모드별로 이 툴이 사용자 승인을 요구하는지."""
    spec = REGISTRY.get(name)
    approval = spec.approval if spec else Approval.SAFE  # 미등록 툴 → 승인 불필요(기존 동작)
    if mode == "auto":
        return False
    if approval is Approval.ALWAYS:
        return True
    if mode == "partial":
        return approval is Approval.DELETE
    return approval is not Approval.SAFE  # require: 파괴/삭제 모두 승인


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
