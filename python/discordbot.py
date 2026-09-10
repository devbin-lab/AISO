# -*- coding: utf-8 -*-
"""Discord 봇 — 채팅 + 서버 구성. 거의 무설정: 토큰만 넣고 서버에 초대하면 나머지는 자동.

자동화(사용자 입력 최소화):
- 소유자: 봇 토큰이 식별하는 애플리케이션의 제작자(=당신)를 application_info로 자동 판별.
- 단일 서버: 이미 한 서버에 있으면 새 초대는 자동 퇴장(최대 1개).
- 명령 채널: 초대 시 소유자+봇만 보이는 잠금 채널(#aiso)을 자동 생성하고 그 채널에서만 대화.
- 허용 사용자: 디스코드 슬래시 커맨드(/allow, 소유자 전용)로 동적 관리·영속.
- 서버 구성(팀 카테고리·채널 생성/변경/삭제)은 자연어로 요청.

보안: 도구는 서버 구성(discordops — 채널 생성·변경·삭제)뿐이라 코드 실행(RCE) 표면이 없다.
서버를 바꾸는 작업은 검증(#aiso 보호·개수 상한) 후 소유자 승인 버튼을 통과해야만 적용된다.
봇 토큰은 이 프로세스 메모리에만 둔다. 소유자 미판별·비지정채널·비허용자는 전부 무시(fail-closed).
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import io
import json
import os
import re
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands
from agent_prompting import final_response_language_prompt
from response_language import normalize_response_language, response_language_from_messages

# 전체 응답 텍스트를 돌려주는 생성 함수(main.py가 Ollama 기계를 재사용해 주입).
GenerateFn = Callable[[list], Awaitable[str]]
# 한 턴 생성 — (messages, tools|None) → {"content": str, "tool_calls": list}. 서버 구성 루프용.
StepFn = Callable[[list, "list | None"], Awaitable[dict]]
# 웹 조사 기반 생성(리서치 루프) — 브리핑 예약이 발화 시각에 내용을 만들 때 사용.
# The second argument keeps the original user-request language separate from attachment text.
ResearchFn = Callable[[list, str | None], Awaitable[str]]
ImageFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

DISCORD_MSG_LIMIT = 2000       # 디스코드 단일 메시지 길이 상한
HISTORY_TURNS = 12             # 채널별 최근 대화 유지(간단한 문맥)
COMMAND_CHANNEL = "aiso"       # 자동 생성할 명령 채널 이름
# 동시에 붙을 수 있는 서버 수 상한. 서버마다 명령 채널·허용목록·대화 문맥을 들고 있으므로
# 무제한이면 메모리와 API 호출이 함께 늘어난다. 개인용 봇에 필요한 수를 넉넉히 잡았다.
MAX_GUILDS = 10
STATE_FILE = "state.json"      # data_dir 안에 봇 동적 상태 영속
MAX_TOOL_TURNS = 6             # 서버 구성 루프의 생성 턴 상한(폭주 방지)
APPROVAL_TIMEOUT_S = 120       # 서버 구성 승인 버튼 대기 시간(초) — 지나면 자동 취소

def base_system_prompt(response_language: str | None = "ko") -> str:
    """Return model-facing Discord policy with a request-scoped output language."""
    language = normalize_response_language(response_language)
    return (
        "You are Aiso, a helpful assistant running locally on the user's PC. "
        "Be concise, accurate, and friendly. Do not claim an action succeeded unless a tool result confirms it.\n\n"
        + final_response_language_prompt(language)
    )



_EXPLICIT_WEB_RESEARCH_RE = re.compile(
    r"(?:인터넷|웹).{0,40}(?:검색|조사|확인)|"
    r"(?:검색|조사|찾아|알아)\s*(?:봐|줘|해줘|해주세요|해서)|"
    r"(?:근거|출처)\s*(?:도|를|를\s*포함해)?\s*(?:알려|제시|확인)|"
    r"\b(?:search|research|look\s+up|browse|verify\s+online)\b",
    re.IGNORECASE,
)
_TIME_SENSITIVE_RE = re.compile(
    r"(?:최신|최근|오늘|어제|이번\s*(?:주|달|분기|연도)|뉴스|소식|속보|업데이트|"
    r"사용량\s*초기화|초기화\s*(?:날짜|일시|시간)|가격|요금|정책|버전|출시|"
    r"날씨|환율|주가|대표|최고경영자|CEO|latest|recent|today|current|news|price|"
    r"pricing|release|schedule|weather)",
    re.IGNORECASE,
)


def requires_web_research(text: str) -> bool:
    """Whether a Discord request needs current, external evidence."""
    value = str(text or "").strip()
    return bool(value and (_EXPLICIT_WEB_RESEARCH_RE.search(value) or _TIME_SENSITIVE_RE.search(value)))


def _chat_route(text: str, *, can_research: bool, can_use_tools: bool) -> str:
    if requires_web_research(text):
        return "research" if can_research else "research_unavailable"
    return "tools" if can_use_tools else "generate"


_DELIVERY_CHANNEL_RE = re.compile(
    r"(?:#\s*)?([0-9A-Za-z가-힣_.-]{1,100})\s*(?:채널|방)\s*(?:에(?:다(?:가)?)?|으로)\s*"
    r"(?:알려|보내|전송|올려|게시|공지)|"
    r"#\s*([0-9A-Za-z가-힣_.-]{1,100})\s*(?:에|으로)\s*(?:알려|보내|전송|올려|게시)",
    re.IGNORECASE,
)


def requested_delivery_channel(text: str) -> str:
    match = _DELIVERY_CHANNEL_RE.search(str(text or ""))
    if not match:
        return ""
    return str(match.group(1) or match.group(2) or "").strip()


def _research_failed(reply: str) -> bool:
    value = str(reply or "").lstrip()
    return value.startswith(("(웹 조사 실패", "(브리핑 생성 실패", "(빈 브리핑"))


async def _research_chat(
    channel,
    text: str,
    messages: list[dict],
    *,
    response_language: str | None = None,
) -> str:
    """Run the research callback without allowing attachment text to choose the reply language."""
    if _S.research is None:
        return "(웹 조사 기능을 사용할 수 없어 최신 정보를 확인하지 못했습니다. 근거 없이 답하지 않았습니다.)"
    language = normalize_response_language(
        response_language
        or response_language_from_messages([{"role": "user", "content": text}], fallback="ko")
    )
    research_messages = [item for item in messages if item.get("role") != "system"]
    async with _S.gen_lock:
        reply = await _S.research(research_messages, language)
    delivery_channel = requested_delivery_channel(text)
    if delivery_channel and not _research_failed(reply):
        delivery_result = await _send_with_approval(
            channel,
            {"channel": delivery_channel, "message": reply},
        )
        if language == "ko":
            return f"웹 조사를 완료했습니다. {delivery_result}"
        return f"Research is complete. {delivery_result}"
    return reply


@functools.lru_cache(maxsize=32)
def _tools_prompt(image_enabled: bool = False, response_language: str = "ko") -> str:
    """Build the model-only Discord tool policy.

    ``response_language`` is deliberately part of the cache key: a Korean request must not
    inherit an English final-answer instruction (or vice versa) from a previous request.
    """
    import discordops  # noqa: PLC0415 — 설계 기준·형식은 discordops가 단일 출처

    image_guide = (
        "\nImage generation: when the user clearly asks for an image, illustration, or drawing, call "
        "generate_image. Automatically select only a registered ComfyUI model that allows Agent use. "
        "Never construct an arbitrary node graph or download an external model. Put the requested scene, "
        "composition, and style into the prompt precisely.\n"
        if image_enabled
        else ""
    )
    return base_system_prompt(response_language) + (
        "\n\nDiscord server tools are available. When the user asks to change this server's channel layout "
        "(create team categories or channels, rename, move, set topics, or delete), first call "
        "discord_server_map to inspect the current structure, then call discord_server_apply(ops=[...]) with "
        "the proposed operations. Once the plan is ready, do not ask for textual confirmation: call "
        "discord_server_apply immediately. That call automatically shows the user a preview with approve/cancel "
        "buttons. The same rule applies to deletion; if the user already made the request explicit, do not ask "
        "again in text because the approval button is the final confirmation. Never include #aiso, the protected "
        "command channel, in a delete or modification list. Do not use tools for ordinary conversation.\n"
        "Send a message with discord_send(channel, message). Register a schedule with "
        "discord_schedule_add(channel, text, when, repeat, kind): when is HH:MM or YYYY-MM-DD HH:MM, repeat is "
        "once or daily, and kind is message (fixed text) or briefing (generate current information by web research "
        "at the scheduled time, for example weather or news). Use discord_schedule_list to list schedules and "
        "discord_schedule_remove(id) to delete one. Schedules run only while the app is open. Sending and "
        "scheduling also show the owner an approval button automatically; when channel, content, and time are known, "
        "call discord_send or discord_schedule_add instead of asking \"shall I?\". Ask exactly one clarifying "
        "question only when necessary. Use once when repetition is unclear. Interpret time expressions in the "
        "user's language as 24-hour local time.\n\n"
        "Register a recurring channel-conversation report with "
        "discord_channel_report_add(channels, report_channel, interval_hours, instruction). Summarize only new "
        "messages posted after registration, and never include a successfully reported message again.\n\n"
        "Register a recurring git repository report with "
        "discord_repo_report_add(repo_path, branch, report_channel, interval_hours, instruction). Use the folder "
        "path the user gave, verbatim - never invent or complete a path. Only commits made after registration "
        "are reported, and commit content is limited to messages and file/line counts.\n\n"
        + image_guide
        + discordops.DESIGN_GUIDE + "\n\n"
        + "Every ops entry MUST use exactly these field names (aliases such as op, parent, and type are not accepted): "
        '{"action":"create_category","name":"..."}, '
        '{"action":"create_text_channel","name":"...","category":"...","topic":"..."}, '
        '{"action":"create_voice_channel","name":"...","category":"..."}, '
        '{"action":"rename","target":"...","new_name":"..."}, '
        '{"action":"move","target":"...","category":"..."}, '
        '{"action":"delete","target":"..."}.'
    )


def chunk_message(text: str, n: int = DISCORD_MSG_LIMIT) -> list[str]:
    text = (text or "").strip() or "(빈 응답)"
    return [text[i : i + n] for i in range(0, len(text), n)]


def is_authorized(owner_id: str, command_channel_id: str, allowlist, author_id, channel_id) -> bool:
    """소유자 또는 허용목록 사용자가 '명령 채널'에서 보낸 메시지만 처리한다.

    스노플레이크 ID는 discord.py에서 int, 저장은 str이므로 양쪽을 str로 정규화해 비교한다.
    소유자 미판별이면 전면 거부(fail-closed)."""
    owner = str(owner_id or "").strip()
    if not owner:
        return False
    ch = str(command_channel_id or "").strip()
    if not ch or str(channel_id) != ch:
        return False
    allow = {str(x).strip() for x in (allowlist or ())}
    return str(author_id) == owner or str(author_id) in allow


class GuildState:
    """봇이 붙어 있는 서버 하나의 상태.

    명령 채널과 허용목록이 서버마다 따로 있어야 한다. 하나로 합치면 A 서버에서 허용한
    사용자가 B 서버의 명령 채널에서도 말을 걸 수 있다 — 서버는 서로 남이다.
    """

    def __init__(self, name: str = "", channel_id: str = "", allowlist: "set[str] | None" = None) -> None:
        self.name = name
        self.channel_id = channel_id
        self.allowlist: set[str] = set(allowlist or ())

    def to_json(self) -> dict:
        return {"name": self.name, "channel_id": self.channel_id, "allowlist": sorted(self.allowlist)}


class _State:
    def __init__(self) -> None:
        self.client: "discord.Client | None" = None
        self.task: "asyncio.Task | None" = None
        self.generate: GenerateFn | None = None
        self.step: StepFn | None = None    # 툴콜 지원 한 턴 생성(서버 구성 루프용)
        self.research: ResearchFn | None = None  # 웹 조사 생성(브리핑 예약용)
        self.image: ImageFn | None = None
        self.allow_attachment_images: bool = False
        # 실제로 봇에 주입된 공급자·모델. 저장된 설정이 아니라 **지금 돌고 있는 값**이다.
        # 둘이 어긋날 수 있어서(설정 저장 실패·전용 동의 흐름 등) 저장 파일만 봐서는
        # "로컬로 도는 건지 NVIDIA로 도는 건지" 확인할 길이 없었다.
        self.provider: str = ""
        self.model: str = ""
        self.sched_task: "asyncio.Task | None" = None  # 예약 러너
        self.tree: "app_commands.CommandTree | None" = None
        self.data_dir: str = ""
        # 런타임에 자동 판별/관리되는 동적 상태
        self.owner_id: str = ""          # application_info 제작자
        self.app_id: str = ""            # 봇 애플리케이션 ID(=봇 user id) — 초대 링크 생성용
        # 서버마다 따로 관리한다. 예전에는 봇이 한 서버에만 붙을 수 있어 값이 하나씩이었고,
        # 두 번째 초대는 그냥 퇴장시켰다. 서버끼리 명령 채널·허용목록이 섞이면 한쪽 서버의
        # 사용자가 다른 서버를 조작할 수 있으므로, 격리는 기능이 아니라 안전 요건이다.
        self.guilds: "dict[str, GuildState]" = {}
        # 슬래시 커맨드를 올린 길드들. 서버마다 한 번씩 올려야 /allow 가 뜬다.
        self.synced_guilds: set[str] = set()
        self.history: "dict[int, deque]" = defaultdict(lambda: deque(maxlen=HISTORY_TURNS))
        self.gen_lock = asyncio.Lock()
        self.last_error: str | None = None


_S = _State()


def is_running() -> bool:
    return _S.client is not None and not _S.client.is_closed()


def status() -> dict:
    c = _S.client
    user = str(c.user) if (c and c.user) else None
    return {
        "running": is_running(),
        "user": user,
        "owner_id": _S.owner_id,
        "app_id": _S.app_id,
        # 붙어 있는 서버 전부. 첫 서버의 값을 guild_id/channel_id 로도 실어 두어
        # 서버가 하나뿐인 기존 화면·도구가 그대로 동작한다.
        "guilds": [
            {
                "guild_id": gid,
                "guild_name": g.name,
                "channel_id": g.channel_id,
                "allowlist": sorted(g.allowlist),
            }
            for gid, g in _S.guilds.items()
        ],
        "guild_id": next(iter(_S.guilds), ""),
        "guild_name": next((g.name for g in _S.guilds.values()), ""),
        "channel_id": next((g.channel_id for g in _S.guilds.values()), ""),
        "allowlist": sorted({uid for g in _S.guilds.values() for uid in g.allowlist}),
        "provider": _S.provider,
        "model": _S.model,
        "attachment_images": _S.allow_attachment_images,
        "comfy_image_generation": _S.image is not None,
        "last_error": _S.last_error,
    }


def text_channels() -> list[dict]:
    """붙어 있는 서버마다 글 채널 목록 — 설정 탭의 보고 채널 선택을 채운다.

    봇이 실제로 볼 수 있는 채널만 담는다. 목록에 없는 채널을 고를 수 없게 만드는 것이
    목적이다 — 예약은 사람이 보지 않는 동안 돌기 때문에, 보낼 수 없는 채널로 등록되면
    발화 시각마다 조용히 실패하고 아무도 그 사실을 모른다.
    """
    client = _S.client
    if not is_running() or client is None:
        return []
    out: list[dict] = []
    for gid, state in _S.guilds.items():
        guild = client.get_guild(int(gid)) if str(gid).isdigit() else None
        if guild is None:
            continue
        out.append({
            "guild_id": str(gid),
            "guild_name": guild.name,
            "channels": [
                {"id": str(ch.id), "name": ch.name}
                for ch in guild.channels
                if isinstance(ch, discord.TextChannel)
            ],
            "command_channel_id": state.channel_id,
        })
    return out


async def register_repo_report(
    *, repo_path: str, branch: str, guild_id: str, channel_id: str,
    interval_hours, instruction: str = "",
) -> "tuple[dict | None, str | None]":
    """설정 탭에서 저장소 보고를 등록한다. 반환 (등록된 잡, 오류).

    디스코드 채널로 들어오는 등록은 소유자에게 승인 버튼을 물어본다 — 말을 건 사람이
    앱 주인인지 알 수 없기 때문이다. 여기서는 앱 창 앞의 사람이 직접 폴더와 브랜치와
    채널을 고르고 등록을 눌렀으므로 그 행위 자체가 승인이다. 대신 검증은 똑같이 거친다:
    저장소가 열리는지, **브랜치가 실제로 있는지**, 채널이 봇에게 보이는지.

    브랜치 확인이 특히 중요하다. 없는 ref 로 등록되면 발화할 때마다 실패하는데, 이
    기능은 새 커밋이 없을 때 침묵하는 것이 정상이라 밖에서는 구분이 되지 않는다.
    """
    import discordsched  # noqa: PLC0415
    import gitreport  # noqa: PLC0415

    if not is_running():
        return None, "디스코드 봇을 먼저 연결해 주세요."
    guild = bound_guild(str(guild_id or ""))
    if guild is None:
        return None, "보고를 보낼 서버를 찾지 못했습니다."
    channel = guild.get_channel(int(channel_id)) if str(channel_id or "").isdigit() else None
    if not isinstance(channel, discord.TextChannel):
        return None, "보고를 보낼 글 채널을 찾지 못했습니다."

    try:
        # fetch 하지 않는다 — 브랜치 목록을 뽑을 때 이미 받아 왔고, 등록 버튼을 누른
        # 사람을 네트워크 대기로 붙잡아 둘 이유가 없다.
        if branch == gitreport.ALL_BRANCHES:
            baseline = await gitreport.collect_all_branches(repo_path, {}, fetch=False)
        else:
            baseline = await gitreport.collect(repo_path, branch, "", fetch=False)
    except gitreport.GitReportError as error:
        return None, str(error)

    # 이름을 error 로 두지 않는다 — 위 except 절이 쓴 이름이라 파이썬이 절을 벗어나며
    # 지운다. 같은 이름을 다시 쓰면 사람 눈에는 멀쩡해도 삭제된 변수를 읽는 코드가 된다.
    draft, refusal = discordsched.build_repo_report_job(
        repo_path=repo_path,
        branch=branch,
        report_channel_id=str(channel.id),
        report_channel_name=channel.name,
        interval_hours=interval_hours,
        instruction=instruction,
        head=baseline.head,
        cursors=baseline.ref_heads,
        guild_id=str(guild.id),
    )
    if refusal:
        return None, refusal
    assert draft is not None
    return discordsched.commit_job(draft), None


# 지금 처리 중인 요청이 어느 서버에서 왔는지. 도구 실행 경로가 여기서 대상을 읽는다.
#
# contextvar 를 쓰는 이유: 메시지 처리는 서버마다 동시에 돌 수 있어서, 전역 변수 하나에
# 담으면 A 서버의 요청이 B 서버를 건드릴 수 있다. contextvar 는 async 작업마다 따로 산다.
_CURRENT_GUILD: "contextvars.ContextVar[str]" = contextvars.ContextVar("aiso_discord_guild", default="")


def guild_state(guild_id: str) -> "GuildState":
    """서버 상태를 얻는다. 없으면 만들어 둔다 — 조회가 곧 등록이 되지 않게 저장은 호출부가 한다."""
    key = str(guild_id)
    state = _S.guilds.get(key)
    if state is None:
        state = GuildState()
        _S.guilds[key] = state
    return state


def current_guild_id() -> str:
    """처리 중인 요청의 서버. 지정되지 않았고 서버가 하나뿐이면 그 서버로 본다."""
    explicit = _CURRENT_GUILD.get()
    if explicit:
        return explicit
    return next(iter(_S.guilds), "") if len(_S.guilds) == 1 else ""


def bound_guild(guild_id: str = ""):
    """라이브 길드 객체 — 없으면 None. (discordops 가 서버 구성에 사용)

    인자가 없으면 지금 처리 중인 요청의 서버를 쓴다. 서버가 여럿인데 문맥이 없으면
    None 을 준다 — 아무 서버나 골라 조작하는 것이 가장 나쁜 실패다.
    """
    target = str(guild_id) or current_guild_id()
    if not is_running() or not target.isdigit():
        return None
    assert _S.client is not None  # is_running()이 곧 client 존재 검사다
    return _S.client.get_guild(int(target))


def command_channel_id(guild_id: str = "") -> str:
    target = str(guild_id) or current_guild_id()
    return _S.guilds[target].channel_id if target in _S.guilds else ""


# ── 동적 상태 영속(guild·channel·allowlist) ─────────────────────────────
def _state_path() -> "Path | None":
    if not _S.data_dir:
        return None
    return Path(_S.data_dir) / STATE_FILE


def _load_state() -> None:
    p = _state_path()
    if not p or not p.is_file():
        return
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        raw = d.get("guilds")
        if isinstance(raw, dict):
            _S.guilds = {
                str(gid): GuildState(
                    name=str((entry or {}).get("name") or ""),
                    channel_id=str((entry or {}).get("channel_id") or ""),
                    allowlist={str(x) for x in ((entry or {}).get("allowlist") or [])},
                )
                for gid, entry in raw.items()
                if str(gid).isdigit()
            }
            return
        # 옛 단일 서버 파일을 옮겨 온다. 그냥 무시하면 사용자가 이미 만들어 둔 명령 채널과
        # 허용목록을 잃고, 봇이 채널을 새로 만들어 예전 대화 통로가 끊긴다.
        legacy_guild = str(d.get("guild_id") or "")
        if legacy_guild.isdigit():
            _S.guilds = {legacy_guild: GuildState(
                channel_id=str(d.get("channel_id") or ""),
                allowlist={str(x) for x in (d.get("allowlist") or [])},
            )}
    except (ValueError, OSError):
        pass


def _save_state() -> None:
    p = _state_path()
    if not p:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"guilds": {gid: g.to_json() for gid, g in _S.guilds.items()}},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


# ── 명령 채널 자동 생성/확보 ────────────────────────────────────────────
async def _resolve_member(guild: "discord.Guild", uid: str):
    """멤버를 캐시 조회 → 실패 시 REST fetch로 확보한다.

    members 인텐트가 꺼져 있으면 갓 조인한 서버의 멤버 캐시가 비어 get_member가 None을 준다.
    소유자 열람 권한 부여가 조용히 누락되지 않도록 fetch_member(특권 인텐트 불필요)로 보강한다."""
    if not uid or not uid.isdigit():
        return None
    m = guild.get_member(int(uid))
    if m is not None:
        return m
    try:
        return await guild.fetch_member(int(uid))
    except Exception:  # noqa: BLE001 — 서버에 없거나 조회 불가면 그냥 스킵
        return None


async def _lock_overwrites(guild: "discord.Guild") -> dict:
    """명령 채널 잠금 권한 — @everyone 숨김, 봇·소유자·서버주인·허용목록 사용자만 열람.

    허용목록 사용자에게 view를 주지 않으면 is_authorized가 인가해도 채널이 안 보여 말을 걸 수 없다
    (/allow가 응답만 하고 실제로 작동 안 하던 문제). 허용목록 변경 시 _refresh_command_overwrites로 갱신."""
    # 키에 Role 과 Member 가 섞인다. 첫 항목만으로 추론시키면 dict[Role, ...] 이 되어
    # 아래에서 Member 를 넣을 때 타입이 어긋난다.
    ow: dict[Any, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False)
    }
    # guild.me 는 멤버 캐시 조회다. members 인텐트가 꺼져 있으면 갓 조인한 서버에서 None 이
    # 나올 수 있는데, 그대로 키에 넣으면 discord.py 가 "Role 이나 Member 가 아니다"로 거절해
    # 채널 생성 자체가 실패한다. 그러면 채널이 없어 봇이 어디서도 대답하지 못한다.
    # REST fetch 로 보강하고, 그래도 없으면 봇 몫만 빼고 만든다 — 채널을 만든 주체라 접근은 된다.
    me = guild.me
    if me is None and _S.app_id:
        me = await _resolve_member(guild, _S.app_id)
    if me is not None:
        ow[me] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    view_send = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    owner_m = await _resolve_member(guild, _S.owner_id)
    if owner_m is not None:
        ow[owner_m] = view_send
    guild_owner = guild.owner or await _resolve_member(guild, str(guild.owner_id or ""))
    if guild_owner is not None:
        ow[guild_owner] = view_send
    # 그 서버의 허용목록만 쓴다. 전체를 쓰면 A 서버에서 허용한 사람이 B 서버의 명령
    # 채널까지 볼 수 있다 — 서버는 서로 남이다.
    for uid in list(guild_state(str(guild.id)).allowlist):
        m = await _resolve_member(guild, uid)
        if m is not None:
            ow[m] = view_send
    return ow


async def _refresh_command_overwrites(guild_id: str = "") -> None:
    """허용목록이 바뀌면 명령 채널 권한을 다시 적용해 새 허용자가 채널을 볼 수 있게 한다."""
    target = str(guild_id) or current_guild_id()
    guild = bound_guild(target)
    state = _S.guilds.get(target)
    if guild is None or state is None or not state.channel_id.isdigit():
        return
    ch = guild.get_channel(int(state.channel_id))
    if ch is None:
        return
    try:
        await ch.edit(overwrites=await _lock_overwrites(guild), reason="Aiso 허용목록 갱신")
    except Exception:  # noqa: BLE001 — 권한 부족 등은 조용히 무시(다음 재연결 시 반영)
        pass


async def _ensure_command_channel(guild: "discord.Guild") -> None:
    """소유자+봇만 보이는 잠금 명령 채널을 서버마다 확보한다(있으면 재사용, 없으면 생성)."""
    state = guild_state(str(guild.id))
    # 저장된 채널이 아직 유효하면 그대로 사용(우리가 만든 잠금 채널로 신뢰)
    if state.channel_id:
        existing = guild.get_channel(int(state.channel_id)) if state.channel_id.isdigit() else None
        if existing is not None:
            return
    # 같은 이름의 채널이 이미 있으면 채택 — 단, 남이 만든 공개 채널일 수 있으므로 반드시 잠금을
    # 다시 적용한 뒤 채택한다(소유자 대화·승인 미리보기가 서버 전원에게 노출되는 것을 차단).
    for ch in guild.text_channels:
        if ch.name == COMMAND_CHANNEL:
            try:
                await ch.edit(overwrites=await _lock_overwrites(guild), reason="Aiso 명령 채널 잠금 확보")
            except discord.Forbidden:
                _S.last_error = "명령 채널을 잠글 권한이 없습니다 — 초대 시 채널 관리 권한을 확인하세요."
                return  # 잠그지 못하면 채택하지 않는다(공개 채널을 제어 통로로 쓰지 않음)
            except Exception as e:  # noqa: BLE001
                _S.last_error = f"명령 채널 잠금 실패: {e}"
                return
            state.channel_id = str(ch.id)
            _save_state()
            return
    # 없으면 잠금 채널 생성 — @everyone 숨김, 봇·소유자·서버주인만 열람
    try:
        ch = await guild.create_text_channel(
            COMMAND_CHANNEL, overwrites=await _lock_overwrites(guild), reason="Aiso 명령 채널 자동 생성"
        )
        state.channel_id = str(ch.id)
        _save_state()
        await ch.send(
            "👋 Aiso 봇이 연결되었습니다. 이 채널에서 말을 걸면 로컬 모델이 답합니다. "
            "\"팀 서버로 꾸며줘\"처럼 서버 구성도 요청할 수 있습니다."
        )
    except discord.Forbidden:
        _S.last_error = (
            "채널 생성 권한(Manage Channels)이 없습니다 — 봇을 내보낸 뒤 설정탭의 초대 링크로 다시 초대하거나, "
            "서버 설정에서 Aiso 역할에 '채널 관리' 권한을 주세요."
        )
    except Exception as e:  # noqa: BLE001
        _S.last_error = f"명령 채널 생성 실패: {type(e).__name__}: {e}"


async def _bind_guild(guild: "discord.Guild") -> None:
    """단일 서버로 고정하고 명령 채널을 확보한 뒤 슬래시 커맨드를 동기화한다."""
    key = str(guild.id)
    state = guild_state(key)
    state.name = str(getattr(guild, "name", "") or "")
    _save_state()
    await _ensure_command_channel(guild)
    if _S.tree is not None and key not in _S.synced_guilds:
        try:
            # 명령은 전역(guild 인자 없이)으로 등록돼 있다. 길드 sync에 포함되려면 먼저 전역 명령을
            # 이 길드로 복사해야 한다 — 안 하면 빈 배열이 올라가 /allow가 하나도 안 뜬다.
            guild_obj = discord.Object(id=guild.id)
            _S.tree.copy_global_to(guild=guild_obj)
            await _S.tree.sync(guild=guild_obj)
            _S.synced_guilds.add(key)
        except Exception as e:  # noqa: BLE001
            print(f"[discord] 슬래시 동기화 실패: {e}")


# ── 서버 구성(자연어 → 계획 → 소유자 승인 → 적용) ──────────────────────
class _ApproveView(discord.ui.View):
    """서버 구성 승인 버튼 — 소유자만 누를 수 있고, 시간 초과는 취소로 처리한다."""

    def __init__(self, owner_id: str) -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT_S)
        self.owner_id = owner_id
        self.approved = False

    async def interaction_check(self, interaction: "discord.Interaction") -> bool:
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("소유자만 승인할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="승인", style=discord.ButtonStyle.danger)
    async def _approve(self, interaction: "discord.Interaction", _button) -> None:
        self.approved = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def _cancel(self, interaction: "discord.Interaction", _button) -> None:
        self.approved = False
        await interaction.response.defer()
        self.stop()


async def _ask_owner_approval(channel, preview: str) -> bool:
    """미리보기와 승인/취소 버튼을 올리고 소유자의 결정을 기다린다(시간 초과=취소).

    미리보기는 절대 자르지 않는다 — 잘리면 뒤쪽 삭제 항목을 못 본 채 승인할 수 있다.
    2000자를 넘으면 나눠 보내고 버튼은 마지막 조각에 붙인다."""
    if not _S.owner_id:
        return False  # 소유자 미판별 → fail-closed
    view = _ApproveView(_S.owner_id)
    text = (
        "**서버 구성 변경 승인 요청**\n" + preview
        + f"\n\n소유자가 {APPROVAL_TIMEOUT_S}초 안에 결정해야 합니다."
    )
    parts = chunk_message(text)
    for part in parts[:-1]:
        await channel.send(part)
    msg = await channel.send(parts[-1], view=view)
    await view.wait()
    # _ApproveView의 자식은 @discord.ui.button 두 개(432·438행)뿐이라 모두 disabled를 갖지만,
    # discord.py가 children을 기반 타입 Item[...]으로 선언해 그 속성이 보이지 않는다 —
    # 분기를 추가하지 않고 루프 변수만 넓혀 둔다.
    child: Any
    for child in view.children:
        child.disabled = True
    outcome = "✅ 승인됨 — 적용합니다." if view.approved else "❌ 취소됨(거부 또는 시간 초과)."
    try:
        tail = parts[-1] + "\n\n" + outcome
        if len(tail) > DISCORD_MSG_LIMIT:  # 결과 문구가 안 들어가면 결과만 남긴다(미리보기는 위에 있음)
            tail = outcome
        await msg.edit(content=tail, view=view)
    except Exception:  # noqa: BLE001 — 결과 표시는 부가 기능
        pass
    return view.approved


_REJECTED = "[거부됨] 소유자가 승인하지 않았습니다."


async def _apply_with_approval(channel, ops) -> str:
    """검증 → 미리보기 → 소유자 승인 → 적용. 각 단계 결과를 모델에게 툴 결과로 돌려준다."""
    import discordops  # noqa: PLC0415 — 순환 import 회피(디스코드 쪽만 지연)

    guild = bound_guild()
    if guild is None:
        return "[불가] 서버 정보를 찾을 수 없습니다(봇 재연결 필요)."
    snap = discordops.snapshot_guild(guild, command_channel_id(str(guild.id)))
    clean, skipped, error_msg = discordops.prepare_ops(ops, snap)  # 보호대상 분리 + 검증(server_apply와 공유)
    if error_msg:
        return error_msg
    # prepare_ops가 clean=None을 내는 두 경로(discordops.py 375·380행)는 반드시 error_msg를
    # 함께 내므로, 오류 가드를 지나면 clean은 리스트다(server_apply도 같은 계약을 쓴다).
    assert clean is not None
    preview = discordops.render_ops_preview(clean, snap)
    if skipped:
        preview += "\n제외됨(보호): " + " · ".join(skipped)
    if not await _ask_owner_approval(channel, preview):
        return _REJECTED
    return (await discordops.apply_ops_live(guild, clean, snap)) + discordops.format_skipped_report(skipped)


async def _send_with_approval(channel, args: dict) -> str:
    """즉시 전송 — 검증 → 미리보기 → 소유자 승인 버튼 → 전송."""
    import discordops  # noqa: PLC0415

    a = discordops.canonical_send_args(args or {})
    got, err = discordops.validate_send(a["channel"], a["message"])
    if err:
        return f"[거부] {err}"
    assert got is not None  # validate_send(discordops.py 671행)의 거부 return은 모두 (None, 오류)다
    ch_id, ch_name, body = got
    # 본문을 자르지 않는다 — 소유자가 실제로 전송될 전체 내용을 보고 승인해야 한다
    # (서버구성 승인과 동일 원칙; _ask_owner_approval이 2000자 단위로 분할 전송한다).
    preview = f"메시지 전송 요청 → #{ch_name}\n───\n{body}"
    if not await _ask_owner_approval(channel, preview):
        return _REJECTED
    guild = bound_guild()
    if guild is None:
        return "[불가] 서버 정보를 찾을 수 없습니다(봇 재연결 필요)."
    return await discordops.send_message_live(guild, ch_id, body)


async def _schedule_add_with_approval(channel, args: dict) -> str:
    """예약 등록 — 완전 선검증 → 미리보기 → 소유자 승인 버튼 → 등록(발화 시엔 무승인 실행).

    검증(개수·길이·시각 포함)을 승인 전에 모두 마치므로 '승인했는데 거부'가 없고, 승인 전 계산한
    발화 시각(draft.next_run)을 그대로 등록해 미리보기와 실제 예약이 어긋나지 않는다."""
    import discordops  # noqa: PLC0415
    import discordsched  # noqa: PLC0415

    a = discordsched.canonical_add_args(args or {})
    got, err = discordops.resolve_text_channel(a["channel"])
    if err:
        return f"[거부] {err}"
    # resolve_text_channel(discordops.py 652행)·build_job(discordsched.py 244행) 모두
    # (값,None)·(None,오류)로 상보적이라 오류 가드를 지나면 값이 반드시 있다.
    assert got is not None
    ch_id, ch_name = got
    draft, derr = discordsched.build_job(
        channel_id=ch_id, channel_name=ch_name, kind=a["kind"], text=a["text"],
        when=a["when"], repeat=a["repeat"],
    )
    if derr:
        return f"[거부] {derr}"
    assert draft is not None
    if not await _ask_owner_approval(channel, discordsched.render_add_preview(draft)):
        return _REJECTED
    return "예약이 등록되었습니다.\n" + discordsched.render_job(discordsched.commit_job(draft))


async def _channel_report_add_with_approval(channel, args: dict) -> str:
    """채널 보고 예약 — 완전 선검증 → 미리보기 → 소유자 승인 버튼 → 등록.

    검증(채널 해석·권한·개수·주기·지시 길이·예약 개수 상한)을 승인 전에 모두 마치므로
    '승인했는데 거부'가 없다. 예전에는 승인 뒤 channel_report_add 안에서 9개 거부 경로가
    돌았고, 미리보기도 해석 전 값이라 실제 등록과 어긋났다 — 보고 채널을 생략하면
    미리보기엔 "#(없음)"이 뜨는데 실제로는 첫 수집 채널로 등록됐다.

    _schedule_add_with_approval과 같은 형태이며, 같은 트레이드오프를 공유한다:
    승인 전에 계산한 draft를 그대로 커밋하므로 미리보기와 실제 예약이 어긋나지 않는다.
    """
    import discordsched  # noqa: PLC0415

    draft, meta, error = await discordsched.prepare_channel_report(**(args or {}))
    if error:
        return error
    # prepare_channel_report는 거부 시 (None, None, 오류)만 낸다(discordsched.py 386행) —
    # 오류가 없으면 draft·meta가 함께 있다(channel_report_add도 같은 계약을 쓴다).
    assert draft is not None and meta is not None
    if not await _ask_owner_approval(channel, discordsched.render_channel_report_preview(meta)):
        return _REJECTED
    return discordsched.render_channel_report_registered(discordsched.commit_job(draft), meta)


async def _repo_report_add_with_approval(channel, args: dict) -> str:
    """저장소 보고 등록 — 선검증 → 미리보기 → 소유자 승인 → 등록.

    저장소가 실제로 열리는지, 어느 커밋을 기준으로 삼을지까지 **승인 전에** 확인한다.
    승인 화면에 경로와 브랜치와 기준 커밋이 그대로 보이므로, 사용자는 무엇을 허락하는지
    알고 누른다. 등록 후에는 그 경로가 고정되어 모델이 바꿀 수 없다.
    """
    import discordops  # noqa: PLC0415
    import discordsched  # noqa: PLC0415
    import gitreport  # noqa: PLC0415

    got, err = discordops.resolve_text_channel(str(args.get("report_channel") or ""))
    if err:
        return f"[거부] {err}"
    assert got is not None
    ch_id, ch_name = got
    repo_path = str(args.get("repo_path") or "")
    branch = str(args.get("branch") or "HEAD")
    try:
        baseline = await gitreport.collect(repo_path, branch, "", fetch=False)
    except gitreport.GitReportError as error:
        return f"[거부] {error}"
    draft, derr = discordsched.build_repo_report_job(
        repo_path=repo_path,
        branch=branch,
        report_channel_id=ch_id,
        report_channel_name=ch_name,
        interval_hours=args.get("interval_hours"),
        instruction=str(args.get("instruction") or ""),
        head=baseline.head,
        guild_id=current_guild_id(),
    )
    if derr:
        return f"[거부] {derr}"
    assert draft is not None
    preview = (
        "저장소 변경 보고를 등록합니다.\n"
        f"· 저장소: {draft['repo_path']}\n"
        f"· 브랜치: {draft['branch']} (현재 {baseline.head})\n"
        f"· 보고 채널: #{ch_name}\n"
        f"· 주기: {draft['interval_hours']}시간마다\n"
        f"· 첫 보고: {draft['next_run']}\n"
        + (f"· 지시: {draft['text']}\n" if draft["text"] else "")
        + "\n등록 이후의 새 커밋만 보고합니다. 커밋 메시지와 파일·줄 수를 보내며 "
        "코드 본문은 보내지 않습니다."
    )
    if not await _ask_owner_approval(channel, preview):
        return _REJECTED
    return "저장소 보고가 등록되었습니다.\n" + discordsched.render_job(discordsched.commit_job(draft))


async def _run_bot_tool(channel, author_id: str, name: str, args: dict) -> str:
    import discordops  # noqa: PLC0415
    import discordsched  # noqa: PLC0415

    if name == "discord_server_map":
        return await discordops.server_map()
    if name == "discord_server_apply":
        return await _apply_with_approval(channel, (args or {}).get("ops"))
    if name == "discord_send":
        return await _send_with_approval(channel, args)
    if name == "discord_schedule_add":
        return await _schedule_add_with_approval(channel, args)
    if name == "discord_channel_report_add":
        return await _channel_report_add_with_approval(channel, args)
    if name == "discord_repo_report_add":
        return await _repo_report_add_with_approval(channel, args)
    if name == "discord_schedule_list":
        return await discordsched.schedule_list()
    if name == "discord_schedule_remove":
        # 삭제는 버튼 없이 소유자 본인 요청만 허용(허용목록 사용자가 남의 예약을 지우지 못하게)
        if not _S.owner_id or str(author_id) != _S.owner_id:
            return "[거부] 예약 삭제는 소유자만 할 수 있습니다."
        return await discordsched.schedule_remove(**(args or {}))
    if name == "generate_image":
        if _S.image is None:
            return "[오류] Discord 이미지 생성은 ComfyUI 연결과 준비된 Agent 모델이 있을 때만 사용할 수 있습니다."
        result = await _S.image(args if isinstance(args, dict) else {})
        data = result.get("data")
        filename = str(result.get("filename") or "aiso-image.png")
        summary = str(result.get("summary") or "이미지를 생성했습니다.")
        if not isinstance(data, bytes) or not data:
            raise ValueError("이미지 생성 결과 데이터 형식이 올바르지 않습니다.")
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("생성 이미지는 Discord 첨부 제한(10MB)을 초과해 전송할 수 없습니다.")
        if Path(filename).name != filename or not filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            raise ValueError("생성 이미지 파일명 형식이 올바르지 않습니다.")
        await channel.send(f"🖼️ {summary}", file=discord.File(io.BytesIO(data), filename=filename))
        return f"이미지를 Discord에 전송했습니다. {summary}"
    return f"[불가] 알 수 없는 도구: {name}"


def _parse_call(call: dict) -> tuple[str, dict]:
    fn = (call or {}).get("function") or {}
    name = str(fn.get("name") or "")
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return name, (args if isinstance(args, dict) else {})


async def _tool_chat(channel, author_id: str, convo: list) -> str:
    """서버 구성·전송·예약 도구를 쓸 수 있는 미니 루프 — 최종 답변 텍스트를 돌려준다."""
    import discordops  # noqa: PLC0415
    import discordsched  # noqa: PLC0415

    # 스키마 dict들의 값 타입이 제각각이라 리터럴만으로는 Collection[str]으로 좁게 추론된다 —
    # 실제로 담기는 것(그리고 model_schemas_for가 돌려주는 것)은 툴 스키마 dict다.
    tools: list[dict[str, Any]] = [
        discordops.MAP_SCHEMA, discordops.APPLY_SCHEMA, discordops.SEND_SCHEMA,
        discordsched.SCHEDULE_ADD_SCHEMA, discordsched.SCHEDULE_LIST_SCHEMA,
        discordsched.SCHEDULE_REMOVE_SCHEMA, discordsched.CHANNEL_REPORT_ADD_SCHEMA,
        discordsched.REPO_REPORT_ADD_SCHEMA,
    ]
    if _S.image is not None:
        from comfy_generation import GENERATE_IMAGE_SCHEMA  # noqa: PLC0415
        tools.append(GENERATE_IMAGE_SCHEMA)
    # Raw schemas also feed Settings in Korean.  Send a separate English model
    # copy here, while matching and execution still use the original names.
    from tool_schema_language import model_schemas_for  # noqa: PLC0415
    tools = model_schemas_for(tools)
    allowed_tool_names = {
        str((tool.get("function") or {}).get("name") or "") for tool in tools
    }
    convo = list(convo)
    completed_provider_calls: dict[str, tuple[str, str]] = {}
    for _ in range(MAX_TOOL_TURNS):
        async with _S.gen_lock:  # 생성만 직렬화한다 — 뒤이은 승인 대기는 락 밖에서
            # 이 루프로 들어오는 유일한 경로가 _S.step 유무로 정해진다(827·828·877행:
            # use_tools = _S.step is not None → route == "tools"일 때만 호출).
            assert _S.step is not None
            resp = await _S.step(convo, tools)
        calls = resp.get("tool_calls") or []
        if not calls:
            return (resp.get("content") or "").strip() or "(빈 응답)"
        requested_names = [_parse_call(call)[0] for call in calls]
        if any(name not in allowed_tool_names for name in requested_names):
            return "(모델이 Discord 승인 범위 밖의 도구를 요청해 실행하지 않았습니다.)"
        batch_provider_ids: set[str] = set()
        batch_signatures: list[tuple[str | None, str]] = []
        for call in calls:
            provider_id = call.get("provider_tool_call_id") or call.get("id")
            name, args = _parse_call(call)
            signature = f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
            if provider_id:
                if provider_id in batch_provider_ids:
                    return "(Discord 도구 호출 ID가 한 응답에서 중복되어 실행하지 않았습니다.)"
                batch_provider_ids.add(provider_id)
                previous = completed_provider_calls.get(provider_id)
                if previous is not None and previous[0] != signature:
                    return "(Discord 도구 호출 ID가 다른 작업에 재사용되어 실행하지 않았습니다.)"
            batch_signatures.append((provider_id, signature))
        wire_calls = []
        for call in calls:
            provider_id = call.get("provider_tool_call_id") or call.get("id")
            fn = call.get("function") or {}
            canonical = call.get("canonical_arguments")
            if provider_id and isinstance(canonical, str):
                wire_calls.append({
                    "id": provider_id,
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": canonical},
                })
            else:
                wire_calls.append(call)
        convo.append({"role": "assistant", "content": resp.get("content") or "", "tool_calls": wire_calls})
        for call, (provider_id, signature) in zip(calls, batch_signatures):
            name, args = _parse_call(call)
            previous = completed_provider_calls.get(provider_id) if provider_id else None
            if previous is not None:
                result = previous[1]
            else:
                try:
                    result = await _run_bot_tool(channel, author_id, name, args)
                except Exception as e:  # noqa: BLE001 — 도구 실패는 모델에게 알리고 계속
                    result = f"[오류] {e}"
                if provider_id:
                    completed_provider_calls[provider_id] = (signature, result)
            convo.append({
                "role": "tool",
                **({"tool_call_id": provider_id} if provider_id else {}),
                "content": result,
            })
    return "(작업이 너무 길어져 중단했습니다 — 요청을 나눠서 다시 시도해 주세요)"


def _build_client(generate: GenerateFn) -> "discord.Client":
    intents = discord.Intents.default()
    intents.message_content = True  # 메시지 본문 읽기(특권 인텐트 — 개발자 포털에서 켜야 함)
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    _S.tree = tree  # on_ready에서 길드 동기화에 참조

    # ── 슬래시: 허용 사용자 관리(소유자 전용) ──
    allow = app_commands.Group(name="allow", description="명령 허용 사용자 관리 (소유자 전용)")

    def _owner_only(interaction: "discord.Interaction") -> bool:
        return bool(_S.owner_id) and str(interaction.user.id) == _S.owner_id

    def _interaction_guild(interaction: "discord.Interaction") -> str:
        """슬래시 명령이 실행된 서버. DM 에서는 빈 문자열이라 아래에서 거절한다."""
        guild = getattr(interaction, "guild", None)
        return str(guild.id) if guild is not None else ""

    @allow.command(name="add", description="허용 사용자를 추가합니다")
    @app_commands.describe(user="추가할 사용자")
    async def allow_add(interaction: "discord.Interaction", user: "discord.User") -> None:
        if not _owner_only(interaction):
            await interaction.response.send_message("소유자만 사용할 수 있습니다.", ephemeral=True)
            return
        gid = _interaction_guild(interaction)
        if not gid:
            await interaction.response.send_message("서버 안에서 사용해 주세요.", ephemeral=True)
            return
        # 이 서버에만 허용한다. 전체에 더하면 여기서 허용한 사람이 다른 서버까지 조작한다.
        guild_state(gid).allowlist.add(str(user.id))
        _save_state()
        await _refresh_command_overwrites(gid)  # 새 허용자가 #aiso 채널을 볼 수 있게 권한 갱신
        await interaction.response.send_message(
            f"✅ 이 서버에 허용 추가: {user} (`{user.id}`)", ephemeral=True
        )

    @allow.command(name="remove", description="허용 사용자를 제거합니다")
    @app_commands.describe(user="제거할 사용자")
    async def allow_remove(interaction: "discord.Interaction", user: "discord.User") -> None:
        if not _owner_only(interaction):
            await interaction.response.send_message("소유자만 사용할 수 있습니다.", ephemeral=True)
            return
        gid = _interaction_guild(interaction)
        if not gid:
            await interaction.response.send_message("서버 안에서 사용해 주세요.", ephemeral=True)
            return
        guild_state(gid).allowlist.discard(str(user.id))
        _save_state()
        await _refresh_command_overwrites(gid)  # 제거된 사용자의 채널 접근 회수
        await interaction.response.send_message(
            f"🗑 이 서버에서 허용 제거: {user} (`{user.id}`)", ephemeral=True
        )

    @allow.command(name="list", description="허용 사용자 목록을 봅니다")
    async def allow_list(interaction: "discord.Interaction") -> None:
        if not _owner_only(interaction):
            await interaction.response.send_message("소유자만 사용할 수 있습니다.", ephemeral=True)
            return
        gid = _interaction_guild(interaction)
        if not gid:
            await interaction.response.send_message("서버 안에서 사용해 주세요.", ephemeral=True)
            return
        ids = sorted(guild_state(gid).allowlist)
        body = "\n".join(f"• <@{i}> (`{i}`)" for i in ids) if ids else "(없음)"
        await interaction.response.send_message(f"이 서버의 허용 사용자:\n{body}", ephemeral=True)

    tree.add_command(allow)

    # ── 이벤트 ──
    @client.event
    async def on_ready() -> None:
        _S.last_error = None
        try:
            info = await client.application_info()
            owner = getattr(info, "owner", None)
            if owner is not None:
                _S.owner_id = str(owner.id)  # 봇 제작자 = 소유자(자동)
        except Exception as e:  # noqa: BLE001
            print(f"[discord] 소유자 판별 실패: {e}")
        if client.user is not None:
            _S.app_id = str(client.user.id)  # 봇 user id = 애플리케이션 id (초대 링크용)
        print(f"[discord] 로그인: {client.user} · 소유자 {_S.owner_id}")
        # 들어가 있는 **모든** 서버에 붙는다. 예전에는 하나만 남기고 나머지를 퇴장시켰다.
        guilds = list(client.guilds)
        # 더는 속하지 않는 서버의 기록은 버린다 — 남겨 두면 화면이 없는 서버를 보여 준다.
        live = {str(g.id) for g in guilds}
        for gone in [gid for gid in _S.guilds if gid not in live]:
            _S.guilds.pop(gone, None)
            _S.synced_guilds.discard(gone)
        if len(guilds) > MAX_GUILDS:
            _S.last_error = (
                f"서버 {len(guilds)}개에 초대되어 있습니다. 앞의 {MAX_GUILDS}개만 사용합니다."
            )
            guilds = guilds[:MAX_GUILDS]
        for guild in guilds:
            try:
                await _bind_guild(guild)
            except Exception as error:  # noqa: BLE001 — 한 서버의 실패가 나머지를 막지 않는다
                print(f"[discord] 서버 연결 실패({guild.id}): {error}")
        _save_state()

    @client.event
    async def on_guild_join(guild: "discord.Guild") -> None:
        # 이제 여러 서버에 동시에 붙는다. 서버마다 명령 채널·허용목록을 따로 두므로
        # 한 서버의 사용자가 다른 서버를 조작할 수 없다.
        if len(_S.guilds) >= MAX_GUILDS and str(guild.id) not in _S.guilds:
            try:
                sys_ch = guild.system_channel
                if sys_ch is not None:
                    await sys_ch.send(
                        f"이미 서버 {MAX_GUILDS}개에 연결되어 있어 이 서버에서는 나갑니다."
                    )
            except Exception:  # noqa: BLE001
                pass
            try:
                await guild.leave()
            except Exception:  # noqa: BLE001
                pass
            return
        await _bind_guild(guild)

    @client.event
    async def on_guild_remove(guild: "discord.Guild") -> None:
        """서버에서 쫓겨나거나 나갔으면 그 기록을 버린다. 남으면 화면이 거짓을 보여 준다."""
        _S.guilds.pop(str(guild.id), None)
        _S.synced_guilds.discard(str(guild.id))
        _save_state()

    @client.event
    async def on_message(message: "discord.Message") -> None:
        if client.user is not None and message.author.id == client.user.id:
            return
        if message.author.bot:
            return
        # 메시지가 온 **그 서버**의 명령 채널·허용목록으로만 인가한다. 전역 값으로 판단하면
        # A 서버에서 허용한 사용자가 B 서버의 채널에서도 통과한다.
        guild = getattr(message, "guild", None)
        if guild is None:
            return  # DM 은 명령 채널이 아니다
        gid = str(guild.id)
        state = _S.guilds.get(gid)
        if state is None:
            return
        if not is_authorized(_S.owner_id, state.channel_id, state.allowlist,
                             message.author.id, message.channel.id):
            return  # 비인가·비지정채널 → 무응답
        # 이후 도구 실행이 어느 서버를 대상으로 하는지 여기서 정한다. contextvar 라
        # 서버별 처리가 동시에 돌아도 서로 섞이지 않는다.
        _CURRENT_GUILD.set(gid)
        text = (message.content or "").strip()
        raw_attachments = list(getattr(message, "attachments", ()) or ())
        if not text and raw_attachments:
            text = "첨부한 자료를 읽고 핵심 내용을 요약해 주세요."
        if not text:
            return
        hist = _S.history[message.channel.id]
        # Decide the answer language from Discord text before an attachment extractor appends
        # document/OCR content. A foreign-language attachment must not change the user's reply language.
        raw_language_messages = [
            {"role": role, "content": content}
            for role, content in hist
        ]
        raw_language_messages.append({"role": "user", "content": text})
        response_language = response_language_from_messages(raw_language_messages, fallback="ko")
        use_tools = _S.step is not None  # 서버 구성 도구를 쓸 수 있으면 툴 루프로
        route = _chat_route(text, can_research=_S.research is not None, can_use_tools=use_tools)
        messages = [{
            "role": "system",
            "content": (
                _tools_prompt(_S.image is not None, response_language)
                if use_tools
                else base_system_prompt(response_language)
            ),
        }]
        for role, content in hist:
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": text})
        try:
            async with message.channel.typing():
                if raw_attachments:
                    from discord_attachments import (  # noqa: PLC0415
                        DiscordAttachmentError,
                        append_discord_attachment_context,
                        build_discord_attachment_context,
                    )
                    try:
                        attachment_context = await build_discord_attachment_context(
                            raw_attachments, allow_images=_S.allow_attachment_images
                        )
                        messages = append_discord_attachment_context(messages, attachment_context)
                        if attachment_context.notices:
                            messages[-1]["content"] += (
                                "\n\n[첨부 처리 안내]\n- " + "\n- ".join(attachment_context.notices)
                            )
                    except DiscordAttachmentError as error:
                        reply = f"(첨부 분석을 시작하지 못했습니다: {error})"
                        hist.append(("user", text))
                        hist.append(("assistant", reply))
                        await message.channel.send(reply)
                        return
                if route == "research":
                    # Discord 조작 지침을 조사 프롬프트와 섞지 않는다. 조사 실패 시에는
                    # 모델의 오래된 기억으로 대체하지 않고 리서치 루프의 실패를 그대로 알린다.
                    reply = await _research_chat(
                        message.channel,
                        text,
                        messages,
                        response_language=response_language,
                    )
                elif route == "research_unavailable":
                    reply = "(웹 조사 기능을 사용할 수 없어 최신 정보를 확인하지 못했습니다. 근거 없이 답하지 않았습니다.)"
                elif route == "tools":
                    # _tool_chat이 생성 턴마다 gen_lock을 잡고 놓는다 — 승인 대기는 락 밖에서
                    # 이뤄져 미결 승인이 다른 채팅을 최대 120초 얼리지 않는다.
                    reply = await _tool_chat(message.channel, str(message.author.id), messages)
                else:
                    async with _S.gen_lock:
                        reply = await generate(messages)
        except Exception as e:  # noqa: BLE001
            reply = f"(오류: {e})"
        hist.append(("user", text))
        hist.append(("assistant", reply))
        try:
            for part in chunk_message(reply):
                await message.channel.send(part)
        except Exception:  # noqa: BLE001 — 채널 삭제·연결 종료 등 전송 실패는 조용히 무시
            pass

    return client


# ── 예약 러너 — 앱이 켜져 있는 동안 30초 간격으로 발화 시각을 확인한다 ──
SCHED_TICK_S = 30
BRIEFING_TIMEOUT_S = 240  # 브리핑 생성(조사 루프)이 이 시간을 넘으면 중단 — gen_lock을 물고 채팅을 무한정 얼리지 않게

CHANNEL_REPORT_TIMEOUT_S = 240
CHANNEL_REPORT_TOTAL_CHARS = 40_000
CHANNEL_REPORT_MESSAGES_PER_CHANNEL = 100

def briefing_system(response_language: str | None = "ko") -> str:
    """Model-only policy for a scheduled Discord briefing."""
    language = normalize_response_language(response_language)
    return (
        "You are Aiso. Write a briefing that will be posted to a Discord channel according to the user's "
        "instruction. Return only the body: no greeting, no preamble. Use concise Markdown with useful headings "
        "and lists. Do not invent facts or sources.\n\n"
        + final_response_language_prompt(language)
    )


def repo_report_system(response_language: str | None = "ko") -> str:
    """저장소 보고 정책. 사용자가 고른 '분류형' 양식을 모델에게 그대로 요구한다.

    수치는 이미 사실로 주어지므로 모델이 다시 계산하지 않는다. 모델이 하는 일은
    커밋 메시지를 읽고 **추가된 것 / 수정된 것 / 설정 변경**으로 나눠 서술하는 것뿐이다.
    커밋 메시지에 없는 이유를 지어내면 보고서 전체를 믿을 수 없게 되므로 금지한다.
    """
    language = normalize_response_language(response_language)
    return (
        "You are Aiso. Write a git repository change report for a Discord channel.\n"
        "You are given verified facts collected from `git log`. Every number is already correct: "
        "never recompute, never estimate, never invent a number that is not given.\n\n"
        "Structure the report exactly like this, omitting any section that has no items:\n"
        "  header line: repository name, branch, period, commit count, author count\n"
        "  section '추가된 것' — new features, files, or capabilities\n"
        "  section '수정된 것' — bug fixes and corrections to existing behaviour\n"
        "  section '설정 변경' — build config, ignore rules, tooling\n"
        "  section '상태' — branch position and anything the facts warn about\n\n"
        "For each item give a short title, then the date, author, and the file/line numbers "
        "taken from the facts. Describe WHY only when the commit message says why; when the "
        "message is only a subject line, describe what changed and stop. Never guess intent.\n"
        "Plain text for Discord: no markdown tables (they do not render), no code fences. "
        "Use simple bullets and indentation. Keep it under 1800 characters when possible.\n\n"
        + final_response_language_prompt(language)
    )


def channel_report_system(response_language: str | None = "ko") -> str:
    """Model-only policy for a recurring, new-message-only channel report."""
    language = normalize_response_language(response_language)
    return (
        "You are a records assistant summarizing only newly collected collaboration-channel messages. Do not infer "
        "facts that are absent from the supplied source messages. Write concise Markdown, in this semantic order: "
        "Key summary, Decisions, To-dos, Open questions. Use equivalent localized headings in the required output "
        "language. If a section has no evidence, explicitly say that it has none. Include an assignee only when the "
        "source explicitly names one, and cite the source channel for important items. Keep the entire body within "
        "3,000 characters.\n\n"
        + final_response_language_prompt(language)
    )




def _job_response_language(job: dict) -> str:
    """Infer a scheduled job's output language from its original instruction, never collected messages."""
    return response_language_from_messages(
        [{"role": "user", "content": str(job.get("text") or "")}],
        fallback="ko",
    )


def _scheduled_heading(kind: str, response_language: str) -> str:
    """Localized deterministic wrapper labels for the two scheduled model outputs."""
    if normalize_response_language(response_language) == "ko":
        return "예약 브리핑" if kind == "briefing" else "채널 대화 보고서"
    return "Scheduled briefing" if kind == "briefing" else "Channel conversation report"


async def _collect_channel_report_messages(guild, job: dict) -> tuple[list[str], list[dict], list[str]]:
    """Collect only messages after each persisted channel cursor."""
    import discord

    sources = [dict(item) for item in (job.get("source_channels") or []) if isinstance(item, dict)]
    per_channel_chars = max(2_000, CHANNEL_REPORT_TOTAL_CHARS // max(1, len(sources)))
    collected: list[tuple[datetime, str]] = []
    updated_sources: list[dict] = []
    errors: list[str] = []
    for source in sources:
        channel_id = str(source.get("id") or "")
        channel_name = str(source.get("name") or channel_id)
        cursor = str(source.get("last_message_id") or "0")
        channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
        if channel is None or not hasattr(channel, "history"):
            errors.append(f"#{channel_name}: 채널을 찾을 수 없음")
            updated_sources.append(source)
            continue
        used_chars = 0
        last_processed = cursor
        after = discord.Object(id=int(cursor)) if cursor.isdigit() and int(cursor) > 0 else None
        try:
            async for message in channel.history(
                limit=CHANNEL_REPORT_MESSAGES_PER_CHANNEL,
                after=after,
                oldest_first=True,
            ):
                message_id = str(message.id)
                if getattr(message.author, "bot", False):
                    last_processed = message_id
                    continue
                content = str(getattr(message, "clean_content", None) or message.content or "").strip()
                attachments = [str(getattr(item, "filename", "첨부 파일")) for item in message.attachments]
                if attachments:
                    suffix = "첨부: " + ", ".join(attachments)
                    content = f"{content} [{suffix}]" if content else f"[{suffix}]"
                if not content:
                    last_processed = message_id
                    continue
                content = content.replace("\n", " ")[:1000]
                created = message.created_at
                stamp = created.astimezone().strftime("%Y-%m-%d %H:%M")
                author = str(getattr(message.author, "display_name", None) or message.author)
                line = f"[{stamp}] #{channel_name} · {author}: {content}"
                if used_chars and used_chars + len(line) + 1 > per_channel_chars:
                    break
                used_chars += len(line) + 1
                last_processed = message_id
                collected.append((created, line))
        except Exception as error:  # noqa: BLE001
            errors.append(f"#{channel_name}: 기록 읽기 실패 ({error})")
            updated_sources.append(source)
            continue
        updated = dict(source)
        updated["last_message_id"] = last_processed
        updated_sources.append(updated)
    collected.sort(key=lambda item: item[0])
    return [line for _created, line in collected], updated_sources, errors


async def _report_repo_failure(guild, job: dict, reason: str, *, pending: int = 0) -> None:
    """저장소 보고가 실패했음을 명령 채널(#aiso)에 알린다. 커서는 옮기지 않는다.

    보고 채널이 아니라 **명령 채널**로 보낸다. 보고 채널은 사람이 결과만 읽는 곳이라
    실패 문구가 섞이면 보고서 이력이 지저분해지고, 무엇보다 전송 자체가 실패한 경우에는
    그 채널로는 애초에 아무 말도 할 수 없다.

    같은 사유가 이어지면 한 번만 알린다. 모델이 며칠 멈춰 있으면 주기마다 같은 문장이
    쌓여 명령 채널이 못 쓰게 된다. 사유가 바뀌거나 한 번 성공한 뒤에는 다시 알린다.
    """
    import discordsched  # noqa: PLC0415

    repeated = str(job.get("last_failure") or "") == reason
    discordsched.update_job(job.get("id", ""), {"last_failure": reason})
    if repeated:
        return
    channel_id = command_channel_id(str(guild.id))
    channel = guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
    if channel is None:
        return
    name = Path(str(job.get("repo_path") or "")).name or "저장소"
    hours = job.get("interval_hours")
    lines = [f"⚠ **{name}** 저장소 보고를 만들지 못했습니다 — {reason}"]
    if pending:
        # 몇 개가 밀려 있는지 밝힌다. 커서를 옮기지 않았으니 다음 회차에 함께 나간다.
        lines.append(f"커밋 {pending}개는 아직 보고되지 않았습니다.")
    lines.append(f"다음 회차{f'({hours}시간 뒤)' if hours else ''}에 다시 시도합니다.")
    try:
        await channel.send("\n".join(lines))
    except Exception as error:  # noqa: BLE001 — 알림 실패가 러너를 죽이면 안 된다
        print(f"[discord] 저장소 보고 실패 알림 전송 실패: {error}")


async def _run_repo_report(job: dict) -> None:
    """저장소 보고 1건 — 성공적으로 보낸 뒤에만 커서를 옮긴다.

    커서를 먼저 옮기면 전송 실패한 회차의 커밋이 영영 보고되지 않는다. 반대로 나중에
    옮기면 최악의 경우 같은 내용을 한 번 더 보내는데, 보고서는 중복이 누락보다 낫다.
    """
    import discordsched  # noqa: PLC0415
    import gitreport  # noqa: PLC0415

    # 등록 때 적어 둔 서버로 간다. 인자 없이 부르면 '지금 처리 중인 서버'를 찾는데,
    # 예약 러너에는 그 문맥이 없어 서버가 둘 이상이면 None 이 되어 조용히 끝난다.
    guild = bound_guild(str(job.get("guild_id") or ""))
    if guild is None:
        return
    channel = guild.get_channel(int(job["channel_id"])) if str(job.get("channel_id", "")).isdigit() else None
    if channel is None:
        return
    branch = str(job.get("branch") or "HEAD")
    all_branches = branch == gitreport.ALL_BRANCHES
    try:
        if all_branches:
            report = await gitreport.collect_all_branches(
                str(job.get("repo_path") or ""),
                dict(job.get("branch_cursors") or {}),
            )
        else:
            report = await gitreport.collect(
                str(job.get("repo_path") or ""),
                branch,
                str(job.get("last_commit") or ""),
            )
    except gitreport.GitReportError as error:
        await _report_repo_failure(guild, job, str(error))
        return
    if not report.commits:
        # 새 커밋이 없으면 아무 말도 하지 않는다. 조용한 것이 정상이다.
        # 다만 기준점은 지금 상태로 당겨 둔다 — 등록 직후 첫 회차나, 기준이 사라져
        # 다시 잡아야 하는 경우가 여기로 온다.
        if all_branches:
            if report.ref_heads and report.ref_heads != dict(job.get("branch_cursors") or {}):
                discordsched.update_job(job.get("id", ""), {"branch_cursors": report.ref_heads})
        elif report.head and report.head != str(job.get("last_commit") or ""):
            discordsched.update_job(job.get("id", ""), {"last_commit": report.head})
        return

    facts = gitreport.build_facts(report)
    response_language = _job_response_language(job)
    instruction = str(job.get("text") or "").strip()
    messages = [
        {"role": "system", "content": repo_report_system(response_language)},
        {
            "role": "user",
            "content": (
                (f"Additional user instruction: {instruction}\n\n" if instruction else "")
                + "[Verified facts from git]\n" + facts
            ),
        },
    ]
    # 생성이 실패하면 **보내지 않고 커서도 옮기지 않는다.**
    #
    # 예전에는 실패 문구를 본문 삼아 그대로 보내고 커서를 전진시켰다. 전송에는 성공하니
    # 커밋이 소모되어, 그 회차의 커밋들은 영영 다시 보고되지 않았다 — 12B 로 커밋 수십
    # 개를 요약하다 타임아웃이 나는 것은 드문 일이 아니다. 보고서는 누락이 가장 나쁘므로
    # 실패한 회차는 통째로 다음으로 미룬다.
    body = ""
    failure = ""
    try:
        async with _S.gen_lock:
            if _S.generate is None:
                return
            body = await asyncio.wait_for(_S.generate(messages), timeout=CHANNEL_REPORT_TIMEOUT_S)
    except asyncio.TimeoutError:
        failure = f"보고서 생성이 {CHANNEL_REPORT_TIMEOUT_S}초를 넘겨 중단되었습니다."
    except Exception as error:  # noqa: BLE001
        failure = f"보고서 생성 실패: {error}"
    if not failure and not str(body or "").strip():
        # 빈 응답도 실패다. 헤더만 있는 보고서를 보내고 커밋을 소모하면 같은 손실이 난다.
        failure = "모델이 빈 보고서를 내놓았습니다."
    if failure:
        await _report_repo_failure(guild, job, failure, pending=len(report.commits))
        return

    name = Path(str(job.get("repo_path") or "")).name or "저장소"
    header = f"📦 **{name}** — {len(report.commits)}개 커밋"
    if all_branches and report.branch != gitreport.ALL_BRANCHES:
        # 어느 브랜치가 움직였는지 제목에 적는다. 여러 브랜치를 한꺼번에 보는 모드에서는
        # 이 줄이 없으면 어디서 벌어진 일인지 보고서를 다 읽어야 알 수 있다.
        header += f" · {report.branch}"
    sent = False
    try:
        for part in chunk_message(f"{header}\n\n{body}"):
            await channel.send(part)
        sent = True
    except Exception as error:  # noqa: BLE001
        await _report_repo_failure(
            guild, job, f"보고를 채널에 보내지 못했습니다: {error}", pending=len(report.commits)
        )
    if sent:
        # 커서와 함께 '언제 보고했는지'도 남긴다. 새 커밋이 없으면 침묵하는 것이 정상인
        # 기능이라, 이 값이 없으면 설정 탭에서 정상적인 침묵과 고장난 침묵이 똑같아 보인다.
        # 실패 기록도 함께 지운다 — 지우지 않으면 다음 실패가 '같은 사유'로 묵살된다.
        # 모든 브랜치 모드의 커서는 **읽은 시각의** ref 목록이다. 보내는 동안 누가
        # 푸시했다면 그 커밋은 다음 회차에 잡혀야 하므로, 지금 다시 읽어서는 안 된다.
        moved = (
            {"branch_cursors": report.ref_heads}
            if all_branches
            else {"last_commit": report.commits[-1].sha}
        )
        discordsched.update_job(job.get("id", ""), {
            **moved,
            "last_reported_at": datetime.now().isoformat(timespec="minutes"),
            "last_failure": "",
        })


async def _run_channel_report(job: dict) -> None:
    """Generate and send one report, committing cursors only after success."""
    import discordsched  # noqa: PLC0415

    guild = bound_guild()
    if guild is None:
        return
    lines, updated_sources, errors = await _collect_channel_report_messages(guild, job)
    if not lines:
        # Bot-only or empty traffic is safe to skip permanently and should not produce spam.
        if updated_sources != job.get("source_channels"):
            discordsched.update_job(job.get("id", ""), {"source_channels": updated_sources})
        return
    instruction = str(job.get("text") or "").strip()
    response_language = _job_response_language(job)
    user_prompt = (
        f"The following are {len(lines)} newly collected Discord messages since the last report.\n"
        + (f"Additional user instruction: {instruction}\n" if instruction else "")
        + (f"Collection warnings: {'; '.join(errors)}\n" if errors else "")
        + "\n[New source messages]\n"
        + "\n".join(lines)
    )
    try:
        async with _S.gen_lock:
            if _S.generate is None:
                return
            body = await asyncio.wait_for(
                _S.generate([
                    {"role": "system", "content": channel_report_system(response_language)},
                    {"role": "user", "content": user_prompt},
                ]),
                timeout=CHANNEL_REPORT_TIMEOUT_S,
            )
    except Exception as error:  # noqa: BLE001
        print(f"[discord] 채널 보고서 생성 실패: {error}")
        return
    body = str(body or "").strip()
    if not body:
        return
    destination_id = str(job.get("channel_id") or "")
    destination = guild.get_channel(int(destination_id)) if destination_id.isdigit() else None
    if destination is None:
        return
    source_names = ", ".join(f"#{item.get('name')}" for item in job.get("source_channels", []))
    report = f"**{_scheduled_heading('channel_report', response_language)}** · {source_names}\n\n{body}"
    try:
        for part in chunk_message(report):
            await destination.send(part, allowed_mentions=discord.AllowedMentions.none())
    except Exception as error:  # noqa: BLE001
        print(f"[discord] 채널 보고서 전송 실패: {error}")
        return
    discordsched.update_job(job.get("id", ""), {
        "source_channels": updated_sources,
        "last_reported_at": datetime.now().isoformat(timespec="minutes"),
    })


async def _run_job(job: dict) -> None:
    """예약 1건 실행 — missed는 실행 대신 명령 채널에 안내한다."""
    import discordops  # noqa: PLC0415
    import discordsched  # noqa: PLC0415

    guild = bound_guild()
    if guild is None:
        return
    if job.get("kind") == "channel_report":
        await _run_channel_report(job)
        return
    if job.get("kind") == "repo_report":
        await _run_repo_report(job)
        return
    if job.get("missed"):
        # 앱이 꺼져 있어 놓친 예약 → 그 서버의 명령 채널에 안내
        channel_id = command_channel_id(str(guild.id))
        cmd = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
        if cmd is not None:
            tail = "다음 회차에 다시 발화합니다." if job.get("repeat") == "daily" else "이 예약은 소진되어 삭제되었습니다."
            try:
                await cmd.send(f"⏰ 놓친 예약 안내 — 앱이 꺼져 있어 발화하지 못했습니다.\n{discordsched.render_job(job)}\n{tail}")
            except Exception:  # noqa: BLE001
                pass
        return
    if job.get("kind") == "briefing":
        response_language = _job_response_language(job)
        messages = [
            {"role": "system", "content": briefing_system(response_language)},
            {
                "role": "user",
                "content": (
                    f"{job.get('text')}\n"
                    f"(Current local time: {datetime.now().strftime('%Y-%m-%d %H:%M')})"
                ),
            },
        ]
        try:
            async with _S.gen_lock:  # 채팅과 생성을 직렬화(12B 단일 모델)
                if _S.research is not None:
                    body = await asyncio.wait_for(
                        _S.research(messages, response_language),
                        timeout=BRIEFING_TIMEOUT_S,
                    )
                elif _S.generate is not None:
                    body = await asyncio.wait_for(_S.generate(messages), timeout=BRIEFING_TIMEOUT_S)
                else:
                    return
        except asyncio.TimeoutError:
            body = "(브리핑 생성이 시간을 초과해 중단되었습니다)"
        except Exception as e:  # noqa: BLE001
            body = f"(브리핑 생성 실패: {e})"
        text = f"📋 **{_scheduled_heading('briefing', response_language)}** — {job.get('text', '')[:60]}\n\n{body}"
    else:
        text = str(job.get("text") or "")
    await discordops.send_message_live(guild, job.get("channel_id", ""), text)


async def _retry_command_channel() -> None:
    """명령 채널이 없으면 다시 확보를 시도한다.

    채널이 없으면 is_authorized 가 모든 메시지를 거부하므로 봇은 어디서도 대답하지 못한다.
    그런데 채널 확보는 서버에 고정되는 순간 딱 한 번만 시도했다 — 그때 권한이 아직 반영되지
    않았거나 일시적으로 실패하면, 앱을 다시 켜기 전까지 영영 침묵한다. 사용자가 채널을
    지운 경우도 같다. 러너가 주기적으로 확인해 스스로 복구한다.

    서버마다 따로 확인한다 — 한 서버에서 실패했다고 다른 서버의 복구를 멈추지 않는다."""
    for gid, state in list(_S.guilds.items()):
        if state.channel_id:
            continue
        guild = bound_guild(gid)
        if guild is None:
            continue
        try:
            await _ensure_command_channel(guild)
        except Exception as error:  # noqa: BLE001
            print(f"[discord] 명령 채널 재확보 실패({gid}): {error}")


async def _sched_runner(client: "discord.Client") -> None:
    import discordsched  # noqa: PLC0415

    await client.wait_until_ready()
    while not client.is_closed():
        try:
            await _retry_command_channel()
        except Exception as e:  # noqa: BLE001 — 복구 시도가 러너를 죽이면 안 된다
            print(f"[discord] 명령 채널 재시도 실패: {e}")
        try:
            for job in discordsched.pop_due():
                await _run_job(job)
        except Exception as e:  # noqa: BLE001 — 러너는 죽지 않는다
            print(f"[discord] 예약 러너 오류: {e}")
        await asyncio.sleep(SCHED_TICK_S)


async def apply_config(
    config: dict, generate: GenerateFn, step: "StepFn | None" = None,
    research: "ResearchFn | None" = None, image: "ImageFn | None" = None,
) -> None:
    """설정을 적용해 봇을 (재)시작하거나 중지한다. 토큰·활성만 넘기면 나머지는 자동.

    step이 주어지면 채팅이 서버 구성·전송·예약 도구를 쓰는 툴 루프로 동작하고,
    research가 주어지면 브리핑 예약이 발화 시각에 웹 조사로 내용을 생성한다."""
    await stop()
    _S.generate = generate
    _S.step = step
    _S.research = research
    _S.image = image
    _S.allow_attachment_images = bool(config.get("allow_attachment_images"))
    _S.data_dir = str(config.get("data_dir") or os.path.join(str(Path.home()), ".aiso", "discord"))
    _load_state()  # 저장된 guild·channel·allowlist 복원
    import discordsched  # noqa: PLC0415
    discordsched.configure(_S.data_dir)  # 예약 저장소 초기화(봇을 안 켜도 목록 API는 동작)
    if not config.get("enabled") or not str(config.get("token") or "").strip():
        return
    token = str(config["token"]).strip()
    # 여기까지 왔다는 것은 실제로 봇을 띄운다는 뜻이다. 꺼진 봇이 공급자를 표시하지
    # 않도록, 위의 조기 반환(비활성·토큰 없음)을 지난 뒤에만 기록한다.
    _S.provider = str(config.get("provider") or "")
    _S.model = str(config.get("model") or "")
    _S.synced_guilds.clear()
    client = _build_client(generate)
    _S.client = client
    _S.last_error = None

    async def _runner() -> None:
        try:
            await client.start(token)
        except discord.LoginFailure:
            _S.last_error = "로그인 실패 — 봇 토큰이 올바르지 않습니다."
            print("[discord] " + _S.last_error)
        except discord.PrivilegedIntentsRequired:
            _S.last_error = "Message Content Intent가 꺼져 있습니다 — 개발자 포털에서 켜세요."
            print("[discord] " + _S.last_error)
        except asyncio.CancelledError:
            raise  # stop()이 취소한 경우 — 거기서 이미 client.close() 처리
        except Exception as e:  # noqa: BLE001
            _S.last_error = f"봇 종료: {e}"
            print("[discord] " + _S.last_error)
        # 취소가 아닌 종료(로그인 실패·정상 종료)에 도달. 로그인 단계 실패는 client.close()를 부르지
        # 않아 is_closed()가 False로 남아 is_running()/status()가 '실행 중'으로 오보하고 세션이 상주한다.
        # 여기서 확실히 닫고 참조를 되돌린다(취소 경로는 위에서 raise로 건너뜀).
        try:
            if not client.is_closed():
                await client.close()
        except Exception:  # noqa: BLE001
            pass
        if _S.client is client:
            _S.client = None

    _S.task = asyncio.create_task(_runner())
    _S.sched_task = asyncio.create_task(_sched_runner(client))  # 예약 러너(클라이언트 준비 후 동작)


async def stop() -> None:
    if _S.client is not None:
        try:
            await _S.client.close()
        except Exception:  # noqa: BLE001
            pass
    if _S.task is not None:
        _S.task.cancel()
    if _S.sched_task is not None:
        _S.sched_task.cancel()
        _S.sched_task = None
    _S.client = None
    _S.task = None
    _S.tree = None
    _S.synced_guilds.clear()
    # 멈춘 봇이 공급자·서버를 계속 표시하면 "무엇으로 어디서 돌고 있나"에 거짓말을 하게 된다.
    _S.provider = ""
    _S.model = ""
    _S.guilds.clear()
