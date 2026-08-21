"""에이전트 하네스 — 로컬 LLM(gemma4·gpt-oss 등) 툴 콜링으로 로컬 파일을 다루는 반복 루프.

생성 → 툴콜 → (필요 시 승인) → 실행 → 결과 피드백 → 반복.
이벤트를 dict로 yield 하며, 승인 여부는 사용자가 선택한 권한 모드가 결정한다.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from pathlib import Path
from typing import Any, AsyncGenerator

import discordops  # 서버 구성·전송(디스코드) — 모듈 자체는 discord 미의존(지연 import)
import discordsched  # 예약(디스코드) — 순수 파이썬
import agent_validation as validation
import agent_prompting as prompting
import agent_research as research
import agent_execution as execution
import agent_runner as runner
import agent_routing as routing
from agent_approval import ApprovalRegistry
from response_language import normalize_response_language, response_language_from_messages
from comfy_generation import (
    GENERATE_IMAGE_SCHEMA,
    GenerationError,
    generate_image,
    result_to_tool_text,
)
from comfy_workflows import MAX_PROMPT_LENGTH

from llm import LlmFailureKind, LlmModelRuntime, LlmProviderError, LlmRequest, LlmRuntime, create_runtime
from llm.tool_calls import ToolCallAssembler, ToolCallProtocolError, canonicalize_tool_arguments
from agent_ledger import (
    AgentExecutionLedger,
    LedgerError,
    LedgerIndeterminate,
    LedgerInProgress,
    LedgerKey,
    LedgerProtocolConflict,
)
from rag import (
    SEARCH_DOCS_SCHEMA,
    build_index,
    format_context,
    search as rag_search,
    status as rag_status,
)
from runskill import list_skills, run_skill
from toolspec import (
    AGENT_TOOLS,
    MODEL_AGENT_TOOLS,
    REGISTRY,
    execute,
    is_meta,
    needs_approval,
    normalize_enabled_tool_names,
    model_schema_for,
    model_schemas_for,
)
from tools import (
    MAX_CODE_FILE_BYTES,
    MAX_HTML_SCAN_SECONDS,
    ToolError,
    find_html_entries,
    run_tool,
    validate_workspace,
)

# 대량 작업(수십~수백 파일 정리 등)도 끝까지 돌 수 있게 상한을 높게 둔다.
# 이건 '정상 작업 제한'이 아니라 병리적 폭주를 막는 최후의 안전선일 뿐이고,
# 진짜 무한 루프는 아래 STALL_REPEAT(동일 동작 반복) 감지로 막는다.
MAX_STEPS = 300
STALL_REPEAT = 6  # 완전히 동일한 (툴,인자) 호출이 연속 이 횟수를 넘으면 정체로 보고 중단
# 연속 동일만 보면 A→B→A→B… 교대 루프를 영원히 못 잡는다(서명이 매번 달라져 카운터가
# 리셋된다). 작은 로컬 모델의 가장 흔한 퇴행이 이 모양이라, 최근 호출 창의 '동작 다양성'도
# 함께 본다. 실측: 교대 루프가 총량 상한에 걸릴 때까지 85회, 3중 회전은 95회 돌았다.
STALL_WINDOW = 9  # 최근 실질(비-meta) 도구 호출 이만큼을 본다
STALL_WINDOW_MIN_DISTINCT = 4  # 그 창의 서로 다른 동작이 이보다 적으면 정체로 본다
#   2중 교대 → 2가지, 3중 회전 → 3가지 (둘 다 걸린다)
#   수정-확인 반복 → 내용이 매번 달라 6가지 이상 (안 걸린다)
SUBSTANTIVE_TOOL_CALL_LIMIT = 128  # 서로 다른 호출로 위장한 폭주까지 막는 런 전체 상한
IDENTICAL_TOOL_BATCH_LIMIT = 2  # 같은 실질 도구 묶음은 두 번까지만 허용
MUTATION_TARGET_ATTEMPT_LIMIT = 16  # 한 경로를 반복 수정하는 교대 루프 상한
MAX_NUDGES = 3    # 툴 없이 멈추려 할 때 '이어서 진행하라'고 찌를 최대 연속 횟수
SPIN_LIMIT = 4    # 실질 진전(update_plan 외 툴 실행) 없는 턴이 연속 이 횟수면 정체로 보고 중단
APPROVAL_TIMEOUT = 600  # 파괴적 툴 승인 대기 상한(초)
# 한 턴 생성 토큰 상한(num_predict). num_ctx(컨텍스트 창)와 분리한다 — 안 그러면 컨텍스트를
# 크게 잡을수록 한 턴이 폭주(반복 퇴행)로 수만 토큰을 쏟아내 무한루프처럼 보인다(16k→64k 사례).
MAX_GEN_TOKENS = 8192

# 생성 루프 상수는 agent_execution이 소유한다. 여기서 값을 다시 적으면 실제 동작과
# 조용히 갈라지므로(고쳐도 아무 변화가 없다) 재수출만 한다 — _looks_degenerate와 같은 패턴.
MAX_PARSE_RETRIES = execution.MAX_PARSE_RETRIES  # 툴콜 파싱 실패 시 재생성 최대 횟수
REP_MIN_LEN = execution.REP_MIN_LEN              # 이 길이 넘을 때부터 반복 퇴행 감지 시작(자)
REP_CHECK_EVERY = execution.REP_CHECK_EVERY      # 이후 이 간격마다 재검사(자)


_looks_degenerate = execution._looks_degenerate

# AGENT_TOOLS·needs_approval·툴 실행은 toolspec 레지스트리에서 온다 (import 참고).
# 스키마 정의·분류·디스패치가 흩어져 있던 것을 한 곳으로 모았다.

_STATUS_WORDS = {"pending", "in_progress", "completed", "not_started", "todo", "done", "doing"}


def _norm_status(raw: Any) -> str:
    """모델마다 제각각인 상태 문자열을 3가지로 정규화."""
    s = str(raw or "").lower().replace("-", "_").replace(" ", "_")
    if s in ("completed", "complete", "done", "finished", "closed", "resolved"):
        return "completed"
    if s in ("in_progress", "inprogress", "doing", "active", "started", "current", "wip", "running"):
        return "in_progress"
    return "pending"  # not_started, todo, pending, 등


def _step_text(s: dict) -> str:
    """단계 텍스트를 여러 키 후보에서 찾는다 (모델이 content 대신 name/task 등을 쓰기 때문)."""
    for k in ("content", "name", "task", "title", "step", "description", "text", "label", "todo"):
        v = s.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # 그래도 없으면: 상태값이 아닌 첫 문자열
    for v in s.values():
        if isinstance(v, str) and v.strip() and v.strip().lower() not in _STATUS_WORDS:
            return v.strip()
    return "(단계)"


def normalize_plan(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    return [
        {"content": _step_text(s), "status": _norm_status(s.get("status"))}
        for s in raw
        if isinstance(s, dict)
    ]


def render_plan(plan: list[dict]) -> str:
    """현재 계획을 시스템 메시지에 끼워넣을 텍스트로 렌더링한다 (항상 컨텍스트에 유지)."""
    if not plan:
        return ""
    # plan 은 모델이 준 값이 그대로 실린 dict 라 status 키가 없을 수도 있다
    # (그때 s.get("status") 는 None). 미지의 키·None 은 기본값 "[ ]"로 떨어지는 것이
    # 의도한 동작이므로, 조회 키 타입에 None 을 포함시켜 그 계약을 적는다.
    mark: dict[str | None, str] = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
    lines = "\n".join(f"{mark.get(s.get('status'), '[ ]')} {s.get('content', '')}" for s in plan)
    return (
        "\n\n[현재 작업 계획]\n" + lines +
        "\n각 단계를 시작할 때 in_progress, 끝내면 completed로 update_plan을 호출해 갱신하라."
    )


TOOL_RESULT_TRUNCATION_LABEL = "[tool result truncated for model context]"
OLDER_TURNS_OMITTED_MARKER = "[older turns omitted for model context]"
# 예산 초과 시에도 반드시 남기는 최근 메시지 수. 마지막 요청과 그에 딸린 도구 결과가
# 통째로 사라지면 모델이 자기가 방금 무엇을 했는지 모른다.
MIN_RETAINED_MESSAGES = 8
# 예산 초과로 버릴 때는 이 비율까지 내려가게 한 번에 버린다. 딱 예산에 맞춰 버리면
# 다음 턴에 또 버려야 하고, 그러면 프리픽스가 매 턴 흔들려 안정화가 무의미해진다.
_DROP_HYSTERESIS = 0.7


def tool_result_cap(context_length: int, reserve_tokens: int = 0) -> int:
    """도구 결과 1건의 상한 — 런 시작에 한 번 계산해 고정한다.

    **대화 길이에 의존하면 안 된다.** 예전에는 캡이 `tool_count`와 '최근 6개' 윈도에
    따라 매 턴 다시 계산됐고, 그 결과 같은 도구 결과 하나가 한 런 안에서
    2296 → 901 → 644 → 501 → 409 → 346 → 300 → 265 → 237자로 아홉 번 다시 잘렸다.
    프롬프트 앞부분이 매 턴 바뀌므로 KV 캐시는 사실상 시스템 메시지 뒤로 한 번도
    재사용되지 못했다.

    값은 예전 정상 상태(도구 결과가 6개 이상 쌓였을 때의 `budget // 6`)와 같게 잡는다.
    초반 결과에 더 넉넉히 주던 동작만 사라지므로 예산 측면에서는 보수적이다.
    """
    avail_tokens = max(900, context_length - reserve_tokens - MAX_GEN_TOKENS)
    budget = avail_tokens * 3
    return max(800, min(6_000, budget // 6))


def truncate_tool_message(message: dict, cap: int) -> dict:
    """도구 결과를 기록 시점에 한 번 자른다. 이미 짧으면 원본 객체를 그대로 돌려준다."""
    if message.get("role") != "tool":
        return message
    content = str(message.get("content") or "")
    if len(content) <= cap:
        return message
    return {**message, "content": content[:cap] + "\n" + TOOL_RESULT_TRUNCATION_LABEL}


class ModelConversation(list):
    """append-only 대화. 도구 결과는 들어오는 순간 잘리고 그 뒤로 절대 변하지 않는다.

    자르는 지점을 여기 한 곳으로 모은 이유는, `role="tool"` 을 append 하는 곳이
    agent_runner에 11군데나 흩어져 있기 때문이다. 각 호출지에서 자르게 하면
    새 경로가 하나 늘 때마다 조용히 빠진다 — `_maybe_reindex`가 정확히 그렇게
    53개 종료 경로 중 15곳에만 붙어 있었다. 보장은 주석이 아니라 타입이 만든다.
    """

    def __init__(self, iterable=(), *, tool_result_cap: int) -> None:
        self._tool_result_cap = int(tool_result_cap)
        # 예산 초과로 이미 버린 구간의 끝. 단조 증가만 한다 — 되돌리면 프리픽스가
        # 다시 흔들린다. compact_convo가 읽고 갱신한다.
        self.dropped_before = 0
        super().__init__(truncate_tool_message(m, self._tool_result_cap) for m in iterable)

    def append(self, message: dict) -> None:
        super().append(truncate_tool_message(message, self._tool_result_cap))


def _build_model_conversation(
    messages: list[dict], context_length: int, reserve_tokens: int
) -> list[dict]:
    """주입용 팩토리 — 리서치 루프도 같은 기록-시점 절단 관문을 쓰게 한다."""
    return ModelConversation(
        messages, tool_result_cap=tool_result_cap(context_length, reserve_tokens)
    )


def _message_size(message: dict) -> int:
    return (
        len(str(message.get("content") or ""))
        + len(json.dumps(message.get("tool_calls") or "", ensure_ascii=False))
    )


def compact_convo(
    convo: list[dict],
    context_length: int,
    reserve_tokens: int = 0,
    *,
    output_reserve_tokens: int = 1024,
) -> list[dict]:
    """모델 컨텍스트를 예산 안에 유지한다 — **선택만 하고 내용은 다시 자르지 않는다.**

    도구 결과는 `ModelConversation`이 기록 시점에 런 고정 캡으로 이미 잘라 두었다.
    여기서 또 자르면 대화가 길어질 때마다 같은 메시지가 다른 바이트가 되어
    KV 프리픽스가 매 턴 깨진다(위 `tool_result_cap` 주석 참조).

    예산을 넘으면 **오래된 메시지를 통째로 버린다.** 남은 메시지의 바이트는 그대로이므로,
    버리는 턴에만 프리픽스가 한 번 흔들리고 그다음부터는 다시 안정된다. 매 턴 조금씩
    다시 자르는 것보다 훨씬 낫다.

    렌더러/이벤트 로그는 언제나 도구 결과 전문을 유지한다 — 이건 모델 컨텍스트 전용이다.
    """
    output_reserve_tokens = max(256, int(output_reserve_tokens or 1024))
    # num_ctx − fixed system/tool/RAG overhead − requested output allowance.
    avail_tokens = max(900, context_length - reserve_tokens - output_reserve_tokens)
    budget = avail_tokens * 3

    messages = list(convo)
    # 이미 버린 구간은 되돌리지 않는다. 되돌리면 프리픽스가 다시 흔들린다.
    watermark = min(int(getattr(convo, "dropped_before", 0) or 0), len(messages))
    kept = messages[watermark:]

    if sum(_message_size(message) for message in kept) <= budget:
        return _with_omission_marker(kept, watermark)

    # 예산을 넘으면 오래된 것부터 버린다. 한 칸씩만 버리면 매 턴 다시 버리게 되어
    # 프리픽스가 계속 흔들리므로, 여유분(hysteresis)까지 한 번에 버려 다음 몇 턴은
    # 버릴 필요가 없게 만든다. 프리픽스는 버리는 턴에만 흔들리고 그 뒤로 안정된다.
    target = max(1, int(budget * _DROP_HYSTERESIS))
    limit = max(0, len(messages) - MIN_RETAINED_MESSAGES)
    drop_to = watermark
    while drop_to < limit:
        if sum(_message_size(message) for message in messages[drop_to:]) <= target:
            break
        drop_to += 1
    # 잘린 꼬리가 고아 도구 결과로 시작하면 안 된다 — 짝이 되는 assistant tool_calls가
    # 없는 tool 메시지는 공급자가 거부한다.
    while drop_to < len(messages) and messages[drop_to].get("role") == "tool":
        drop_to += 1

    if drop_to > watermark:
        try:
            convo.dropped_before = drop_to  # type: ignore[attr-defined]
        except AttributeError:
            pass  # 평범한 list로 호출된 경우(리서치 채팅 등) — 상태 없이 동작한다
    return _with_omission_marker(messages[drop_to:], drop_to)


def _with_omission_marker(messages: list[dict], dropped: int) -> list[dict]:
    if dropped <= 0:
        return list(messages)
    # 마커는 바이트 고정이고 항상 0번이라 프리픽스를 흔들지 않는다.
    return [{"role": "user", "content": OLDER_TURNS_OMITTED_MARKER}, *messages]


# 작업 폴더 없이도 쓸 수 있는 도구 — 웹 조사·스킬·계획·Discord와 Aiso 자체가 저장한 ToDo 색인.
# 이 밖의 파일·코드·명령 도구는 작업 폴더가 있어야 하며, 없으면 노출도·실행도 하지 않는다.
WORKSPACE_FREE_TOOLS = frozenset(
    {
        "update_plan", "get_system_time", "list_calendar_events", "create_calendar_event", "manage_calendar_event",
        "list_mydb_library", "list_mydb_history", "list_mydb_trash", "restore_mydb_trash_node",
        "web_search", "web_fetch", "create_skill", "run_skill",
        "generate_image",
        "discord_server_map", "discord_server_apply", "discord_send",
        "discord_schedule_add", "discord_channel_report_add",
        "discord_schedule_list", "discord_schedule_remove",
    }
)

# 자동(auto)은 예외 없는 무승인 실행이다. 이 정책은 사용자가 직접 선택한
# 권한 모드이며, NVIDIA 전송 범위 고지는 설정에서 한 번만 안내한다.
# 읽기 모드에서는 작업 폴더/RAG의 내용이 모델에 노출된 뒤 외부 웹 도구로
# 나가는 경우에만 추가 확인을 유지한다. 이 경계는 자동 모드에 적용하지 않는다.
NETWORK_EGRESS_TOOLS = frozenset({"web_search", "web_fetch"})
WORKSPACE_CONTEXT_TOOLS = frozenset({
    "list_dir", "list_tree", "read_file", "grep", "glob", "analyze_document_calendar", "search_docs",
    "run_code", "run_command", "run_web",
})

def _nvidia_image_schema() -> dict:
    """Expose semantic generation inputs without any local registry selector."""
    schema = json.loads(json.dumps(GENERATE_IMAGE_SCHEMA, ensure_ascii=False))
    parameters = schema["function"]["parameters"]
    parameters["properties"].pop("model_hint", None)
    return schema


NVIDIA_GENERATE_IMAGE_SCHEMA = _nvidia_image_schema()

_IMAGE_TOOL_ARGS = frozenset(
    {
        "prompt", "negative_prompt", "model_hint", "width", "height", "seed",
    }
)

# Renderer settings are persisted JSON and the agent entry point is also used
# directly by tests/internal callers.  Keep the manual-selection boundary
# defensive here rather than relying only on the FastAPI request model.
_COMFY_SELECTION_MODES = frozenset({"auto", "manual"})
_COMFY_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_WEB_VALIDATION_ONLY_TOOLS = frozenset({
    "update_plan", "list_dir", "list_tree", "read_file", "glob", "run_web",
})
_WEB_VALIDATION_DISCOVERY_TOOLS = frozenset({
    "list_dir", "list_tree", "read_file", "glob",
})
_WEB_VALIDATION_LISTING_TOOLS = frozenset({"list_dir", "list_tree", "glob"})
_WEB_VALIDATION_DISCOVERY_TURN_LIMIT = 6
_WEB_VALIDATION_DISCOVERY_BATCH_LIMIT = 2
_WEB_VALIDATION_DISCOVERY_CALL_LIMIT = 8
_WEB_VALIDATION_INVALID_RUN_LIMIT = 2
_WEB_VALIDATION_RUN_BATCH_LIMIT = 8
_WEB_VALIDATION_TARGET_ATTEMPT_LIMIT = 2
_WEB_VALIDATION_TOTAL_ATTEMPT_LIMIT = 8
_CODE_AUTHORING_TOOLS = frozenset({
    "write_code_file", "edit_code_file", "multi_edit_code_file",
})
_WEB_VALIDATION_BLOCKED_MUTATION_TOOLS = frozenset({
    *_CODE_AUTHORING_TOOLS,
    "write_file", "edit_file", "multi_edit", "create_dir", "move",
    "delete_file", "delete_dir", "run_code", "run_command", "create_skill",
})


def _manual_comfy_selection_error(
    mode: Any,
    selected_profile_id: Any,
    profiles: list[dict],
) -> tuple[str | None, str | None]:
    """Validate a manual model choice before exposing the image tool.

    Returns ``(error, exact_profile_id)``.  The selected ID is deliberately
    matched case-sensitively against the renderer-provided registered profile
    list.  A model name or an LLM ``model_hint`` never substitutes for it.
    """
    if mode not in _COMFY_SELECTION_MODES:
        return "ComfyUI 모델 선택 모드가 올바르지 않습니다. 설정에서 자동 또는 직접 선택을 다시 저장해 주세요.", None
    if mode != "manual":
        return None, None
    if not isinstance(selected_profile_id, str) or not _COMFY_PROFILE_ID_RE.fullmatch(selected_profile_id):
        return "직접 선택 모드에서는 이미지 생성 전에 등록 모델 하나를 선택해야 합니다.", None
    if not any(
        isinstance(profile, dict) and profile.get("id") == selected_profile_id
        for profile in profiles
    ):
        return "선택한 모델을 현재 수동 실행 후보에서 찾을 수 없습니다. 등록 상태와 준비 상태를 확인해 주세요.", None
    return None, selected_profile_id


def _looks_like_image_generation_request(
    text: str,
    previous_assistant: str = "",
    *,
    previous_image_verified: bool = False,
) -> bool:
    """명시적인 생성 의도만 인정하고, 부정·설명 요청을 실제 GPU 작업으로 뒤집지 않는다."""
    lowered = " ".join(text.casefold().split())

    # 이미지에 넣을 인용문(예: '포기하지 마')을 생성 거부로 오인하지 않는다.
    unquoted = lowered
    for quoted in (r'"[^"\n]*"', r"(?<!\w)'[^'\n]*'(?!\w)", r"“[^”\n]*”", r"‘[^’\n]*’"):
        unquoted = re.sub(quoted, " ", unquoted)
    denial_patterns = (
        r"(?:이미지|그림|사진|일러스트|텍스처).{0,24}(?:생성|그리|만들|뽑)(?:하)?지\s*(?:마|말)",
        r"(?:생성|그리|만들|뽑)(?:하)?지\s*(?:마|말)",
        r"(?:이미지|그림|사진|일러스트|텍스처).{0,24}원하지\s*않",
        r"(?:생성|그리|그려|만들|뽑).{0,24}싶지\s*않",
        r"(?:생성|그리|그려|만들|뽑).{0,24}필요(?:는|가)?\s*없",
        r"(?:생성|그리|만들|뽑)(?:하)?지\s*않아도",
        r"(?:이미지|그림|사진|일러스트|텍스처).{0,24}안\s*(?:해도|그려도|만들어도|뽑아도)",
        r"\b(?:do not|don't|never)\s+(?:generate|create|draw)\b",
        r"\b(?:i\s+)?(?:do not|don't)\s+want\s+(?:you\s+to\s+)?(?:generate|create|draw|an?\s+image)",
        r"^no image(?:\s|$)",
    )
    if any(re.search(pattern, unquoted) for pattern in denial_patterns):
        return False
    command_text = " ".join(unquoted.split())

    # 문장의 주된 요청이 설명·확인이라면 중간의 '생성하고/그려서'를 실행 명령으로 보지 않는다.
    meta_nouns = (
        "방법", "하는 법", "과정", "절차", "사용법", "튜토리얼",
        "수 있는지", "가능한지", "어떻게 해야", "어떻게 하면",
    )
    meta_end = re.search(
        r"(?:설명(?:해\s*줘|해주세요|해줘|해\s*주세요)?|알려\s*(?:줘|주세요)|"
        r"보여\s*(?:줘|주세요)|확인해\s*(?:줘|주세요)|말해\s*(?:줘|주세요)|"
        r"요약해\s*(?:줘|주세요)|정리해\s*(?:줘|주세요)|번역해\s*(?:줘|주세요)|"
        r"검토해\s*(?:줘|주세요)|분석해\s*(?:줘|주세요)|문서화해\s*(?:줘|주세요)|"
        r"뭐야|무엇(?:이야|인가요)?|어디서\s*확인해)\s*[?.!]*$",
        command_text,
    )
    procedural_end = re.search(
        r"(?:방법|하는 법|과정|절차|사용법|튜토리얼|하려면|려면)\s*[?.!]*$",
        command_text,
    )
    if procedural_end or (meta_end and any(marker in command_text for marker in meta_nouns)):
        return False
    if command_text.startswith(("how to generate", "how to create", "how to draw")):
        return False

    software_requests = (
        "기능을 만들어", "기능 만들어", "기능 구현", "워크플로를 만들어", "워크플로 만들어",
        "코드를 만들어", "코드 만들어", "모듈을 만들어", "모듈 만들어", "프로그램을 만들어",
        "서비스를 만들어", "플러그인을 만들어", "엔드포인트를 만들어", "앱을 만들어",
        "생성 버튼을 만들어", "생성 모듈을 만들어", "생성 기능을 만들어",
    )
    if any(marker in command_text for marker in software_requests):
        return False
    if re.search(
        r"(?:그래프|다이어그램|순서도|프로젝트 구조|아키텍처 도식)"
        r"(?:을|를|으로|로)?\s*(?:그려|만들어|생성)",
        command_text,
    ) or re.search(r"\b(?:draw|create)\s+(?:a\s+)?(?:flowchart|diagram|architecture chart)\b", command_text):
        return False

    if re.search(r"(?:그려\s*(?:줘|주세요|줄래)|그려서|그린\s*뒤)", command_text):
        return True

    subjects = (
        "이미지", "그림", "캐릭터", "일러스트", "텍스처", "사진",
        "image", "picture", "illustration", "texture", "artwork", "photo",
    )
    has_subject = any(subject in command_text for subject in subjects)
    generation_command = re.search(
        r"생성\s*(?:(?:좀|(?:한|두|세|네|\d+)\s*(?:번|장|개)(?:만)?|한번(?:만)?|하나(?:만)?)\s*)?"
        r"(?:해\s*줘|해주세요|해\s*주세요|해\s*줄래|부탁해|부탁드립니다)|생성(?:하고|해서)",
        command_text,
    )
    make_command = re.search(r"(?:만들어|뽑아)\s*(?:줘|주세요|줄래)", command_text)
    noun_request = re.search(
        r"(?:이미지|그림|사진|일러스트|텍스처|캐릭터)(?:를|을)?\s*"
        r"(?:(?:한\s*장|하나)(?:만)?\s*)?부탁(?:해|드립니다)",
        command_text,
    )
    if has_subject and (generation_command or make_command or noun_request):
        return True

    # '이미지 생성'이 설명/소프트웨어 수식어로만 쓰인 경우는 실행하지 않는다.
    if any(term in command_text for term in ("이미지 생성", "그림 생성", "image generation")):
        return False

    stripped = command_text.lstrip()
    english_requests = (
        "generate ", "create ", "draw ", "please generate ", "please create ", "please draw ",
        "can you generate ", "can you create ", "can you draw ",
        "could you generate ", "could you create ", "could you draw ",
    )
    english_software_request = re.match(
        r"^(?:please\s+|can you\s+|could you\s+)?create\s+(?:a\s+|an\s+|the\s+)?"
        r"(?:(?:python|typescript|javascript)\s+)?"
        r"(?:script|code|program|module|service|plugin|endpoint|feature|generator|api|ui|button)\b",
        stripped,
    ) or re.match(
        r"^(?:please\s+|can you\s+|could you\s+)?create\s+(?:a\s+|an\s+|the\s+)?"
        r"image generation\s+(?:feature|module|service|api|ui|button)\b",
        stripped,
    )
    if english_software_request:
        return False
    if has_subject and any(stripped.startswith(marker) for marker in english_requests):
        return True

    # A research-first request often starts with "search" rather than "draw".
    # It is still a genuine image command when it later asks to draw or
    # illustrate a character/image through ComfyUI. Keep the condition narrow
    # so an unrelated diagram or software-authoring request does not start GPU
    # generation.
    if (
        ("comfyui" in command_text or has_subject)
        and re.search(r"\b(?:draw|illustrate|paint|render)\b", command_text)
    ):
        return True

    # Assistant prose is never evidence that an image exists.  The renderer
    # sets this flag only after it has received and persisted a real
    # ``image_result`` event for the current conversation.  Keeping the old
    # text parameter preserves callers/tests but deliberately does not make a
    # completion-looking sentence trustworthy.
    context_is_image = bool(previous_image_verified)
    contextual_actions = (
        "진행해줘", "진행해 줘", "그걸로 해줘", "그걸로 해 줘", "이걸로 해줘", "이걸로 해 줘",
        "그대로 해줘", "그대로 해 줘", "뽑아줘", "뽑아 줘", "한 장 부탁", "하나 더",
        "바꿔줘", "바꿔 줘", "수정해줘", "수정해 줘", "다시 생성해줘", "다시 생성해 줘",
        "go with that", "use that one", "one more", "regenerate",
    )
    english_change = bool(re.match(r"^(?:please\s+)?change\s+.+\s+to\s+.+[.!]*$", command_text))
    # A correction can be phrased as visual feedback rather than an imperative
    # (for example, "the expression is too dark").  It is actionable only
    # when a real image event supplied the verified context above.
    visual_feedback = bool(
        re.search(
            r"(?:표정|얼굴|눈|머리|색감|구도|배경|포즈|의상|밝은|밝게|어두운|어둡|아이돌|"
            r"expression|face|eyes?|hair|colou?r|composition|background|pose|outfit|brighter|darker)",
            command_text,
        )
    )
    return context_is_image and (
        any(marker in command_text for marker in contextual_actions) or english_change or visual_feedback
    )


_ENGLISH_MUTATION_VERB = (
    r"(?:create|build|implement|write|rewrite|edit|add|fix|change|modify|improve|"
    r"optimi[sz]e|update|refactor|repair|revise|polish|apply|perform|execute|"
    r"carry\s+out|patch|replace|delete|remove|rename|move|"
    r"copy|overwrite|generate|make|develop|code|save|deploy|publish|format|convert|"
    r"export|archive|upload|install|minify|bundle|compile|package|commit|push|release|ship)"
)

_ENGLISH_MUTATION_REQUEST_PREFIX = (
    r"(?:(?:(?:please|kindly)\s+)?(?:can|could|would)\s+you\s+"
    r"(?:(?:please|kindly)\s+)?|"
    r"(?:(?:please|kindly)\s+)?|"
    r"i\s+(?:need|want|would\s+like)\s+you\s+to\s+|"
    r"let(?:'s|\s+us)\s+)"
)


_ENGLISH_VALIDATION_ACTION = (
    r"(?:(?:re[-\s]?)?(?:verify|validate|test|check|run)|open|audit|review|inspect)"
)


_ENGLISH_VALIDATION_REQUEST_PREFIX = (
    r"(?:(?:(?:please|kindly)\s+)?(?:can|could|would)\s+you\s+"
    r"(?:(?:please|kindly)\s+)?|"
    r"(?:(?:please|kindly)\s+)?would\s+you\s+mind\s+|"
    r"(?:please|kindly)\s+|"
    r"i\s+(?:need|want|would\s+like)\s+you\s+to\s+|"
    r"let['’]?s\s+|you\s+)?"
)


def _is_bare_validation_question(text: str) -> bool:
    """Default-deny terse status questions that do not contain a request form."""
    normalized = " ".join(str(text or "").split())
    if not normalized.endswith("?"):
        return False
    if re.match(
        r"^(?:verify|validate|test|check|run|open|audit|review|inspect)\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        return True
    return bool(
        re.search(r"(?:검증|테스트|점검|검토|검수|체크|확인)\s*\?$", normalized)
        and not re.search(
            r"(?:해\s*줘|해주세요|해줘|해봐|해\s*봐|해\s*줄래|해줄래|"
            r"해\s*줄\s*수\s*있(?:어|을까|나요)?|부탁|하자|봐\s*줘|봐줘)\s*\?$",
            normalized,
        )
    )


def _has_positive_validation_command(text: str) -> bool:
    """Find an actual validation command, not a denial, explanation, or future statement."""
    normalized = " ".join(str(text or "").split())
    if not normalized or _is_bare_validation_question(normalized):
        return False
    # A command may start the request or a later clause.  Keeping the boundary
    # grammar explicit prevents "why rerun", "I will validate", and similar
    # descriptive text from being interpreted as authorization.
    boundary = (
        r"(?:^|[.;:!?/\u2014\u2013]\s*|\s+-\s+|,\s*|"
        r"\b(?:and|then|but|however|also|rather|yet|instead|plus|while|as|so)\s+)"
    )
    modifier = r"(?:(?:however|also|rather|yet|instead|then|now)\s+)?"
    request = _ENGLISH_VALIDATION_REQUEST_PREFIX
    base_action = (
        r"(?:(?:re[-\s]?)?(?:verify|validate|test|check|run)|open|audit|review|inspect|"
        r"(?:re)?verifying|(?:re)?validating|(?:re)?testing|(?:re)?checking|"
        r"(?:re)?running|auditing|reviewing|inspecting)"
    )
    return bool(re.search(rf"{boundary}{modifier}{request}{base_action}\b", normalized))


def _has_trailing_validation_cancellation(text: str) -> bool:
    """Recognize a same-turn withdrawal that revokes an earlier validation command."""
    normalized = " ".join(str(text or "").casefold().split())
    if not re.search(
        rf"(?:{_ENGLISH_VALIDATION_ACTION}|검증|테스트|점검|검토|확인)",
        normalized,
    ):
        return False
    return bool(re.search(
        r"(?:[,;:]|\s[-—–]\s|\b(?:but|actually)\b).{0,80}"
        r"(?:never\s*mind|scratch\s+that|cancel\s+that|cancel\s+it|"
        r"(?:no\s*,?\s*)?(?:do\s+not|don't|dont)(?:\s+do\s+that)?|"
        r"(?:no\s*,?\s*)?cancel|actually\s+no)\s*[.!?]*$|"
        r"(?:그건?|검증|테스트)?\s*(?:취소|하지\s*마|하지\s*말|됐어|그만)"
        r"(?:해|해줘|해주세요)?\s*[.!?]*$",
        normalized,
    ))


def _is_standalone_validation_rejection(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return bool(re.fullmatch(
        r"(?:cancel(?:\s+(?:it|that))?|never\s*mind|scratch\s+that|stop|"
        r"no(?:\s+thanks)?|nope|nah|not\s+now|forget\s+it|leave\s+it|"
        r"취소(?:해|해줘|해주세요|할게)?|그만(?:해|해주세요)?|됐어|아니(?:야)?|"
        r"안\s*(?:할래|해)|필요\s*없어|멈춰)[.!?]?",
        normalized,
    ))


def _has_candidate_reference(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return bool(re.search(
        r"\b(?:it|its|this|that|these|those|them|their|theirs|one|ones|both|all|either|whichever|"
        r"each|every|pair|option|options|choice|choices|candidate|candidates|"
        r"file|files|page|pages|target|targets|former|latter|first|second|third|"
        r"last|other|previous|changes?|revisions?|edits?|modifications?)\b|"
        r"\b(?:as\s+discussed|accordingly|go\s+ahead\s+with\s+(?:it|that|the\s+changes?))\b|"
        r"(?:그거|이거|저거|그것|이것|저것|후보|선택지|파일|페이지|"
        r"첫\s*번째|두\s*번째|세\s*번째|다른\s*(?:것|거)|이전\s*(?:것|거)|"
        r"모두|전부|둘\s*다|아무거나|각각|그것들|이것들)",
        normalized,
    ))


def _is_nonexecuting_web_validation_statement(text: str) -> bool:
    """Return True when validation is discussed or denied but not requested."""
    normalized = " ".join(str(text or "").casefold().split())
    if not normalized:
        return False
    if _has_trailing_validation_cancellation(normalized):
        return True
    if _has_mixed_positive_validation_clause(normalized):
        return False
    validation_term = bool(re.search(
        rf"\b(?:validation|verification|{_ENGLISH_VALIDATION_ACTION}|"
        r"validating|verifying|testing|checking|running|auditing|reviewing|inspecting|"
        r"run_web|(?:browser|web)\s+(?:checks?|tests?|testing)|"
        r"rechecking|retesting|rerunning|검증|재검증|테스트|점검|검토|검수|체크|확인)\b",
        normalized,
    ))
    if not validation_term:
        return False
    denied = bool(re.search(
        rf"\b(?:do\s+not|don't|dont)\s+(?:ever\s+)?{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\bnever\s+{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\b(?:there(?:'s|\s+is)\s+)?no\s+need\s+to\s+{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\b(?:you\s+)?(?:do\s+not|don't|dont)\s+need\s+to\s+{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\bi(?:\s+am|'m)\s+not\s+asking\s+you\s+to\s+{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\b(?:i|we)\s+(?:do\s+not|don't|dont)\s+want\s+(?:you\s+)?to\s+"
        rf"{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\b(?:you\s+)?(?:should|must|shall|need)\s+not\s+{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\b(?:you\s+)?(?:shouldn't|mustn't|needn't)\s+{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\bunder\s+no\s+circumstances\b.{{0,40}}\b{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\b(?:cancel|skip|avoid)\s+(?:the\s+)?(?:validation|verification|validating|"
        rf"verifying|testing|checking|running|auditing|reviewing|inspecting|"
        rf"checking|{_ENGLISH_VALIDATION_ACTION})\b|"
        rf"\brefrain\s+from\s+(?:{_ENGLISH_VALIDATION_ACTION}|validating|verifying|testing|"
        rf"checking|running|auditing|reviewing|inspecting)\b|"
        r"\b(?:do\s+not|don't|dont)\s+(?:use\s+)?run_web\b|"
        r"\b(?:avoid|skip)\s+(?:(?:the\s+)?(?:browser|web)\s+)?"
        r"(?:checks?|testing|tests?|validation|verification)\b|"
        r"\bhold\s+off\s+on\s+(?:the\s+)?(?:testing|tests?|checking|validation|verification)\b|"
        r"\b(?:leave|keep)\s+(?:the\s+)?(?:browser\s+testing|web\s+validation|validation)\s+off\b|"
        r"\b(?:browser\s+testing|web\s+validation|validation)\s+(?:is\s+)?(?:off|disabled)\b|"
        r"\bno\s+need\s+for\s+(?:a|the)?\s*(?:(?:browser|web)\s+)?"
        r"(?:checks?|tests?|testing|validation|verification)\b|"
        r"\b(?:do\s+not|don't|dont)\s+execute\s+(?:the\s+)?(?:browser|web)?\s*"
        r"(?:tests?|testing|checks?|validation|verification)\b|"
        r"\bwithout\s+(?:(?:browser|web)\s+)?(?:validation|verification|testing|checking|"
        r"running\s+(?:validation|verification)|testing\s+it|checking\s+it)\b|"
        r"\bno\s+(?:web|browser)?\s*(?:test|testing|check|checking|validation|verification)\b|"
        r"\b(?:validation|verification|testing|checking)\b.{0,24}"
        r"\b(?:is\s+)?(?:unnecessary|not\s+needed|not\s+required)\b|"
        r"(?:검증|테스트|점검|검토|검수|체크|확인).{0,20}"
        r"(?:하지\s*마|하지\s*말|필요\s*없|안\s*해도)",
        normalized,
    ))
    explanatory = bool(re.search(
        rf"\b(?:show|tell)\s+me\s+how\s+to\s+{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\bwhat\s+happens?\s+if\s+(?:i|we|you)\s+{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\bshould\s+(?:i|we)\s+{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\b(?:why|how)\b.{{0,40}}\b(?:validation|verification|{_ENGLISH_VALIDATION_ACTION})\b|"
        rf"\b(?:what\s+is|explain|is\s+it\s+possible)\b.{{0,40}}"
        rf"\b(?:validation|verification|{_ENGLISH_VALIDATION_ACTION})\b|"
        r"(?:검증|테스트|점검|검토|검수|체크).{0,28}"
        r"(?:뭐(?:야|지)?|무엇|의미|방법|어떻게|설명|가능(?:해|한지|한가)|"
        r"지원(?:해|하는지|여부)|필요(?:해|한지))",
        normalized,
    ))
    future_statement = bool(re.search(
        rf"^\s*(?:i|we)\s+(?:will|'ll|am\s+going\s+to|are\s+going\s+to)\s+"
        rf"{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"^\s*(?:i'm|i\s+am|we're|we\s+are)\s+going\s+to\s+"
        rf"{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\b(?:i|we)\s+(?:might|may|could|plan\s+to|intend\s+to|expect\s+to|"
        rf"hope\s+to)\s+{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\bmaybe\b.{{0,40}}\b{_ENGLISH_VALIDATION_ACTION}\b|"
        rf"\b{_ENGLISH_VALIDATION_ACTION}\b.{{0,60}}\b(?:later|tomorrow|"
        r"next\s+(?:time|week|month)|someday|eventually)\b|"
        r"(?:나중에|추후에).{0,24}(?:검증|테스트|점검|확인)",
        normalized,
    ))
    return denied or explanatory or future_statement


def _strip_no_edit_phrases(text: str) -> str:
    """Remove only explicit no-change clauses before mutation-intent checks."""
    cleaned = str(text or "").casefold()
    cleaned = re.sub(
        rf"\b(?:(?:and|but)\s+)?{_ENGLISH_MUTATION_VERB}\s+"
        rf"(?:nothing|no\s+changes?|zero\s+changes?)\b|"
        r"\bwithout\s+(?:any\s+)?(?:changes?|edits?|editing(?:\s+it)?|modifications?)\b|"
        r"\bwithout\s+(?:changing\s+anything|making\s+(?:any\s+)?changes?)\b|"
        r"\b(?:but\s+)?(?:do\s+not|don't|dont)\s+"
        r"(?:edit|change|modify|rewrite|update)(?:\s+it)?\b|"
        r"\b(?:i|we)\s+(?:did|do|have)\s+not\s+"
        r"(?:edit(?:ed)?|change(?:d)?|modif(?:y|ied)|rewrite|update(?:d)?)(?:\s+it)?\b|"
        r"\b(?:make|made)\s+no\s+(?:changes?|edits?)\b|"
        r"\bno\s+(?:changes?|edits?)\s+(?:were\s+)?made\b|"
        r"\b[a-z0-9_./\\-]+\.html?\s+was\s+not\s+"
        r"(?:edited|changed|modified|rewritten|updated)\b|"
        r"(?:수정|변경|편집|업데이트)(?:하지\s*말고|\s*없이)|"
        r"건드리지\s*말고|원본(?:을|를)?\s*유지하고",
        " ",
        cleaned,
    )
    return " ".join(cleaned.split())


def _contains_explicit_mutation_request(text: str) -> bool:
    """Detect a requested artifact mutation, excluding explicit no-change clauses."""
    cleaned = _strip_no_edit_phrases(text)
    if not cleaned:
        return False
    # 설명·회고·사용법 요청 속 과거/관형형 동사는 실행 권한이 아니다.
    # 예: "만들어서 테스트했던 과정을 설명해줘", "고치고 테스트했다는 기록을 요약해줘".
    if re.search(
        r"(?:만들|작성|구현|수정|고치|바꾸|변경)(?:어(?:서)?|해(?:서)?|고)\s*"
        r"(?:검증|테스트|점검|확인)(?:했(?:던|다는)|한|하는)\s*"
        r"(?:과정|방법|기록|사례|내용).{0,40}(?:설명|알려|요약|정리)",
        cleaned,
    ):
        return False
    gerund_mutation = (
        r"(?:editing|updating|fixing|changing|modifying|rewriting|creating|building)"
    )
    if re.search(
        rf"^(?:after|once)\s+{gerund_mutation}\b|"
        rf"^(?:verify|validate|test|check|run|audit|review|inspect)\b.{{0,160}}"
        rf"\bafter\s+{gerund_mutation}\b",
        cleaned,
    ):
        return True
    command_end = r"(?=\s|$|[,;:!?]|\.(?:\s|$))"
    if re.search(
        rf"^{_ENGLISH_MUTATION_REQUEST_PREFIX}{_ENGLISH_MUTATION_VERB}{command_end}|"
        rf"(?:[.,;!?]|\b(?:and\s+then|and|then)\b)\s*(?:please\s+)?"
        rf"{_ENGLISH_MUTATION_VERB}{command_end}|"
        rf"^(?:use|read|open)\s+__html_path__.{{0,100}}\b(?:to|and\s+then)\s+"
        rf"{_ENGLISH_MUTATION_VERB}{command_end}|"
        rf"\b{_ENGLISH_MUTATION_VERB}\s+(?:it|the\s+(?:file|page|app|code|docs?))\b",
        cleaned,
    ):
        return True
    return bool(re.search(
        r"(?:만들어\s*(?:줘|주세요)|만들(?:고|어서)|고쳐\s*(?:줘|주세요|서)|고치고|"
        r"바꿔\s*(?:줘|주세요|서)|바꾸고)|"
        r"(?:만들|작성|생성|구현|추가|수정|고치|바꾸|변경|개발|개선|최적화|보완|"
        r"업데이트|리팩터|리팩토|수리|패치|교체|대체|삭제|제거|이름\s*변경|"
        r"이동|복사|덮어|저장|코딩|웹\s*작업|배포|게시|포맷|변환|내보내|"
        r"압축|보관|업로드|설치|최소화|번들|컴파일|패키징|커밋|푸시|릴리스)"
        r"(?:\s*(?:계속|해|하고|해서|고|어서|한\s*뒤|후|뒤|해\s*줘|해주세요|하자|할래|"
        r"좀\s*(?:해\s*)?(?:줘|주세요)|부탁(?:해|해요|합니다|드립니다)))",
        cleaned,
    ))


def _has_ambiguous_validation_target_reference(text: str) -> bool:
    """Recognize invalid or unresolved plural targets before interpreting a tail verb."""
    normalized = " ".join(str(text or "").split())
    validation_mentioned = bool(re.search(
        r"(?:검증|테스트|점검|검토|확인)|"
        r"\b(?:verify|validate|test|check|run|audit|review|inspect)\b",
        normalized,
        flags=re.IGNORECASE,
    ))
    if not validation_mentioned:
        return False
    if _explicit_html_paths(normalized):
        return False
    return bool(_invalid_html_path_mentions(normalized) or len(_html_path_tokens(normalized)) > 1)


def _is_clear_multi_html_authoring_request(text: str) -> bool:
    """Recognize multi-file authoring with an exact, later validation scope."""
    normalized = " ".join(str(text or "").split())
    paths = _html_path_tokens(normalized)
    if len(paths) < 2 or _invalid_html_path_mentions(normalized):
        return False
    masked = " ".join(_mask_html_path_mentions(normalized).casefold().split())
    if not _contains_explicit_mutation_request(masked):
        return False
    if re.search(r"\b(?:or|either)\b|(?:또는|혹은|아니면)", masked):
        return False
    validation = bool(re.search(
        r"\b(?:verify|validate|test|check|run|audit|review|inspect)\b|"
        r"(?:검증|테스트|점검|확인)",
        masked,
    ))
    if not validation:
        return False
    mutation_match = re.search(
        rf"\b{_ENGLISH_MUTATION_VERB}\b|"
        r"(?:만들|작성|구현|추가|수정|고치|바꾸|변경|개선|최적화|보완)",
        masked,
    )
    validation_match = re.search(
        r"\b(?:verify|validate|test|check|run|audit|review|inspect)\b|"
        r"(?:검증|테스트|점검|확인)",
        masked,
    )
    if mutation_match is None or validation_match is None or mutation_match.start() > validation_match.start():
        return False
    validates_all = bool(re.search(
        r"\b(?:both|all|each|every)\b|(?:둘\s*다|모두|전부|각각)",
        masked,
    ))
    reference_flow = bool(re.search(
        r"\b(?:using|with)\b.{0,80}\b(?:as\s+(?:a\s+)?reference|for\s+reference)\b|"
        r"\bbased\s+on\b|\bas\s+(?:a\s+)?template\b|\bfrom\s+it\b|"
        r"(?:참고|레퍼런스|기준)(?:로|으로|해서|하여)",
        masked,
    ))
    return validates_all or reference_flow or bool(_validation_clause_html_paths(normalized))


def _validation_clause_html_paths(text: str) -> list[str]:
    """Return exact paths named by the final validation clause (English or Korean SOV)."""
    normalized = " ".join(str(text or "").split())
    matches = list(re.finditer(
        r"\b(?:verify|validate|test|check|run|audit|review|inspect)\b|"
        r"(?:검증|테스트|점검|확인)",
        normalized,
        flags=re.IGNORECASE,
    ))
    if not matches:
        return []
    last = matches[-1]
    tail_paths = _html_path_tokens(normalized[last.start():])
    if tail_paths:
        return tail_paths
    # Korean normally places the object immediately before 검증/테스트. Pick
    # the closest valid path before the final verb, not an earlier reference.
    if re.fullmatch(r"(?:검증|테스트|점검|확인)", last.group(0), re.IGNORECASE):
        ranked: list[tuple[int, str]] = []
        for path in _html_path_tokens(normalized[:last.start()]):
            position = normalized.rfind(path, 0, last.start())
            if position < 0:
                position = normalized.casefold().rfind(path.casefold(), 0, last.start())
            if position >= 0:
                ranked.append((position, path))
        if ranked:
            return [max(ranked, key=lambda item: item[0])[1]]
    return []


def _normal_requested_validation_paths(text: str) -> list[str]:
    """Extract only HTML outputs the user actually asked to validate."""
    normalized = " ".join(str(text or "").split())
    paths = _html_path_tokens(normalized)
    if not paths or not _has_explicit_validation_execution_command(normalized):
        return []
    if _is_clear_multi_html_authoring_request(normalized) and re.search(
        r"\b(?:both|all|each|every)\b|(?:둘\s*다|모두|전부|각각)",
        _mask_html_path_mentions(normalized).casefold(),
    ):
        return paths
    clause_paths = _validation_clause_html_paths(normalized)
    if clause_paths:
        return clause_paths
    if len(paths) == 1:
        return paths
    return []


def _request_directly_mutates_html_path(text: str, path: str) -> bool:
    """Whether the request requires this HTML itself to change before validation."""
    normalized = " ".join(str(text or "").casefold().split())
    if _request_explicitly_preserves_path(normalized, path):
        return False
    escaped_path = re.escape(path.casefold())
    english_mutation = re.compile(rf"\b{_ENGLISH_MUTATION_VERB}\b")
    english_validation = re.compile(
        r"\b(?:verify|validate|test|check|run|audit|review|inspect)\b"
    )
    for occurrence in re.finditer(escaped_path, normalized):
        before = normalized[:occurrence.start()]
        mutations = list(english_mutation.finditer(before))
        validations = list(english_validation.finditer(before))
        if mutations:
            last_mutation = mutations[-1]
            last_validation_start = validations[-1].start() if validations else -1
            if last_mutation.start() > last_validation_start:
                return True
            bridge = before[last_mutation.start():]
            if re.fullmatch(
                rf"{_ENGLISH_MUTATION_VERB}\s*[,;]?\s*"
                r"(?:(?:and\s+)?(?:then\s+)?)?"
                r"(?:verify|validate|test|check|run|audit|review|inspect)"
                r"(?:\s+(?:only|the))?\s*",
                bridge,
            ):
                return True
        after = normalized[occurrence.end():occurrence.end() + 120]
        following_mutation = english_mutation.search(after)
        following_validation = english_validation.search(after)
        if following_mutation and (
            following_validation is None
            or following_mutation.start() < following_validation.start()
        ):
            mutation_tail_end = (
                following_validation.start() if following_validation else len(after)
            )
            mutation_tail = after[following_mutation.end():mutation_tail_end]
            named_dependencies = _non_html_file_tokens(normalized)
            direct_dependency_target = any(
                re.match(
                    rf"^\s*(?:only\s+)?(?:the\s+)?(?:[`\"'“‘]\s*)?"
                    rf"{re.escape(dependency)}(?:\s*[`\"'”’])?(?=$|[\s,.;:!?])",
                    mutation_tail,
                )
                for dependency in named_dependencies
            )
            if not direct_dependency_target:
                return True
        if re.search(
            r"\bafter\s+(?:editing|updating|fixing|changing|modifying|rewriting|"
            r"creating|building)\s+(?:it|the\s+(?:file|page|html))\b",
            after,
        ):
            return True
        korean_mutation = re.search(
            r"(?:을|를|은|는|도|만)?\s*"
            r"(?:만들|작성|구현|추가|수정|고치|바꾸|변경|개선|최적화|보완)",
            after,
        )
        korean_validation = re.search(r"(?:검증|테스트|점검|확인)", after)
        if korean_mutation and (
            korean_validation is None or korean_mutation.start() < korean_validation.start()
        ):
            return True
    return False


def _has_ambiguous_validation_target_for_mutation(text: str) -> bool:
    """Backward-compatible predicate used by focused policy tests."""
    normalized = " ".join(str(text or "").split())
    return bool(
        not _is_clear_multi_html_authoring_request(normalized)
        and
        _has_ambiguous_validation_target_reference(normalized)
        and (
            _contains_explicit_mutation_request(_mask_html_path_mentions(normalized))
            or _has_additional_operation_after_validation(normalized)
        )
    )


def _is_ambiguous_candidate_mutation_reply(text: str) -> bool:
    """Recognize a mutation that refers back to an unresolved HTML candidate."""
    normalized = " ".join(str(text or "").casefold().split())
    if not normalized or _is_explicit_task_reset(normalized):
        return False
    if not _contains_explicit_mutation_request(_mask_html_path_mentions(normalized)):
        return False
    valid_paths = _html_path_tokens(normalized)
    if _invalid_html_path_mentions(normalized) or len(valid_paths) > 1:
        return True
    if len(valid_paths) == 1:
        return False
    # Any mutation without one exact HTML path remains bound to the unresolved
    # candidate state.  A genuinely new mutation task must explicitly reset the
    # task or name its own target path; this avoids verb-list bypasses.
    if _has_explicit_non_html_file_target(normalized):
        return False
    return True


def _has_additional_operation_after_validation(text: str) -> bool:
    """Fail validation-only classification when a second operation is requested."""
    normalized = " ".join(_mask_html_path_mentions(str(text or "").casefold()).split())
    english_action = r"(?:(?:re[-\s]?)?(?:verify|validate|test|check|run)|open|audit|review|inspect)"
    for match in re.finditer(
        rf"\b{english_action}\b.{{0,180}}?(?:\b(?:and\s+then|and|but|then|after\s+that)\b|[;,])"
        r"\s*(?P<tail>[^.;!?]+)",
        normalized,
    ):
        tail = re.sub(
            r"^(?:(?:and|but|however|rather|also|instead|yet|then|now|please)\s+)+",
            "",
            match.group("tail").strip(),
        )
        if not tail or re.match(
            r"^(?:do\s+not|don't|dont|without|nothing|no\s+changes?|not\b)",
            tail,
        ):
            continue
        if _has_positive_validation_command(tail):
            continue
        if re.match(r"^(?:explain|report|summarize|describe|show|tell)\b", tail):
            continue
        if re.match(
            r"^(?:give\s+me\s+(?:the\s+)?(?:results?|findings?|report)|"
            r"let\s+me\s+know\b|"
            r"(?:list|return)\s+(?:any\s+|the\s+)?(?:errors?|results?|findings?|issues?))\b",
            tail,
        ):
            continue
        if re.match(
            r"^(?:(?:click|press|type|fill|select|choose|hover|scroll|interact|"
            r"focus|drag|drop|tap)\b|wait\b|take\s+(?:a\s+)?screenshot\b|"
            r"(?:assert|expect|confirm)\b)",
            tail,
        ):
            continue
        # A status clause explains why the requested validation is useful; it
        # is not a second operation.  Unknown command-like tails still fail
        # closed into normal mode below.
        if re.match(
            r"^(?:it|this|that|the\s+(?:app|page|file|test|validation|verification))\s+"
            r"(?:(?:is|was|seems?|appears?|keeps?)\b|(?:fails?|failed)\b|"
            r"(?:does\s+not|doesn't|doesnt)\s+work\b)",
            tail,
        ):
            continue
        return True

    korean_match = re.search(
        r"(?:검증|테스트|점검|검토|확인)(?:해)?\s*"
        r"(?:하고|한\s*뒤|한\s*후|후|뒤)\s*(?P<tail>.+)$",
        normalized,
    )
    if korean_match:
        tail = korean_match.group("tail").strip()
        if re.match(r"^(?:다시\s*)?(?:검증|테스트|점검|검토|확인)", tail):
            return False
        if re.match(r"^(?:(?:결과|오류|문제|내용)(?:를|을)?\s*)?(?:알려|보고|설명|요약)", tail):
            return False
        if re.match(
            r"^(?:.{0,48}(?:클릭|누르|입력|채우|선택|호버|스크롤|기다리|대기|"
            r"스크린샷|캡처|상호작용|포커스|드래그|드롭|탭|보이는지|바뀌는지|"
            r"동작하는지|작동하는지|확인))",
            tail,
        ):
            return False
        return True
    return False


def _looks_like_validation_continuation_command(text: str) -> bool:
    """Recognize a bounded, mutation-free command to resume an active validation."""
    normalized = " ".join(str(text or "").casefold().split()).strip()
    if not normalized or "?" in normalized or _contains_explicit_mutation_request(normalized):
        return False

    if re.fullmatch(
        r"(?:yes(?:\s+please)?|ok(?:ay)?|sure|please\s+do|do\s+(?:it|that)|"
        r"use\s+(?:it|that))(?:[.!])?",
        normalized,
    ):
        return True

    english = re.sub(r"\bre[-\s]+(?=(?:run|test|check|validate|verify)\b)", "re", normalized)
    if re.fullmatch(r"[a-z0-9\s.!-]+", english):
        words = re.findall(r"[a-z0-9]+", english)
        if words and words[0] == "please":
            words = words[1:]
        allowed = {
            "continue", "resume", "proceed", "keep", "going", "go", "on", "carry",
            "pick", "up", "where", "you", "left", "off", "finish", "complete", "do",
            "rest", "take", "it", "this", "from", "here", "ahead", "with", "run",
            "rerun", "retest", "recheck", "revalidate", "reverify", "test", "check",
            "validate", "verify", "audit", "review", "inspect", "validation",
            "verification", "web", "app", "game", "browser", "page", "again", "once",
            "more", "validating", "verifying", "testing", "checking", "the", "a", "please",
            "that",
        }
        starters = {
            "continue", "resume", "proceed", "keep", "go", "carry", "pick", "finish",
            "complete", "do", "take", "run", "rerun", "retest", "recheck", "revalidate",
            "reverify", "test", "check", "validate", "verify", "audit", "review", "inspect",
        }
        if words and words[0] in starters and all(word in allowed for word in words):
            return True

    request = r"(?:줘|주세요|부탁(?:해|해요|합니다)|부탁드려(?:요)?|부탁드립니다)"
    return bool(re.fullmatch(
        rf"(?:계속\s*(?:(?:(?:진행|검증|테스트|점검)(?:해)?)|해)?\s*(?:{request})?|"
        rf"(?:검증|테스트|점검)\s*(?:계속\s*(?:진행)?|마저)\s*(?:해\s*)?(?:{request})|"
        rf"이어서\s*(?:계속\s*)?(?:해\s*)?(?:{request})|"
        rf"이어\s*가\s*(?:{request})?|재개(?:해)?\s*(?:{request})?|"
        rf"하던\s*(?:것|거)\s*(?:마저\s*)?(?:해\s*)?(?:{request})|"
        rf"멈춘\s*(?:곳|데)부터\s*(?:해\s*)?(?:{request})|"
        rf"마저\s*(?:해\s*)?(?:{request})|나머지(?:\s*(?:검증|테스트|점검))?(?:도)?\s*"
        rf"(?:해\s*)?(?:{request})|그렇게\s*(?:해\s*)?(?:{request})|"
        rf"(?:진행|끝내|완료)(?:해)?\s*(?:{request})|"
        rf"(?:재\s*(?:검증|확인)|다시\s*(?:검증|테스트|점검|확인|봐|돌려)|"
        rf"한\s*번\s*더\s*(?:검증|테스트|점검|확인|봐|돌려))(?:\s*해)?\s*(?:{request}))"
        r"(?:[.!])?",
        normalized,
    ))


def _has_mixed_positive_validation_clause(text: str) -> bool:
    """Detect a later positive validation command in a mixed question/denial request."""
    normalized = " ".join(str(text or "").casefold().split())
    english = _has_positive_validation_command(normalized)
    korean = bool(re.search(
        r"(?:[.;!?]\s*|(?:그(?:러면|리고)|이제|다음으로)\s*)"
        r"[^.;!?]{0,120}"
        r"(?:검증|테스트|점검|확인)(?:해)?\s*(?:줘|주세요|부탁)",
        normalized,
    ))
    korean = korean or bool(re.search(
        r"^(?=.+(?:검증|테스트|점검|확인)(?:해)?\s*(?:줘|주세요|부탁))"
        r".+(?:검증|테스트|점검|확인).{0,16}(?:하지\s*마|하지\s*말)",
        normalized,
    ))
    return english or korean


def _looks_like_validation_scope_change(text: str) -> bool:
    """Recognize a target change/exclusion that requires a fresh exact HTML choice."""
    normalized = " ".join(str(text or "").casefold().split())
    english_verb = r"(?:verify|validate|test|check|run|audit|review|inspect)"
    english_scope = (
        r"(?:different|another|other|except|without|but\s+not|anything\s+other\s+than|"
        r"previous|old|before)"
    )
    if re.search(
        rf"\b{english_verb}\b.{{0,60}}\b{english_scope}\b|"
        rf"\b{english_scope}\b.{{0,60}}\b{english_verb}\b",
        normalized,
    ):
        return True
    if re.fullmatch(
        r"(?:(?:the|a)\s+)?(?:other|different|previous)(?:\s+one)?|"
        r"(?:choose|select|use)\s+(?:an?\s+)?(?:other|different|previous)(?:\s+one)?|"
        r"(?:choose|select|use)\s+another(?:\s+one)?|"
        r"(?:not|except)\s+(?:this|that|the\s+previous)(?:\s+one)?|"
        r"(?:same|that|this)(?:\s+one)?",
        normalized,
    ):
        return True
    return bool(re.search(
        r"(?:검증|테스트|점검|확인).{0,40}(?:다른|제외|말고)|"
        r"(?:다른|제외|말고).{0,40}(?:검증|테스트|점검|확인)|"
        r"(?:이전|기존|예전).{0,20}(?:제외|말고|아닌)",
        normalized,
    ))


def _is_explicit_task_reset(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return bool(re.search(
        r"^(?:new\s+task|start\s+a\s+new\s+task|forget\s+(?:that|this)|"
        r"새\s*작업|새로운\s*작업|이전\s*작업\s*취소)\b",
        normalized,
    ))


def _is_validation_feature_reactivation_request(text: str) -> bool:
    """Treat an explicit verifier re-enable command as a fresh validation run."""
    normalized = " ".join(str(text or "").casefold().split())
    return bool(re.search(
        r"검증\s*기능.{0,28}(?:다시\s*)?(?:활성화|켰|켜|enable|enabled|on)|"
        r"(?:다시\s*)?(?:활성화|켰|켜).{0,28}검증\s*기능|"
        r"(?:web\s+)?validat(?:ion|or).{0,36}(?:re-?enabled|enabled|reactivated|turned\s+on)|"
        r"(?:re-?enabled|enabled|reactivated|turned\s+on).{0,36}(?:web\s+)?validat(?:ion|or)|"
        r"(?:turned|switched)\s+(?:web\s+)?validat(?:ion|or)\s+back\s+on|"
        r"(?:web\s+)?validat(?:ion|or)\s+(?:is\s+)?(?:enabled|on)\s+again|"
        r"(?:i(?:'ve|\s+have)|we(?:'ve|\s+have))\s+re-?enabled\s+"
        r"(?:the\s+)?(?:web\s+)?validat(?:ion|or)|"
        r"(?:the\s+)?(?:web\s+)?validator\s+(?:is\s+)?back\s+on|"
        r"(?:web\s+)?validation\s+(?:is\s+)?back\s+on",
        normalized,
    ))


def _has_validation_reactivation_continuation(text: str) -> bool:
    normalized = " ".join(str(text or "").casefold().split())
    return bool(
        _is_validation_feature_reactivation_request(normalized)
        and re.search(
            r"\b(?:continue|resume|proceed|carry\s+on|go\s+on)\b|"
            r"(?:계속|재개|이어서|마저)(?:\s*(?:해|진행))?",
            normalized,
        )
    )


def _is_non_browser_html_file_request(text: str) -> bool:
    """Keep source-reading/editing requests out of the browser validation harness."""
    normalized = " ".join(str(text or "").casefold().split())
    if not _html_path_mentions(normalized):
        return False
    explicit_validation = bool(re.search(
        r"(?:검증|테스트|점검|검토|브라우저)|"
        r"\b(?:verify|validate|test|check|run_web|browser|audit|inspect)\b",
        normalized,
    ))
    if explicit_validation:
        return False
    return bool(re.search(
        r"\b(?:read|open)\b.{0,160}\b(?:text\s+(?:reader|editor)|source|code|content|summari[sz]e)\b|"
        r"(?:내용|소스|코드).{0,40}(?:읽|요약|열어)|(?:읽|요약).{0,40}(?:내용|소스|코드)",
        normalized,
    ))


def _is_topic_only_web_request(text: str) -> bool:
    """Separate research/explanation topics from commands to run a local artifact."""
    normalized = " ".join(str(text or "").casefold().split())
    # A short acknowledgement belongs to the new request that follows it, not
    # to a stale HTML-candidate prompt from the previous assistant turn.
    normalized = re.sub(
        r"^(?:(?:yes|ok(?:ay)?|sure|alright|all\s+right|fine|great)[,;:.!?]?\s+)+",
        "",
        normalized,
    )
    normalized = re.sub(r"^(?:anything|something)\s+else[.!?]?\s+", "", normalized)
    topic_match = re.match(
        r"^(?:please\s+)?(?:search|research|find|explain|summari[sz]e|recommend|compare|list|"
        r"start\s+(?:a\s+)?new\s+research\s+task(?:\s+about)?|"
        r"tell\s+me\s+about|what\s+is)\b",
        normalized,
    )
    topic_lead = bool(topic_match) or bool(re.match(
        r"^(?:검색|조사|연구|설명|요약|추천|비교|목록|알려)"
        r"(?:해|해줘|해주세요|해\s*줘)?\b",
        normalized,
    ))
    if topic_match:
        topic_subject = normalized[topic_match.end():].lstrip(" ,;:-")
        # A bare pronoun or generic candidate noun still refers to the unresolved
        # HTML set. A concrete subject such as "NVIDIA news" is a fresh task.
        if re.match(
            r"^(?:(?:for|about|on|into)\s+)?(?:(?:the|all|our|your)\s+)?"
            r"(?:it|its|this|that|these|those|them|their|theirs|both|either|all|"
            r"one|ones|each|every|another|other|two|first|second|last|best|better|"
            r"whichever|selected|chosen|available|remaining|preferred|suitable|version|"
            r"selection|above|aforementioned|current|present|previous|former|latter|"
            r"candidate|candidates|option|options|choice|choices|file|files|page|pages)\b",
            topic_subject,
        ) or re.match(r"^(?:what|how|why|whether)\s+(?:it|this|that|they|these|those)\b", topic_subject):
            topic_lead = False
        else:
            subject_tokens = re.findall(r"[a-z0-9][a-z0-9_.+'’-]*", topic_subject)
            deictic_words = {
                "whichever", "selected", "chosen", "available", "remaining", "preferred",
                "suitable", "version", "selection", "above", "aforementioned", "current",
                "present", "previous", "former", "latter",
            }
            non_topic_words = {
                "a", "an", "the", "our", "your", "for", "about", "on", "into", "of", "to", "me",
                "it", "its", "this", "that", "these", "those", "them", "their", "theirs",
                "one", "ones", "both", "all", "either", "each", "every", "another", "other",
                "first", "second", "third", "last", "two", "three", "candidate", "candidates",
                "option", "options", "choice", "choices", "file", "files", "page", "pages",
                "what", "how", "why", "whether", "which", "who", "do", "does", "did", "is",
                "are", "was", "were", "be", "being", "been", "would", "should", "could", "can",
                "best", "better", "preferable", "preferred", "suitable", "ideal", "safe", "safer",
                "safest", "fast", "faster", "fastest", "more", "less", "wrong", "content",
            }
            if any(token in deictic_words for token in subject_tokens) or not any(
                token not in non_topic_words for token in subject_tokens
            ):
                topic_lead = False
    korean_topic_tail = bool(re.search(
        r"(?:도구|프레임워크|라이브러리|방법|방식|문서|자료|뉴스|정보|사례|에\s*대해)"
        r".{0,60}(?:검색|조사|연구|설명|요약|추천|비교|정리|목록|찾아|알려)"
        r"(?:해|해줘|해주세요|해\s*줘|줘|주세요)?[.!?]*$",
        normalized,
    ))
    return bool(
        (topic_lead or korean_topic_tail)
        and not _has_explicit_validation_execution_command(normalized)
        and not _contains_explicit_mutation_request(_mask_html_path_mentions(normalized))
    )


def _has_explicit_non_html_file_target(text: str) -> bool:
    """Recognize a newly named non-HTML file while an old candidate list is pending."""
    normalized = " ".join(str(text or "").casefold().split())
    if re.search(r"\bhttps?://[^\s<>'\"`]+", normalized):
        return True
    if re.search(
        r"(?:^|\s)(?:readme|license|changelog|makefile|dockerfile|procfile)"
        r"(?=$|[\s,.;:!?])",
        normalized,
    ):
        return True
    if re.search(
        r"(?:^|\s)(?:the\s+)?[a-z0-9_.-]+\s+(?:folder|directory)(?=$|[\s,.;:!?])",
        normalized,
    ):
        return True
    return bool(re.search(
        r"(?:^|[\s'\"`(\[])"
        r"[^\s'\"`<>|?*\[\]()]+\."
        r"(?!(?:html?|htm)(?=$|[\s'\"`),.;:!?\]]))[a-z][a-z0-9]{0,15}"
        r"(?=$|[\s'\"`),.;:!?\]])",
        normalized,
    ))


def _is_direct_non_html_target_request(text: str) -> bool:
    """Distinguish a new file target from a reference used to mutate an old candidate."""
    normalized = " ".join(str(text or "").casefold().split())
    if not _has_explicit_non_html_file_target(normalized):
        return False
    if not _has_candidate_reference(normalized):
        return True
    target = (
        r"(?:https?://[^\s<>'\"`]+|(?:readme|license|changelog|makefile|dockerfile|procfile)\b|"
        r"(?:the\s+)?[a-z0-9_.-]+\s+(?:folder|directory)\b|"
        r"[^\s'\"`<>|?*\[\]()]+\.(?!(?:html?|htm)\b)[a-z][a-z0-9]{0,15}\b)"
    )
    if re.match(
        rf"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
        rf"(?:{_ENGLISH_MUTATION_VERB}|read|open|summari[sz]e|inspect|analyze|show)"
        rf"\s+(?:the\s+)?{target}",
        normalized,
    ):
        return True
    return bool(re.match(
        rf"^{target}(?:을|를|은|는)?\s*"
        r"(?:수정|변경|편집|업데이트|삭제|제거|저장|이동|복사)(?:해)?\s*(?:줘|주세요)?",
        normalized,
    ))


def _is_clear_independent_task_after_candidate(text: str) -> bool:
    """Allow a complete, unrelated task to replace a pending HTML selection."""
    normalized = " ".join(str(text or "").casefold().split())
    if not normalized:
        return False
    topic_text = re.sub(
        r"^(?:(?:yes|ok(?:ay)?|sure|alright|all\s+right|fine|great)[,;:.!?]?\s+)+|"
        r"^(?:anything|something)\s+else[.!?]?\s+",
        "",
        normalized,
    )
    if re.match(
        r"^(?:please\s+)?(?:search|research|find|explain|summari[sz]e|recommend|compare|list|"
        r"tell\s+me\s+about|start\s+(?:a\s+)?new\s+research\s+task(?:\s+about)?)\b",
        topic_text,
    ) and re.search(
        r"\b(?:nvidia|nim|html|javascript|typescript|python|web|internet|news|billing|"
        r"documentation|library|libraries|framework|frameworks|api|weather|forecast|"
        r"exchange\s+rate|currency|security|monitoring|research)\b",
        topic_text,
    ):
        return True
    if re.search(
        r"^(?:please\s+)?(?:run\s+(?:the\s+)?(?:npm|pnpm|yarn|python|pytest|unit)\b|"
        r"execute\s+(?:the\s+)?(?:pytest|tests?|test\s+suite)\b|"
        r"start\s+(?:the\s+)?(?:backend|frontend|development|dev)\s+server\b|"
        r"(?:restart|launch)\s+(?:the\s+)?(?:comfyui|application|app|server)\b|"
        r"install\s+(?:the\s+)?(?:project\s+)?dependencies\b|"
        r"(?:format|refactor)\s+(?:the\s+)?(?:codebase|backend\s+service|project)\b|"
        r"(?:inspect|show|list|scan|analyze)\s+(?:the\s+)?"
        r"(?:gpu\s+usage|disk\s+space|running\s+processes|project\s+for\s+todos|latest\s+logs)\b|"
        r"commit\b.{0,80}\b(?:git|changes?|repository|repo)\b|"
        r"check\s+git\s+status\b|open\s+(?:visual\s+studio\s+code|vs\s*code)\b)",
        normalized,
    ):
        return True
    if re.search(
        r"^(?:npm|파이썬)\s*(?:테스트|test).{0,24}(?:실행|돌려)|"
        r"^(?:백엔드|프론트엔드)\s*서버.{0,16}(?:시작|재시작)|"
        r"^comfyui.{0,16}(?:다시\s*)?시작|"
        r"^(?:현재\s*)?변경\s*사항.{0,16}커밋|"
        r"^gpu\s*사용량.{0,16}(?:확인|조회)|"
        r"^(?:실행\s*중인\s*)?프로세스.{0,16}(?:목록|확인|조회)|"
        r"^디스크\s*공간.{0,16}(?:확인|조회)",
        normalized,
    ):
        return True
    if _has_candidate_reference(normalized):
        return False
    if _looks_like_image_generation_request(normalized):
        return True
    if re.search(
        r"\b(?:weather|forecast|exchange\s+rate|currency|alarm|reminder|calendar|"
        r"discord|e-?mail|message|calculate|calculation|arithmetic|time|date)\b|"
        r"(?:날씨|일기예보|환율|알람|알림|리마인더|달력|캘린더|디스코드|이메일|"
        r"메시지|계산|현재\s*시간|오늘\s*날짜)",
        normalized,
    ):
        return True
    if normalized.endswith("?") and re.match(
        r"^(?:what|how|who|where|when|why)\b",
        normalized,
    ) and re.search(
        r"\b(?:cuda|nvidia|nim|jensen\s+huang|gpu|rtx|ollama|python|typescript|"
        r"javascript|electron|react|comfyui|llm|api)\b",
        normalized,
    ):
        return True
    if normalized.endswith("?") and re.match(
        r"^who\s+is\s+[a-z0-9_.-]+['’]s\s+[a-z0-9_.-]+",
        normalized,
    ):
        return True
    if re.match(
        r"^(?:please\s+)?(?:create|build|make|write|draft|generate)\s+"
        r"(?:(?:a|an|the)\s+)?(?!it\b|this\b|that\b|one\b|file\b|page\b|work\b|changes?\b)"
        r"\S+",
        normalized,
    ):
        return True
    return bool(re.search(
        r"(?:새로\s*)?(?:게임|문서|보고서|글|소설|시|표|목록|폴더|디렉터리|프로젝트)"
        r"(?:을|를)?\s*(?:만들|작성|생성)(?:어|해)?\s*(?:줘|주세요)",
        normalized,
    ))


def _is_web_status_query(text: str) -> bool:
    """Recognize a status question about a web target without treating topics as runs."""
    normalized = " ".join(str(text or "").casefold().split())
    if not normalized or _is_topic_only_web_request(normalized):
        return False
    status_mention = bool(re.search(
        r"\b(?:work|works|working|broken|errors?|problems?|issues?|status|healthy|ready)\b|"
        r"(?:작동|동작|오류|에러|문제|상태|정상)",
        normalized,
    ))
    if not status_mention:
        return False
    interrogative = bool(
        normalized.endswith("?")
        or re.match(r"^(?:does|do|is|are|was|were|has|have|can)\b", normalized)
        or re.search(r"\b(?:whether|if)\b", normalized)
    )
    return interrogative


def _is_bounded_active_validation_reply(text: str) -> bool:
    """Keep only bounded acknowledgements/selections inside active validation."""
    normalized = " ".join(str(text or "").casefold().split())
    if not normalized:
        return False
    if _contains_explicit_mutation_request(_mask_html_path_mentions(normalized)):
        return False
    if _is_explicit_task_reset(normalized):
        return False
    if (
        _looks_like_validation_continuation_command(normalized)
        or _looks_like_validation_scope_change(normalized)
        or _positive_ordinal_selection_index(normalized) is not None
        or bool(_explicit_html_paths(normalized))
    ):
        return True
    if re.fullmatch(
        r"(?:please\s+)?(?:the\s+)?(?:former|latter)(?:\s+one)?[.!]?|"
        r"(?:please\s+)?(?:(?:use|choose|select|pick|go\s+with)\s+)?"
        r"(?:option|choice)\s*(?:[a-z]|#?\d{1,3})(?:\s+please)?[.!]?|"
        r"(?:please\s+)?(?:the\s+)?(?:first|second|third|fourth|fifth|last)\s+"
        r"(?:option|choice)(?:\s+please)?[.!]?|"
        r"#\s*[1-9]\d{0,2}[.!]?",
        normalized,
    ):
        return True
    english_reference = re.fullmatch(
        r"(?:please\s+)?(?:yes|ok(?:ay)?|sure|sounds?\s+good|looks?\s+good|fine|"
        r"alright|all\s+right)[.!]?|"
        r"(?:which\s+)?(?:validation|verification|candidate|selection|selected\s+target)"
        r"(?:\s+(?:one|target|candidate|please))?[.!?]?|"
        r"(?:anything|something)\s+else[.!?]?|"
        r"(?:all\s+of\s+(?:them|those)|both\s+of\s+them|those\s+two|every\s+one|"
        r"(?:either|any|whichever)\s+one)[.!]?|"
        r"(?:the\s+)?(?:other|different|previous|old|same|this|that|another|former|latter)"
        r"(?:\s+(?:one|target|candidate))?[.!]?|"
        r"(?:please\s+)?(?:use|choose|select|want|prefer|pick|go\s+with|validate|verify|test|check)"
        r"\s+(?:the\s+)?(?:other|different|previous|old|same|this|that|another|former|latter|"
        r"all|both|either|any|every|whichever|them|those)(?:\s+one)?(?:\s+please)?[.!]?|"
        r"(?:all|both|either|any|every|whichever|them|those)[.!]?",
        normalized,
    )
    korean_reference = re.fullmatch(
        r"(?:네|예|응|좋아|알겠어|그래)[.!]?|"
        r"(?:그렇게\s*해|그거(?:로)?|그걸로|이거(?:로)?|다른\s*(?:것|거)|"
        r"이전\s*(?:것|거)|같은\s*(?:것|거)|모두|전부|둘\s*다|아무거나|"
        r"계속|재개|마저|검증|테스트|점검|후보|선택)(?:\s*(?:해|해줘|해주세요))?[.!?]?",
        normalized,
    )
    return bool(english_reference or korean_reference)


def _looks_like_existing_web_validation_request(text: str, recent_context: str = "") -> bool:
    """기존 웹 산출물의 검증만 요청한 후속 대화를 새 제작 요청과 구분한다."""
    current = " ".join(str(text or "").casefold().split())
    raw_context = str(recent_context or "").casefold()[-6000:]
    context = " ".join(raw_context.split())[-3000:]
    previous_user_match = re.search(
        r"\[previous_user\]\s*(.*?)(?=\s*\[previous_assistant\]|$)",
        raw_context,
        re.DOTALL,
    )
    if previous_user_match:
        task_context = " ".join(previous_user_match.group(1).split())[-2000:]
    else:
        context_lines = [" ".join(line.split()) for line in raw_context.splitlines() if line.strip()]
        task_context = context_lines[-1] if context_lines else context
    previous_assistant_match = re.search(
        r"\[previous_assistant\]\s*(.*)$",
        raw_context,
        re.DOTALL,
    )
    assistant_context = (
        " ".join(previous_assistant_match.group(1).split())[-2000:]
        if previous_assistant_match
        else ""
    )
    if not current:
        return False
    if _is_non_browser_html_file_request(current):
        return False
    # Filenames such as edit.html or explain.html are path data, not English
    # mutation/explanation verbs.  Intent checks operate on a masked copy while
    # target extraction below continues to use the exact user text.
    intent_current = " ".join(_mask_html_path_mentions(current).split())

    # 실행을 원하지 않는 부정문과 기능 설명·방법·가능성 질문은 도구 실행 요청이 아니다.
    # 다만 같은 요청의 뒤 절에 실제 검증 명령이 붙으면 일반 작업 모드로 빠뜨리지 않고
    # 검증 전용의 모호한 범위로 보낸다. 그러면 경로 grant가 없으므로 도구 실행은 차단된다.
    if _is_nonexecuting_web_validation_statement(intent_current):
        return False

    current_web = bool(re.search(
        r"(?:\.html?(?![a-z0-9])|html|웹|페이지|브라우저|사이트|javascript|캔버스|canvas|"
        r"테트리스|프론트엔드|web\s*app|web\s*game|web\s*page|browser\s*page|"
        r"website|site|page|run_web)",
        intent_current,
    ))
    task_context_reactivated = _is_validation_feature_reactivation_request(task_context)
    task_context_explicit_selection = bool(_explicit_html_paths(task_context))
    task_context_ordinal_selection = _positive_ordinal_selection_index(task_context) is not None
    assistant_has_validation_state = _assistant_has_active_validation_state(assistant_context)
    assistant_has_pending_candidates = _assistant_has_pending_validation_candidates(
        assistant_context
    )
    assistant_has_web_state = bool(re.search(
        r"(?:\.html?(?![a-z0-9])|html|웹|페이지|web\s*app|web\s*game|web\s*page|"
        r"browser\s*page|website|site|page|run_web)",
        assistant_context,
    ))
    task_context_selection = bool(
        assistant_has_validation_state
        and (
            task_context_explicit_selection
            or (task_context_ordinal_selection and assistant_has_web_state)
        )
    )
    task_context_is_development = bool(re.search(
        r"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
        r"(?:implement|develop|code|fix|edit|modify|build|create|rewrite|add|optimi[sz]e|improve)\b|"
        r"\b(?:continue|resume|proceed)\b.{0,24}\b(?:editing|coding|developing|implementation)\b|"
        r"(?:개발|구현|코딩|수정|고치|만들|작성|추가|최적화|개선|보완)",
        task_context,
    ))
    context_web = bool(re.search(
        r"(?:\.html?(?![a-z0-9])|html|웹|페이지|브라우저|javascript|캔버스|canvas|"
        r"테트리스|프론트엔드|web\s*app|web\s*game|run_web)",
        task_context,
    )) or task_context_selection or task_context_reactivated
    reactivated_verifier = _is_validation_feature_reactivation_request(intent_current)
    context_validation = bool(re.search(
        r"(?:검증\s*기능|재\s*검증|검증|테스트|점검|검토|qa|audit|"
        r"verif(?:y|ying|ication)|validat(?:e|ed|ing|ion)|retest|"
        r"test(?:ing|ed)?|check(?:ing|ed)?|review(?:ing|ed)?|"
        r"inspect(?:ing|ed)?|audit(?:ing|ed)?)",
        task_context,
    ) and not task_context_is_development) or task_context_reactivated or task_context_selection

    # 중단된 검증 런의 다음 사용자 턴에는 이전 사용자 요청이 대화 문맥에 남는다. 새 작업을
    # 명시하지 않은 다양한 계속/재개 표현은 아래 mutation 검사 뒤 검증 전용 상태를 이어간다.
    continuation_requested = bool(
        context_web
        and context_validation
        and _looks_like_validation_continuation_command(current)
    )
    scope_change_requested = bool(
        context_web
        and context_validation
        and _looks_like_validation_scope_change(current)
    )
    active_validation_reply = bool(
        assistant_has_validation_state
        and context_web
        and context_validation
        and _is_bounded_active_validation_reply(current)
    )
    candidate_state_reply = bool(
        assistant_has_pending_candidates
        and not _is_explicit_task_reset(current)
        and not _is_validation_feature_reactivation_request(current)
        and not _contains_explicit_mutation_request(intent_current)
        and _is_bounded_active_validation_reply(current)
    )

    selection_requested = bool(
        context_web
        and context_validation
        and (
            len(_explicit_html_paths(current)) >= 1
            or re.fullmatch(
                r"(?:please\s+)?(?:use|choose|select|go\s+with)\s+"
                r"[a-z0-9_./\\-]+\.html?(?:\s+please)?[.!]?",
                current,
            )
            or re.fullmatch(
                r"[a-z0-9_./\\-]+\.html?(?:로|을|를)?\s*"
                r"(?:(?:선택|사용|검증)(?:해)?\s*)?(?:해\s*)?(?:줘|주세요)[.!]?",
                current,
            )
            or _positive_ordinal_selection_index(current) is not None
        )
    )

    # 최신 요청이 다른 대상을 명시하면 오래된 HTML 문맥을 끌어오지 않는다.
    if not current_web and not active_validation_reply and not candidate_state_reply and re.search(
        r"(?:\.py(?![a-z0-9])|python|파이썬|pdf|docx|엑셀|스프레드시트|"
        r"이\s*문서|해당\s*문서|데이터베이스|\bapi\b|서버)",
        intent_current,
    ):
        return False

    # 수정·구현까지 함께 지시했다면 순수 검증 요청이 아니다. 그런 요청에는 원래 작성 도구
    # 범위를 유지한다. 단, "만들어 둔/고쳐놓은" 같은 과거 완료 표현은 현재 수정 명령이 아니다.
    if (
        _contains_explicit_mutation_request(intent_current)
        or _has_additional_operation_after_validation(current)
    ):
        return False
    mutation_text = re.sub(
        r"(?:새로\s*)?(?:만들|작성|구현|추가|수정|고치|변경|바꾸|개선|최적화|보완)"
        r"(?:지(?:는|만)?\s*말고|진\s*말고|지\s*마|지\s*않고|\s*필요\s*없이?)|"
        r"\b(?:do\s+not|don't|dont)\s+"
        r"(?:create|build|implement|write|rewrite|edit|add|fix|change|modify|improve|optimi[sz]e)\b|"
        r"\bno\s+need\s+to\s+(?:create|build|implement|write|rewrite|edit|add|fix|change|modify)\b|"
        r"(?:만들어\s*(?:둔|놓은)|만든|작성(?:해\s*(?:둔|놓은)|한)|"
        r"구현(?:해\s*(?:둔|놓은)|한)|고쳐\s*놓은|고친|"
        r"수정(?:해\s*(?:둔|놓은)|한)|개선(?:해\s*(?:둔|놓은)|한)|"
        r"without\s+(?:any\s+)?(?:edit(?:s|ing)?|chang(?:e|es|ing)|modif(?:y|ying|ication|ications)))",
        "",
        _strip_no_edit_phrases(intent_current),
    )
    if re.search(
        r"(?:새로|처음부터).{0,16}(?:만들|작성|구현)|"
        r"(?:만들(?:어|고)|작성(?:해|하고|해서)|구현(?:해|하고|해서)|"
        r"추가(?:해|하고|해서)|수정(?:해|하고|해서|\s*(?:후|뒤))|"
        r"고치(?:고|거나)|고쳐|바꾸(?:고|거나)|바꿔|변경(?:해|하고|해서)|"
        r"개발(?:해|하고|해서)|개선(?:해|하고|해서)|최적화(?:해|하고|해서)|보완(?:해|하고|해서))|"
        r"\b(?:continue|resume|proceed|keep\s+going|go\s+on|carry\s+on)\b.{0,24}"
        r"\b(?:editing|coding|developing|implementation|development)\b|"
        r"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
        r"(?:create|build|implement|write|rewrite|edit|add|fix|change|modify|improve|optimi[sz]e)\b|"
        r"\b(?:and|then)\s+(?:create|build|implement|write|rewrite|edit|add|fix|change|modify|improve|optimi[sz]e)\b",
        mutation_text,
    ):
        return False
    if (
        continuation_requested
        or _has_validation_reactivation_continuation(current)
        or selection_requested
        or scope_change_requested
        or active_validation_reply
        or candidate_state_reply
    ):
        return True

    explicit_validation_paths = _explicit_html_paths(current)
    if explicit_validation_paths and re.search(
        r"(?:검증|테스트|점검|실행|확인|verify|validate|test|check|run|open|audit|review|inspect)",
        intent_current,
    ):
        return True

    # 문제를 설명하는 문장만으로 브라우저를 실행하지 않는다. 명시적 실행/검증 명령이
    # 함께 있을 때는 아래 요청 패턴이 우선하므로 정상적인 "확인해줘"는 유지된다.
    korean_diagnostic = bool(re.search(
        r"(?:안\s*(?:되|돼|하)|되지\s*않|오류|문제|버그).{0,24}(?:것|거)?\s*같|"
        r"(?:것|거)\s*같은데",
        intent_current,
    )) and not bool(re.search(
        r"(?:해\s*줘|해주세요|해줘|해봐|해\s*봐|봐\s*줘|봐줘|돌려\s*줘|열어\s*봐|"
        r"실행해\s*줘|확인해\s*줘|점검해\s*줘|검토해\s*줘)",
        intent_current,
    ))
    explicit_english_command = _has_positive_validation_command(intent_current)
    english_diagnostic = bool(re.search(
        r"\b(?:seems?|appears?)\s+(?:to\s+be\s+)?(?:broken|failing|not\s+working)|"
        r"\b(?:is|keeps?)\s+(?:broken|failing|not\s+working)|"
        r"\b(?:does\s+not|doesn't|doesnt)\s+work\b|"
        r"\b(?:test|validation|check)\s+(?:failed|fails)\b|"
        r"\b(?:bug|error|problem|issue)\b|\bfails?\b",
        intent_current,
    )) and not explicit_english_command
    if korean_diagnostic or english_diagnostic:
        return False

    verification_requested = bool(re.search(
        r"(?:재\s*(?:검증|확인)|검증|테스트|점검|검토|체크|검수|qa|audit|확인).{0,24}"
        r"(?:해\s*줘|해주세요|해줘|해봐|해\s*봐|해\s*줄래|해줄래|"
        r"해\s*줄\s*수\s*있(?:어|을까|나요)?|부탁|하자|돌려\s*줘|실행해\s*줘)|"
        r"(?:검증|테스트|점검|검토|qa|audit)\s*[.!?]*$|"
        r"(?:열어|실행해|실행\s*해|돌려|살펴|동작|작동|제대로\s*되나|버튼.{0,8}되).{0,28}"
        r"(?:확인|봐\s*줘|봐줘|봐|해\s*봐|되는지|되나)|"
        r"(?:한\s*번|문제\s*없는지|제대로\s*(?:돌아가는지|동작하는지|작동하는지)).{0,16}"
        r"(?:봐\s*줘|봐줘|확인해\s*줘|확인해줘)|"
        r"\b(?:(?:re[-\s]?)?(?:verify|validate|test|check|run)|audit|open|review|inspect)\b",
        intent_current,
    )) or explicit_english_command
    if not verification_requested:
        return False
    if reactivated_verifier or current_web:
        return True

    continuity = bool(re.search(
        r"(?:재\s*(?:검증|확인)|다시|기존|아까|그거|해당|방금|한번|한\s*번|이어서|"
        r"그대로|문제\s*없는지|제대로\s*(?:돌아가는지|동작하는지|작동하는지))",
        current,
    ))
    return context_web and continuity


def _looks_like_guarded_web_validation_turn(text: str, recent_context: str = "") -> bool:
    """Enter the least-privilege web-validation schema even for meta/denial turns."""
    if _is_non_browser_html_file_request(text):
        return False
    normalized = " ".join(str(text or "").casefold().split())
    if not normalized:
        return False
    intent_text = " ".join(_mask_html_path_mentions(normalized).split())
    if _has_trailing_validation_cancellation(intent_text):
        # "파일은 수정하되 검증은 하지 마"는 작성 요청이며, run_web만 철회한
        # 것이다. 수정 명령까지 검증 후보 선택 상태로 가두지 않는다.
        return not _contains_explicit_mutation_request(intent_text)
    assistant_match = re.search(
        r"\[previous_assistant\]\s*(.*)$",
        str(recent_context or "").casefold(),
        re.DOTALL,
    )
    assistant_context = assistant_match.group(1) if assistant_match else ""
    assistant_has_pending_candidates = _assistant_has_pending_validation_candidates(
        assistant_context
    )
    assistant_has_active_validation = _assistant_has_active_validation_state(
        assistant_context
    )
    if (
        (assistant_has_pending_candidates or assistant_has_active_validation)
        and _is_standalone_validation_rejection(normalized)
    ):
        return True
    if assistant_has_pending_candidates and not _is_explicit_task_reset(normalized):
        if not _html_path_tokens(normalized) and (
            _is_direct_non_html_target_request(normalized)
            or _is_clear_independent_task_after_candidate(normalized)
        ):
            return False
        if not _html_path_tokens(normalized):
            # The assistant explicitly asked the user to resolve multiple HTML
            # candidates. Any otherwise-unresolved follow-up remains read-only.
            return True
    if _is_topic_only_web_request(intent_text):
        return False
    if _looks_like_existing_web_validation_request(text, recent_context):
        return True
    if _is_clear_multi_html_authoring_request(text):
        return False
    if _has_ambiguous_validation_target_reference(text):
        return True
    if (
        assistant_has_pending_candidates
        and not _is_explicit_task_reset(normalized)
        and not _html_path_tokens(normalized)
        and not _has_explicit_non_html_file_target(normalized)
        and _has_candidate_reference(normalized)
    ):
        return True
    if _contains_explicit_mutation_request(intent_text):
        if (
            assistant_has_pending_candidates
            and _is_ambiguous_candidate_mutation_reply(normalized)
        ):
            return True
        return _has_ambiguous_validation_target_for_mutation(text)
    if _has_additional_operation_after_validation(normalized):
        return False
    if (
        assistant_has_pending_candidates
        and not _is_explicit_task_reset(normalized)
        and _is_bounded_active_validation_reply(normalized)
    ):
        return True
    current_web = bool(re.search(
        r"(?:\.html?(?![a-z0-9])|html|웹|페이지|브라우저|사이트|javascript|캔버스|canvas|"
        r"프론트엔드|web\s*app|web\s*game|web\s*page|browser\s*page|website|site|page|run_web)",
        normalized,
    ))
    validation_mention = bool(re.search(
        r"(?:검증|재\s*검증|테스트|점검|검토|검수|체크|확인|실행)|"
        r"\b(?:validation|verification|re[-\s]?(?:verify|validate|test|check|run)|"
        r"verify|validate|test|check|run|open|audit|review|inspect|validating|verifying|"
        r"testing|checking|running|auditing|reviewing|inspecting)\b",
        intent_text,
    ))
    status_query = _is_web_status_query(intent_text)
    html_target_mentioned = bool(_html_path_mentions(normalized))
    return bool(
        current_web and (validation_mention or status_query)
        or (html_target_mentioned and status_query)
    )


def _has_explicit_validation_execution_command(text: str) -> bool:
    """Recognize only language that directly asks Aiso to execute validation."""
    intent_text = " ".join(_mask_html_path_mentions(str(text or "").casefold()).split())
    if _is_bare_validation_question(intent_text) or _has_trailing_validation_cancellation(intent_text):
        return False
    if _has_positive_validation_command(intent_text):
        return True
    return bool(re.search(
        r"(?:재\s*(?:검증|확인)|검증|테스트|점검|검토|체크|검수|확인|실행)"
        r"(?:만)?\s*(?:한\s*번\s*)?(?:해)?\s*"
        r"(?:줘|주세요|해\s*줘|해주세요|해줘|해봐|해\s*봐|해\s*줄래|해줄래|"
        r"해\s*줄\s*수\s*있(?:어|을까|나요)?|봐\s*줘|봐줘|봐|부탁|하자|"
        r"돌려\s*줘|실행해\s*줘)|"
        r"(?:열어|돌려|살펴)\s*(?:봐|줘|주세요|봐\s*줘|봐줘)|"
        r"(?:한\s*번|문제\s*없는지|제대로\s*(?:돌아가는지|동작하는지|작동하는지))"
        r".{0,20}(?:봐\s*줘|봐줘|봐|확인해\s*줘|확인해줘|확인)|"
        r"(?:검증|테스트|점검|검토|qa|audit)\s*[.!?]*$",
        intent_text,
    ))


def _bounded_image_selection_context(text: str) -> str:
    """긴 사용자 요청도 이미지 모델 선택용 앞·뒤 문맥을 제한 안에서 보존한다."""
    cleaned = text.replace("\x00", "")
    if len(cleaned) <= MAX_PROMPT_LENGTH:
        return cleaned
    marker = "\n…\n"
    available = MAX_PROMPT_LENGTH - len(marker)
    head = available // 2
    return f"{cleaned[:head]}{marker}{cleaned[-(available - head):]}"


def _is_image_generation_input_error(error: GenerationError) -> bool:
    """LLM이 노출된 generate_image 인자를 바꿔 복구할 수 있는 오류인지 구분한다."""
    return error.kind == "input"


def _is_retryable_image_generation_error(error: GenerationError) -> bool:
    """생성 계층이 제출 전이라고 증명한 전송 오류만 한 번 재시도한다."""
    # ``retryable``만으로는 충분하지 않다. 실행 후 받은 SeedError 같은 terminal
    # 오류가 호출부 실수로 retryable=True를 갖더라도 새 prompt를 재제출하면 안 된다.
    # generation 계층은 제출 전 연결 오류에만 kind="transport"를 지정한다.
    return error.kind == "transport" and error.retryable is True


_IMAGE_COMPLETION_COPY: dict[str, dict[str, str]] = {
    "ko": {
        "header": "이미지 생성을 완료했습니다. 결과 카드에서 이미지와 실제 ComfyUI 노드 워크플로를 확인할 수 있습니다.",
        "result": "결과 {index}",
        "model": "모델",
        "prompt": "실제 프롬프트",
        "remaining": "그 밖의 결과 {count}개는 결과 카드에서 확인할 수 있습니다.",
        "unavailable": "이미지 생성 도구가 완료되지 않아 결과 이미지를 표시할 수 없습니다. 오류 안내를 확인해 주세요.",
    },
    "en": {
        "header": "Image generation is complete. You can inspect the image and the actual ComfyUI node workflow in the result card.",
        "result": "Result {index}", "model": "Model", "prompt": "Actual prompt",
        "remaining": "The other {count} results are available in the result cards.",
        "unavailable": "The image-generation tool did not complete, so an image cannot be shown. Check the error details.",
    },
    "ja": {
        "header": "画像生成が完了しました。結果カードで画像と実際の ComfyUI ノードワークフローを確認できます。",
        "result": "結果 {index}", "model": "モデル", "prompt": "実際のプロンプト",
        "remaining": "残り {count} 件の結果は結果カードで確認できます。",
        "unavailable": "画像生成ツールが完了しなかったため、結果画像を表示できません。エラー案内を確認してください。",
    },
    "zh": {
        "header": "图像生成已完成。您可以在结果卡中查看图像和实际的 ComfyUI 节点工作流。",
        "result": "结果 {index}", "model": "模型", "prompt": "实际提示词",
        "remaining": "其余 {count} 个结果可在结果卡中查看。",
        "unavailable": "图像生成工具未完成，无法显示结果图像。请查看错误说明。",
    },
    "es": {
        "header": "La generación de imágenes ha terminado. Puedes revisar la imagen y el flujo de nodos real de ComfyUI en la tarjeta de resultados.",
        "result": "Resultado {index}", "model": "Modelo", "prompt": "Prompt real",
        "remaining": "Los otros {count} resultados están disponibles en las tarjetas de resultados.",
        "unavailable": "La herramienta de generación de imágenes no se completó, por lo que no se puede mostrar el resultado. Consulta el error.",
    },
    "fr": {
        "header": "La génération d'image est terminée. Vous pouvez consulter l'image et le workflow réel des nœuds ComfyUI dans la carte de résultat.",
        "result": "Résultat {index}", "model": "Modèle", "prompt": "Prompt réel",
        "remaining": "Les {count} autres résultats sont disponibles dans les cartes de résultat.",
        "unavailable": "L'outil de génération d'image ne s'est pas terminé ; le résultat ne peut pas être affiché. Consultez l'erreur.",
    },
    "de": {
        "header": "Die Bildgenerierung ist abgeschlossen. Bild und tatsächlichen ComfyUI-Node-Workflow finden Sie in der Ergebniskarte.",
        "result": "Ergebnis {index}", "model": "Modell", "prompt": "Tatsächlicher Prompt",
        "remaining": "Die übrigen {count} Ergebnisse finden Sie in den Ergebniskarten.",
        "unavailable": "Das Bildgenerierungswerkzeug wurde nicht abgeschlossen; das Ergebnis kann nicht angezeigt werden. Prüfen Sie den Fehler.",
    },
    "pt": {
        "header": "A geração de imagem foi concluída. Você pode ver a imagem e o fluxo real de nós do ComfyUI no cartão de resultado.",
        "result": "Resultado {index}", "model": "Modelo", "prompt": "Prompt efetivo",
        "remaining": "Os outros {count} resultados estão disponíveis nos cartões de resultado.",
        "unavailable": "A ferramenta de geração de imagem não foi concluída; não é possível mostrar o resultado. Consulte o erro.",
    },
    "it": {
        "header": "La generazione dell'immagine è completata. Puoi controllare l'immagine e il workflow reale dei nodi ComfyUI nella scheda del risultato.",
        "result": "Risultato {index}", "model": "Modello", "prompt": "Prompt effettivo",
        "remaining": "Gli altri {count} risultati sono disponibili nelle schede dei risultati.",
        "unavailable": "Lo strumento di generazione dell'immagine non è stato completato; il risultato non può essere mostrato. Controlla l'errore.",
    },
    "ru": {
        "header": "Генерация изображения завершена. Изображение и фактический рабочий процесс узлов ComfyUI доступны в карточке результата.",
        "result": "Результат {index}", "model": "Модель", "prompt": "Фактический промпт",
        "remaining": "Остальные {count} результатов доступны в карточках результатов.",
        "unavailable": "Инструмент генерации изображения не завершил работу, поэтому результат нельзя показать. Проверьте ошибку.",
    },
    "ar": {
        "header": "اكتمل إنشاء الصورة. يمكنك الاطلاع على الصورة ومسار عقد ComfyUI الفعلي في بطاقة النتيجة.",
        "result": "النتيجة {index}", "model": "النموذج", "prompt": "المطالبة الفعلية",
        "remaining": "تتوفر النتائج الأخرى وعددها {count} في بطاقات النتائج.",
        "unavailable": "لم تكتمل أداة إنشاء الصور، لذلك لا يمكن عرض النتيجة. راجع تفاصيل الخطأ.",
    },
    "he": {
        "header": "יצירת התמונה הושלמה. ניתן לבדוק את התמונה ואת תהליך הצמתים בפועל של ComfyUI בכרטיס התוצאה.",
        "result": "תוצאה {index}", "model": "מודל", "prompt": "הנחיה בפועל",
        "remaining": "{count} התוצאות הנוספות זמינות בכרטיסי התוצאות.",
        "unavailable": "כלי יצירת התמונות לא הושלם, ולכן לא ניתן להציג את התוצאה. בדוק את פרטי השגיאה.",
    },
    "hi": {
        "header": "चित्र निर्माण पूरा हो गया है। परिणाम कार्ड में चित्र और वास्तविक ComfyUI नोड वर्कफ़्लो देखें।",
        "result": "परिणाम {index}", "model": "मॉडल", "prompt": "वास्तविक प्रॉम्प्ट",
        "remaining": "अन्य {count} परिणाम कार्डों में उपलब्ध हैं।",
        "unavailable": "चित्र निर्माण उपकरण पूरा नहीं हुआ, इसलिए परिणाम नहीं दिखाया जा सकता। त्रुटि विवरण देखें।",
    },
    "th": {
        "header": "สร้างภาพเสร็จแล้ว คุณสามารถดูภาพและเวิร์กโฟลว์โหนด ComfyUI จริงได้ในบัตรผลลัพธ์",
        "result": "ผลลัพธ์ {index}", "model": "โมเดล", "prompt": "พรอมป์จริง",
        "remaining": "ผลลัพธ์อื่นอีก {count} รายการดูได้ในบัตรผลลัพธ์",
        "unavailable": "เครื่องมือสร้างภาพทำงานไม่เสร็จ จึงไม่สามารถแสดงผลลัพธ์ได้ โปรดตรวจสอบข้อผิดพลาด",
    },
}


def _image_completion_text(images: list[dict], response_language: str | None = "ko") -> str:
    """결과 카드와 다음 대화 양쪽에 남길 검증된 최소 생성 문맥."""
    copy = _IMAGE_COMPLETION_COPY.get(normalize_response_language(response_language), _IMAGE_COMPLETION_COPY["en"])
    header = copy["header"]
    lines: list[str] = [header]
    for index, image in enumerate(images[:4], start=1):
        profile = _markdown_safe_plain_text(
            str(image.get("profileName") or image.get("modelName") or "등록 모델")
        )
        seed = _markdown_safe_plain_text(str(image.get("seed") or "알 수 없음"))
        width = image.get("width")
        height = image.get("height")
        size = f", 크기 {width}x{height}" if isinstance(width, int) and isinstance(height, int) else ""
        prefix = f"{copy['result'].format(index=index)}: " if len(images) > 1 else ""
        lines.append(f"{prefix}{copy['model']} {profile}, seed {seed}{size}")
        prompt = image.get("effectivePrompt") or image.get("prompt")
        if isinstance(prompt, str) and prompt.strip():
            clean_prompt = " ".join(prompt.split())
            limit = 800 if len(images) == 1 else 400
            if len(clean_prompt) > limit:
                clean_prompt = clean_prompt[:limit - 3] + "…"
            prompt_prefix = (
                f"{copy['result'].format(index=index)} {copy['prompt']}: "
                if len(images) > 1
                else f"{copy['prompt']}: "
            )
            lines.append(f"{prompt_prefix}{_markdown_safe_plain_text(clean_prompt)}")
    if len(images) > 4:
        lines.append(copy["remaining"].format(count=len(images) - 4))
    return "\n".join(lines)


def _safe_image_turn_text(text: str, response_language: str | None = "ko") -> str:
    """이미지 요청 응답에서는 로컬 모델이 지어낸 외부 결과 링크를 표시하지 않는다."""
    decoded = html.unescape(text).casefold()
    if (
        any(marker in decoded for marker in ("![", "http://", "https://", "www."))
        or re.search(r"\[[^\]]*\]\s*(?:\(|\[)", decoded)
        or re.search(r"\b[a-z][a-z0-9+.-]*://", decoded)
        or re.search(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", decoded)
    ):
        copy = _IMAGE_COMPLETION_COPY.get(normalize_response_language(response_language), _IMAGE_COMPLETION_COPY["en"])
        return copy["unavailable"]
    return text


def _safe_unverified_image_completion_text(text: str, response_language: str | None = "ko") -> str:
    """Reject completion-looking prose unless this run emitted ``image_result``.

    Small models occasionally invent the exact Aiso completion header, a seed,
    and an effective prompt while no image tool was exposed.  This boundary is
    intentionally independent of prompt wording: tool result events, not
    natural-language claims, are the source of truth for generated media.
    """
    decoded = html.unescape(text).casefold()
    copies = tuple(copy["header"].casefold() for copy in _IMAGE_COMPLETION_COPY.values())
    korean_claim = (
        "이미지 생성" in decoded
        and "완료" in decoded
        and ("결과 카드" in decoded or "실제 프롬프트" in decoded or "seed" in decoded)
    )
    english_claim = (
        "image generation" in decoded
        and any(marker in decoded for marker in ("complete", "completed", "finished"))
        and any(marker in decoded for marker in ("result card", "actual prompt", "seed"))
    )
    if any(header in decoded for header in copies) or korean_claim or english_claim:
        copy = _IMAGE_COMPLETION_COPY.get(
            normalize_response_language(response_language), _IMAGE_COMPLETION_COPY["en"]
        )
        return copy["unavailable"]
    return text


def _nvidia_image_error_result(*, input_error: bool = False) -> str:
    """Provider-visible/ledger image errors never include local registry or workflow detail."""
    return (
        "[오류] 이미지 생성 입력이 허용 범위에 맞지 않습니다."
        if input_error
        else "[오류] 로컬 이미지 생성이 실패했습니다."
    )


# Compatibility exports for the extracted prompt-policy module.
_markdown_safe_plain_text = prompting.markdown_safe_plain_text
SYSTEM_PROMPT = prompting.SYSTEM_PROMPT
_operational_tool_policy_prompt = prompting.operational_tool_policy_prompt
_programming_policy_prompt = prompting.programming_policy_prompt
_skill_policy_prompt = prompting.skill_policy_prompt
_discord_policy_prompt = prompting.discord_policy_prompt
_exact_tool_scope_prompt = prompting.exact_tool_scope_prompt
_final_response_language_prompt = prompting.final_response_language_prompt
# 승인 대기 레지스트리 (단일 프로세스 asyncio 기준)
_approvals = ApprovalRegistry()


def resolve_approval(key: str, approved: bool) -> bool:
    return _approvals.resolve(key, approved)


_release_llm_for_image = execution._release_llm_for_image


_chat_turn = execution._chat_turn


_parse_args = execution._parse_args


# Compatibility exports: deterministic validation primitives now live in
# agent_validation. These private names remain stable for main.py and tests.
_html_entry_path = validation.html_entry_path
_web_validation_policy_key = validation.web_validation_policy_key
_safe_relative_effect_path = validation.safe_relative_effect_path
_relative_tool_effect_paths = validation.relative_tool_effect_paths
_relative_tool_effect_path = validation.relative_tool_effect_path
_display_path_key = validation.display_path_key
_workspace_paths_match = validation.workspace_paths_match
_workspace_effect_covers_path = validation.workspace_effect_covers_path
_workspace_file_fingerprint = validation.workspace_file_fingerprint
_non_html_file_tokens = validation.non_html_file_tokens
_request_explicitly_preserves_path = validation.request_explicitly_preserves_path
_request_directly_mutates_dependency_path = validation.request_directly_mutates_dependency_path
def _html_path_mentions(text: str) -> list[str]:
    """Extract HTML-looking path text before validating its workspace scope."""
    mentions: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        mention = str(raw or "").strip().rstrip(",;!?。")
        if mention.endswith("."):
            mention = mention[:-1]
        wrapper_pairs = {"`": "`", '"': '"', "'": "'", "(": ")", "[": "]", "{": "}", "<": ">"}
        while len(mention) >= 2 and wrapper_pairs.get(mention[0]) == mention[-1]:
            mention = mention[1:-1].strip()
        if not re.search(r"\.html?", mention, re.IGNORECASE):
            return
        # Korean particles are prose outside the filename, matching the prior
        # ASCII tokenizer behavior while allowing Unicode Windows filenames.
        particle_match = re.fullmatch(
            r"(?P<path>.+\.html?)(?:을|를|은|는|로|만|과|와|도|에|에서|의)?",
            mention,
            re.IGNORECASE,
        )
        if particle_match:
            mention = particle_match.group("path")
        if mention in seen:
            return
        seen.add(mention)
        mentions.append(mention)

    source = str(text or "")
    remainder = list(source)
    # Paths containing spaces must be quoted.  Extract those spans before the
    # ordinary token pass, and preserve punctuation inside the filename.
    def extract_quoted(opener: str, closer: str) -> None:
        cursor = 0
        while cursor < len(source):
            start = source.find(opener, cursor)
            if start < 0:
                break
            end = source.find(closer, start + len(opener))
            while end >= 0:
                body = source[start + len(opener):end]
                if "\n" in body or "\r" in body:
                    break
                if re.search(r"\.html?", body, re.IGNORECASE):
                    add(body)
                    for index in range(start, end + len(closer)):
                        remainder[index] = " "
                    cursor = end + len(closer)
                    break
                end = source.find(closer, end + len(closer))
            else:
                cursor = start + len(opener)
                continue
            if end < 0 or "\n" in source[start + len(opener):end] or "\r" in source[start + len(opener):end]:
                cursor = start + len(opener)

    for opener, closer in (("`", "`"), ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        extract_quoted(opener, closer)

    for opener, closer in (("(", ")"), ("[", "]"), ("{", "}"), ("<", ">")):
        stack: list[int] = []
        for index, character in enumerate(source):
            if character == opener:
                stack.append(index)
            elif character == closer and stack:
                start = stack.pop()
                if not stack:
                    body = source[start + 1:index]
                    if "\n" not in body and "\r" not in body and re.search(
                        r"\.html?", body, re.IGNORECASE
                    ):
                        add(body)
                        for offset in range(start, index + 1):
                            remainder[offset] = " "
    for raw_token in re.findall(r"\S+", "".join(remainder)):
        add(raw_token)
    return mentions


def _mask_html_path_mentions(text: str) -> str:
    """Hide filename words from natural-language intent classifiers."""
    masked = str(text or "")
    for mention in sorted(_html_path_mentions(masked), key=len, reverse=True):
        masked = re.sub(re.escape(mention), " __html_path__ ", masked, flags=re.IGNORECASE)
    return masked


def _invalid_html_path_mentions(text: str) -> list[str]:
    """Return HTML-looking mentions that are not valid workspace-relative targets."""
    return [
        mention for mention in _html_path_mentions(text)
        if _html_entry_path({"path": mention}) is None
    ]


def _html_path_tokens(text: str) -> list[str]:
    """Extract all syntactically valid relative HTML tokens, without granting access."""
    tokens: list[str] = []
    seen: set[str] = set()
    for mention in _html_path_mentions(text):
        target = _html_entry_path({"path": mention})
        if target is None or target[0] in seen:
            continue
        seen.add(target[0])
        tokens.append(target[1])
    return tokens


def _explicit_html_paths(text: str) -> list[str]:
    """Return only positively selected HTML paths from a user instruction.

    Merely mentioning a path is not authorization.  Free-form exclusions such
    as "avoid/except/private" are open-ended, so ambiguous path sentences fail
    closed.  Multiple paths are accepted only by a narrow all-target validation
    grammar; a clean follow-up selection can authorize one path on the next turn.
    """
    raw_text = str(text or "").strip()
    if _is_bare_validation_question(raw_text) or _has_trailing_validation_cancellation(raw_text):
        return []
    paths = _html_path_tokens(raw_text)
    if not paths:
        return []
    normalized = " ".join(raw_text.casefold().split())
    grammar_text = _mask_html_path_mentions(normalized)
    if _is_validation_feature_reactivation_request(normalized):
        grammar_text = re.sub(
            r"(?:web\s+)?validat(?:ion|or)\s+(?:is\s+)?"
            r"(?:re-?enabled|enabled|reactivated|turned\s+on|on\s+again|back\s+on)|"
            r"(?:(?:i|we)(?:'ve|\s+have)?\s+)?re-?enabled\s+(?:the\s+)?"
            r"(?:web\s+)?validat(?:ion|or)|"
            r"(?:re-?enabled|enabled|reactivated)\s+(?:web\s+)?validat(?:ion|or)|"
            r"(?:turned|switched)\s+(?:web\s+)?validat(?:ion|or)\s+back\s+on|"
            r"(?:the\s+)?(?:web\s+)?validator\s+(?:is\s+)?back\s+on|"
            r"(?:web\s+)?validat(?:ion|or)\s+(?:is\s+)?(?:enabled|on)\s+again|"
            r"검증\s*기능.{0,24}(?:다시\s*)?"
            r"(?:활성화(?:했어|했어요|했습니다|해|함|됨|됐어|되었어)?|켰어|켰어요|켜|enable|enabled)|"
            r"(?:다시\s*)?(?:활성화(?:했어|했어요|했습니다|해|함|됨|됐어|되었어)?|"
            r"켰어|켰어요|켜).{0,24}검증\s*기능",
            " ",
            grammar_text,
        )
        grammar_text = re.sub(r"^[\s,;:.!?-]+|[\s,;:.!?-]+$", "", grammar_text)
    grammar_text = re.sub(
        r"[`\"'“‘\(\[\{<]\s*__html_path__\s*[`\"'”’\)\]\}>]",
        " __html_path__ ",
        grammar_text,
    )
    grammar_text = " ".join(grammar_text.split())
    # Masking deliberately inserts spaces so filenames cannot merge with
    # surrounding prose.  Restore only the punctuation and Korean-particle
    # adjacency required by the narrow authorization grammar.
    grammar_text = re.sub(r"\s+([.,;:!?])", r"\1", grammar_text)
    grammar_text = re.sub(
        r"__html_path__\s+(?=(?:을|를|은|는|로|만|과|와|도|에|에서|의)(?:\s|$|[.!?]))",
        "__html_path__",
        grammar_text,
    )
    path_rx = r"__html_path__"
    path_ref = path_rx
    pure_selection = bool(
        re.fullmatch(rf"(?:the\s+)?{path_ref}(?:\s+file)?(?:\s+please)?[.!]?", grammar_text)
        or re.fullmatch(rf"i\s+want\s+{path_ref}[.!]?", grammar_text)
        or re.fullmatch(
            rf"(?:please\s+)?(?:use|choose|select|go\s+with)\s+{path_ref}"
            rf"(?:\s+please)?[.!]?",
            grammar_text,
        )
        or re.fullmatch(
            rf"{path_ref}(?:로|을|를)?\s*(?:(?:선택|사용)(?:해)?\s*)?"
            rf"(?:해\s*)?(?:줘|주세요)?[.!]?",
            grammar_text,
        )
    )
    if pure_selection:
        return paths
    if _contains_explicit_mutation_request(grammar_text):
        return []
    grant_scope_text = _strip_no_edit_phrases(grammar_text)
    if re.search(
        r"\b(?:no|nothing|neither|except|without|not|never)\b|"
        r"\banything\s+(?:other\s+than|except)\b",
        grant_scope_text,
    ):
        return []

    if len(paths) > 1:
        english_path_list = (
            rf"(?:both\s+{path_rx}\s+(?:and|and\s+also)\s+{path_rx}|"
            rf"{path_rx}\s+(?:and|and\s+also)\s+{path_rx}|"
            rf"{path_rx}(?:\s*,\s*{path_rx})+(?:\s*,?\s*and\s+{path_rx})?)"
        )
        english_all = re.fullmatch(
            rf"{_ENGLISH_VALIDATION_REQUEST_PREFIX}"
            rf"(?:verify|validate|test|check|run|audit|review|inspect)\s+"
            rf"{english_path_list}(?:\s+please)?[.!]?",
            grammar_text,
        )
        korean_all = re.fullmatch(
            rf"{path_rx}(?:\s*(?:,|과|와|및)\s*{path_rx})+"
            rf"(?:을|를)?\s*(?:(?:모두|둘\s*다)\s*)?(?:검증|테스트|점검|실행)(?:해)?\s*"
            rf"(?:줘|주세요)?[.!]?",
            grammar_text,
        )
        return paths if english_all or korean_all else []

    english_no_edit_suffix = (
        r"(?:\s*(?:;|,)?\s*(?:"
        r"without\s+(?:any\s+)?(?:changes?|edits?|editing(?:\s+it)?|modifications?)|"
        r"without\s+(?:changing\s+anything|making\s+(?:any\s+)?changes?)|"
        rf"(?:and|but)\s+{_ENGLISH_MUTATION_VERB}\s+"
        r"(?:nothing|no\s+changes?|zero\s+changes?)|"
        r"(?:but\s+)?(?:do\s+not|don't|dont)\s+"
        r"(?:edit|change|modify|rewrite)(?:\s+it)?))?"
    )
    english_validation_verb = (
        r"(?:(?:re[-\s]?)?(?:verify|validate|test|check|run)|open|audit|review|inspect|"
        r"(?:re)?verifying|(?:re)?validating|(?:re)?testing|(?:re)?checking|"
        r"(?:re)?running|auditing|reviewing|inspecting)"
    )
    english_target = (
        rf"(?:(?:(?:the|existing|current|latest)\s+)*{path_ref}(?:\s+file)?|"
        rf"(?:the\s+)?[a-z0-9_-]+(?:\s+[a-z0-9_-]+){{0,8}}\s+in\s+{path_ref})"
    )
    english_validation = re.fullmatch(
        rf"{_ENGLISH_VALIDATION_REQUEST_PREFIX}(?:(?:only|just)\s+)?"
        rf"{english_validation_verb}\s+(?:(?:only|just)\s+)?{english_target}"
        rf"(?:\s+only)?(?:\s+again)?{english_no_edit_suffix}(?:\s+please)?[.!?]?",
        grammar_text,
    )
    korean_no_edit = (
        r"(?:(?:수정|변경|편집)(?:하지\s*말고|\s*없이)|"
        r"건드리지\s*말고|원본(?:을|를)?\s*유지하고)"
    )
    korean_validation = re.fullmatch(
        rf"(?:(?:수정한|기존|현재|만들어\s*둔|고쳐\s*놓은)\s*)?{path_ref}"
        rf"(?:(?:\s*파일)?\s*만|(?:을|를|은|는|로))?\s*"
        rf"(?:{korean_no_edit}\s*)?(?:좀\s*)?(?:다시\s*)?"
        rf"(?:(?:검증|테스트|점검|실행|확인)(?:만)?\s*(?:한\s*번\s*)?(?:해)?\s*"
        rf"(?:줘|주세요|봐|줄래|줄\s*수\s*있(?:어|을까|나요)?|"
        rf"부탁해|부탁드려요|부탁드립니다)?|"
        rf"(?:브라우저에서\s*)?(?:열어|돌려)\s*(?:봐|줘|주세요))[.!?]?",
        grammar_text,
    )
    return paths if english_validation or korean_validation else []


def _assistant_has_active_validation_state(text: str) -> bool:
    """Recognize a validation run state without trusting generic dev/test prose."""
    normalized = " ".join(str(text or "").casefold().split())
    if not normalized:
        return False
    active_state = bool(re.search(
        r"(?:검증(?:을|이)?\s*(?:대상|후보|진행|실행|시작|계속|완료|결과|중단|승인)|"
        r"html\s*후보|후보.{0,40}\.html?|run_web|candidates?.{0,60}\.html?|"
        r"\.html?.{0,40}(?:candidate|target|validat(?:e|ed|ing|ion)|"
        r"verif(?:y|ied|ying|ication)|tested|checked)|"
        r"(?:validat(?:e|ed|ing)|verif(?:y|ied|ying)|test(?:ed|ing)|check(?:ed|ing)).{0,60}\.html?|"
        r"(?:validation|verification|testing|checking).{0,80}(?:approval|required|started?|continue|"
        r"pending|stopped|interrupted|in\s+progress|underway|complete|result))",
        normalized,
    ))
    if not active_state:
        return False

    # A status-led sentence may describe the already-created target ("fixed a.html")
    # or say validation is pending after editing.  In that structure validation,
    # not development, is the active operation.
    status_led = bool(re.search(
        r"^(?:(?:validation|verification)\s+of\s+.{0,100}\.html?|"
        r"(?:testing|checking)\s+(?:the\s+)?"
        r"(?:(?:edited|fixed|modified|created|generated|built|existing|current|latest)\s+)*"
        r"[a-z0-9_./\\-]+\.html?)"
        r".{0,120}\b(?:pending|interrupted|stopped|in\s+progress|underway|required)\b",
        normalized,
    ))
    if status_led:
        return True

    # Reassurance such as "I did not edit a.html" is not an active development
    # operation.  Remove only explicit no-change clauses before checking whether
    # the assistant was still authoring rather than validating.
    development_text = _mask_html_path_mentions(_strip_no_edit_phrases(normalized))
    if re.search(
        r"\b(?:implement(?:ing|ed)?|develop(?:ing|ed)?|edit(?:ing|ed)?|coding|"
        r"(?:write|writing|written|wrote|rewrite|rewriting|rewritten|rewrote)|"
        r"fix(?:ing|ed)?|modif(?:y|ying|ied)|(?:build|building|built|rebuild|rebuilding|rebuilt)|"
        r"creat(?:e|ing|ed)|(?:make|making|made)|generat(?:e|ing|ed)|"
        r"add(?:ing|ed)?|chang(?:e|ing|ed)|improv(?:e|ing|ed)|updat(?:e|ing|ed)|"
        r"refactor(?:ing|ed)?|repair(?:ing|ed)?|patch(?:ing|ed)?|replac(?:e|ing|ed)|"
        r"delet(?:e|ing|ed)|remov(?:e|ing|ed)|renam(?:e|ing|ed)|mov(?:e|ing|ed)|"
        r"optimi[sz](?:e|ing|ed)|unit\s+tests?|test\s+harness)\b|"
        r"(?:개발|구현|코딩|수정|편집|고치|작성|만들|추가|변경|바꾸|최적화|개선|보완|"
        r"업데이트|리팩터|리팩토|수리|패치|교체|대체|삭제|제거|이동|복사)",
        development_text,
    ):
        return False
    return True


def _assistant_has_pending_validation_candidates(text: str) -> bool:
    """Recognize an unresolved candidate-selection turn as harness state."""
    normalized = " ".join(str(text or "").casefold().split())
    if not normalized:
        return False
    paths = _html_path_tokens(normalized)
    # Candidate state is authoritative only when the assistant actually named
    # an HTML path.  Incidental prose such as "HTML parser" or "run_web
    # integration" must not capture a later Python/image task.
    if not paths:
        return False
    if len(paths) > 1 and re.search(r"candidate|option|후보|선택지|select|choose|선택|고르", normalized):
        return True
    return bool(re.search(
        r"(?:candidates?|options?).{0,120}(?:select|choose|pick|required|pending)|"
        r"(?:select|choose|pick).{0,120}(?:candidate|option)|"
        r"(?:후보|선택지).{0,120}(?:선택|고르|지정|필요|대기)|"
        r"(?:선택|고르|지정).{0,120}(?:후보|선택지)",
        normalized,
    ))


def _positive_ordinal_selection_index(text: str) -> int | None:
    """Parse only a whole-sentence positive candidate choice."""
    normalized = " ".join(str(text or "").casefold().split())
    english = re.fullmatch(
        r"(?:please\s+)?(?:(?:use|choose|select|go\s+with)\s+)?(?:the\s+)?"
        r"(?P<ordinal>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|"
        r"tenth|last|[1-9]\d{0,2}(?:st|nd|rd|th))(?:\s+one)?"
        r"(?:\s+please)?[.!]?",
        normalized,
    )
    if english:
        value = english.group("ordinal")
        words = {
            "first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4,
            "sixth": 5, "seventh": 6, "eighth": 7, "ninth": 8, "tenth": 9,
            "last": -1,
        }
        if value in words:
            return words[value]
        # words 에 없다는 건 위 ordinal 패턴의 숫자 가지([1-9]\d{0,2}(st|nd|rd|th))로
        # 잡혔다는 뜻이다 — 따라서 선두 숫자 매치는 반드시 성립한다.
        digits = re.match(r"\d+", value)
        assert digits is not None
        return int(digits.group()) - 1

    korean = re.fullmatch(
        r"(?:(?P<word>첫|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*번째|"
        r"(?P<number>[1-9]\d{0,2})\s*(?:번|번째))"
        r"(?:\s*(?:것|거))?(?:로|으로)?\s*(?:해\s*)?(?:줘|주세요)?[.!]?",
        normalized,
    )
    if not korean:
        return None
    if korean.group("number"):
        return int(korean.group("number")) - 1
    return {
        "첫": 0, "두": 1, "세": 2, "네": 3, "다섯": 4,
        "여섯": 5, "일곱": 6, "여덟": 7, "아홉": 8, "열": 9,
    }[korean.group("word")]


def _followup_html_selection_paths(
    text: str,
    recent_context: str,
    inventory: list[str] | None,
) -> list[str]:
    """Never re-resolve a cross-turn ordinal without a persisted snapshot id."""
    # The displayed candidate list belongs to a previous run.  A fresh scan may
    # have a different order, so it cannot safely preserve what "first" meant.
    _ = (text, recent_context, inventory)
    return []


# Compatibility exports for result formatting and validation state keys.
_validation_target_map = validation.validation_target_map
_authoritative_html_inventory_result = validation.authoritative_html_inventory_result
_provider_safe_web_validation_result = validation.provider_safe_web_validation_result
_web_validation_status = validation.web_validation_status
_unverified_html_notice = validation.unverified_html_notice
_existing_web_validation_notice = validation.existing_web_validation_notice
_normalize_tool_calls = execution._normalize_tool_calls


_reindexing: set[str] = set()  # 진행 중인 워크스페이스 (중복 방지)
_bg_tasks: set = set()          # 백그라운드 태스크 강참조(GC 방지)


def _fire_reindex(root: Path, host: str) -> None:
    """색인 최신화를 백그라운드로 던진다 — 응답(done)을 막지 않는다.

    색인은 '다음 런의 시작'에서만 쓰이므로 임계 경로에 있을 필요가 없다. 임베딩 시간이
    사용자 체감 완료를 지연시키지 않게 detached task로 실행한다. 색인이 이미 있을 때만.
    """
    key = str(root)
    if key in _reindexing:
        return  # 이미 이 워크스페이스 재색인 중 → 중복 방지
    try:
        st = rag_status(root)
    except Exception:  # noqa: BLE001
        return
    model = st.get("embed_model")
    if not st.get("indexed") or not model:
        return

    async def _bg() -> None:
        try:
            async for _ev in build_index(root, host, model):
                pass
        except Exception:  # noqa: BLE001 — 재색인 실패는 조용히
            pass
        finally:
            _reindexing.discard(key)

    try:
        task = asyncio.create_task(_bg())
        _reindexing.add(key)
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
    except RuntimeError:  # 실행 중 루프 없음(이론상) → 무시
        pass


def _maybe_reindex(
    root: Path | None,
    host: str,
    dirty: bool,
    rag_available: bool,
    state: dict[str, Any] | None = None,
) -> None:
    """파일이 바뀌었고 색인이 있으면 백그라운드 재색인을 던진다.

    이 함수를 부르는 곳은 두 종류다.
      1) 루프 안의 개별 done 지점 — 재색인을 되도록 일찍 시작해 다음 런을 빠르게 한다.
      2) run_agent의 finally 백스톱 — 1)을 빠뜨린 경로까지 반드시 덮는다.

    done yield는 50곳이 넘고 그중 일부만 1)을 호출한다. 예전 주석은 "모든 종료 경로가
    이 한 곳을 거친다"고 했지만 사실이 아니었고, 그래서 가드 중단 같은 경로에서 색인이
    조용히 낡았다. 이제 그 보장은 주석이 아니라 2)의 finally가 만든다.

    ``state``(런 단위 cleanup_state)를 주면 한 런에서 한 번만 발화한다. 1)이 이미 던진
    뒤 백그라운드 재색인이 먼저 끝나버리면 _fire_reindex의 _reindexing 중복 방지가
    풀리므로, 그 창을 이 플래그로 닫는다.
    """
    # 작업 폴더 없는 런(WORKSPACE_FREE 도구만 쓰는 경우)에서도 종료 경로는 이 함수를
    # 거친다. rag_available 이 그때 거짓이라 지금까지 무해했지만, 그건 두 값 사이의
    # 암묵 관계였다. root 를 직접 보고 끊어 관계를 코드로 적는다.
    if root is None or not (dirty and rag_available):
        return
    if state is not None:
        if state.get("reindex_fired"):
            return
        state["reindex_fired"] = True
    _fire_reindex(root, host)


async def _generate_turn(
    host: str,
    base: LlmRequest,
    reasoning_effort: str,
    model_runtime: LlmModelRuntime,
    offload_noticed: bool,
    runtime: LlmRuntime | None = None,
    *,
    strict_tool_protocol: bool = False,
) -> AsyncGenerator[dict, None]:
    """Compatibility seam preserving monkeypatchable chat-turn injection."""
    async for event in execution._generate_turn(
        host,
        base,
        reasoning_effort,
        model_runtime,
        offload_noticed,
        runtime,
        strict_tool_protocol=strict_tool_protocol,
        chat_turn=_chat_turn,
    ):
        yield event


_prepare_model = execution._prepare_model


async def _run_agent_impl(
    *,
    host: str,
    workspace: str,
    model: str,
    messages: list[dict],
    reasoning_effort: str = "medium",
    temperature: float = 0.7,
    context_length: int = 16384,
    approval_mode: str = "read",
    session_id: str = "",
    conversation_id: str = "",
    rag_enabled: bool = True,
    rag_top_k: int = 5,
    keep_alive: str = "30m",
    comfy_base_url: str | None = None,
    comfy_profiles: list[dict] | None = None,
    comfy_selection_mode: str = "auto",
    selected_comfy_model_id: str | None = None,
    provider: str = "ollama",
    runtime: LlmRuntime | None = None,
    assistant_turn_id: str = "",
    execution_ledger: AgentExecutionLedger | None = None,
    nvidia_allowed_tools: list[str] | None = None,
    enabled_tools: list[str] | None = None,
    user_request_text: str | None = None,
    image_context_verified: bool = False,
    # 위임 대상(agent_runner._run_agent_impl)은 str 만 받는다. None 을 허용하면
    # normalize_response_language(None)=="en" 이 되어 한국어 폴백이 조용히 사라진다.
    response_language: str = "ko",
    _cleanup_state: dict[str, Any] | None = None,
) -> AsyncGenerator[dict, None]:
    """Compatibility facade for the extracted agent orchestration loop."""
    # 예전에는 여기서 runner.bind_dependencies(globals())로 이 모듈의 전역 107개를
    # 런 시작마다 러너의 globals()에 복사했다. 이제 러너가 `import agent as deps`로
    # 직접 읽으므로 주입이 필요 없다 — 정적 분석이 가능해지고, 호출 시점 속성 조회라
    # monkeypatch 시임은 그대로 동작한다.
    implementation_stream = runner._run_agent_impl(
        host=host,
        workspace=workspace,
        model=model,
        messages=messages,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        context_length=context_length,
        approval_mode=approval_mode,
        session_id=session_id,
        conversation_id=conversation_id,
        rag_enabled=rag_enabled,
        rag_top_k=rag_top_k,
        keep_alive=keep_alive,
        comfy_base_url=comfy_base_url,
        comfy_profiles=comfy_profiles,
        comfy_selection_mode=comfy_selection_mode,
        selected_comfy_model_id=selected_comfy_model_id,
        provider=provider,
        runtime=runtime,
        assistant_turn_id=assistant_turn_id,
        execution_ledger=execution_ledger,
        nvidia_allowed_tools=nvidia_allowed_tools,
        enabled_tools=enabled_tools,
        user_request_text=user_request_text,
        image_context_verified=image_context_verified,
        response_language=response_language,
        _cleanup_state=_cleanup_state,
    )
    completed_normally = False
    try:
        async for event in implementation_stream:
            yield event
        completed_normally = True
    finally:
        if not completed_normally:
            await implementation_stream.aclose()


async def run_agent(
    *,
    host: str,
    workspace: str,
    model: str,
    messages: list[dict],
    reasoning_effort: str = "medium",
    temperature: float = 0.7,
    context_length: int = 16384,
    approval_mode: str = "read",
    session_id: str = "",
    conversation_id: str = "",
    rag_enabled: bool = True,
    rag_top_k: int = 5,
    keep_alive: str = "30m",
    comfy_base_url: str | None = None,
    comfy_profiles: list[dict] | None = None,
    comfy_selection_mode: str = "auto",
    selected_comfy_model_id: str | None = None,
    provider: str = "ollama",
    runtime: LlmRuntime | None = None,
    assistant_turn_id: str = "",
    execution_ledger: AgentExecutionLedger | None = None,
    nvidia_allowed_tools: list[str] | None = None,
    enabled_tools: list[str] | None = None,
    user_request_text: str | None = None,
    image_context_verified: bool = False,
    response_language: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Agent 스트림의 공통 정리 경계.

    정상 종료 경로는 구현부가 즉시 재색인하고, 소비자 중지·취소·예기치 못한 예외로
    구현부가 끝까지 실행되지 못한 경우에는 여기서 변경 파일의 색인을 보정한다.
    """
    cleanup_state: dict[str, Any] = {}
    raw_user_request = str(user_request_text or "").strip()
    if not raw_user_request:
        raw_user_request = next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if str(message.get("role") or "") == "user"
            ),
            "",
        )
    selected_response_language = (
        normalize_response_language(response_language)
        if response_language is not None
        else response_language_from_messages(messages, fallback="ko")
    )
    completed_normally = False
    implementation_stream = _run_agent_impl(
        host=host,
        workspace=workspace,
        model=model,
        messages=messages,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        context_length=context_length,
        approval_mode=approval_mode,
        session_id=session_id,
        conversation_id=conversation_id,
        rag_enabled=rag_enabled,
        rag_top_k=rag_top_k,
        keep_alive=keep_alive,
        comfy_base_url=comfy_base_url,
        comfy_profiles=comfy_profiles,
        comfy_selection_mode=comfy_selection_mode,
        selected_comfy_model_id=selected_comfy_model_id,
        provider=provider,
        runtime=runtime,
        assistant_turn_id=assistant_turn_id,
        execution_ledger=execution_ledger,
        nvidia_allowed_tools=nvidia_allowed_tools,
        enabled_tools=enabled_tools,
        user_request_text=raw_user_request,
        image_context_verified=image_context_verified,
        response_language=selected_response_language,
        _cleanup_state=cleanup_state,
    )
    try:
        async for event in implementation_stream:
            yield event
        completed_normally = True
    finally:
        if not completed_normally:
            await implementation_stream.aclose()
        # 정상 종료에도 반드시 통과하는 유일한 지점. 예전에는 이 백스톱이
        # `not completed_normally` 안에 갇혀 있어서, done을 내고 정상 종료한 경로 중
        # 루프 안에서 _maybe_reindex를 부르지 않은 것들은 재색인 없이 끝났다.
        # _maybe_reindex가 cleanup_state로 1회 발화를 보장하므로 중복 색인은 없다.
        cleanup_root = cleanup_state.get("root")
        if isinstance(cleanup_root, Path):
            _maybe_reindex(
                cleanup_root,
                host,
                cleanup_state.get("dirty") is True,
                cleanup_state.get("rag_available") is True,
                cleanup_state,
            )


# ── 리서치 채팅 (web_search + web_fetch만) ──────────────────────────────────
# 일반 채팅에서 '웹 검색'을 켜면 이 루프로 흐른다. 파일 툴 없이 인터넷 조사 도구만 태워,
# 모르는 걸 여러 출처로 폭넓게 조사한 뒤 종합해 답하게 한다. 에이전트 하네스의 스트리밍/
# 오프로드/파싱재생성(_generate_turn)과 툴 디스패치(REGISTRY)를 그대로 재사용한다.

MAX_RESEARCH_STEPS = 16  # 모델 턴(각 턴은 여러 검색·읽기를 한 번에 낼 수 있음) 상한
RESEARCH_TOOL_NAMES = ("web_search", "web_fetch")
# 검색 직후 하네스가 상위 결과 '원문'을 자동으로 읽어들인다. 작은 모델이 1개만 읽고 마는
# 문제를 없애고, 여러 출처를 실제로 정독해 근거를 넓히기 위함(사용자 요청: 원문 전체 정독·보고).
AUTO_FETCH_TOP = 3       # 검색 1회당 자동으로 원문을 읽을 상위 결과 수
AUTO_FETCH_BUDGET = 6    # 한 런에서 자동 원문 읽기 총 상한(지연·토큰 폭주 방지)
# 자동 정독분은 페이지당 이만큼으로 발췌한다. 원문 전체(최대 3만자)×여러 개는 num_ctx(기본 16k토큰)에
# 안 들어가 compact_convo가 통째로 잘라버려 오히려 모델이 못 읽는다. 발췌하면 3개가 실제로 들어가
# 모델이 여러 출처를 종합할 수 있다(스니펫보다 20배 이상 많은 본문).
AUTO_FETCH_CHARS = 7000


RESEARCH_SYSTEM_PROMPT = research.RESEARCH_SYSTEM_PROMPT


async def run_research_chat(
    *,
    host: str,
    model: str,
    messages: list[dict],
    reasoning_effort: str = "medium",
    temperature: float = 0.7,
    context_length: int = 16384,
    keep_alive: str = "30m",
    runtime: LlmRuntime | None = None,
    strict_tool_protocol: bool = False,
    response_language: str | None = None,
) -> AsyncGenerator[dict, None]:
    """Run the bounded web-research mode through the extracted state machine."""
    research_stream = research.run_research_chat(
        host=host,
        model=model,
        messages=messages,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        context_length=context_length,
        keep_alive=keep_alive,
        runtime=runtime,
        strict_tool_protocol=strict_tool_protocol,
        registry=REGISTRY,
        compact_conversation=compact_convo,
        build_conversation=_build_model_conversation,
        prepare_model=_prepare_model,
        generate_turn=_generate_turn,
        parse_args=_parse_args,
        request_factory=LlmRequest,
        execute_tool=execute,
        tool_error=ToolError,
        tools_unsupported_kind=LlmFailureKind.TOOLS_UNSUPPORTED,
        max_gen_tokens=MAX_GEN_TOKENS,
        stall_repeat=STALL_REPEAT,
        response_language=(
            normalize_response_language(response_language)
            if response_language is not None
            else response_language_from_messages(messages, fallback="ko")
        ),
    )
    completed_normally = False
    try:
        async for event in research_stream:
            yield event
        completed_normally = True
    finally:
        if not completed_normally:
            await research_stream.aclose()
