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
import functools
import json
import os
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

import discord
from discord import app_commands

# 전체 응답 텍스트를 돌려주는 생성 함수(main.py가 Ollama 기계를 재사용해 주입).
GenerateFn = Callable[[list], Awaitable[str]]
# 한 턴 생성 — (messages, tools|None) → {"content": str, "tool_calls": list}. 서버 구성 루프용.
StepFn = Callable[[list, "list | None"], Awaitable[dict]]
# 웹 조사 기반 생성(리서치 루프) — 브리핑 예약이 발화 시각에 내용을 만들 때 사용.
ResearchFn = Callable[[list], Awaitable[str]]

DISCORD_MSG_LIMIT = 2000       # 디스코드 단일 메시지 길이 상한
HISTORY_TURNS = 12             # 채널별 최근 대화 유지(간단한 문맥)
COMMAND_CHANNEL = "aiso"       # 자동 생성할 명령 채널 이름
STATE_FILE = "state.json"      # data_dir 안에 봇 동적 상태 영속
MAX_TOOL_TURNS = 6             # 서버 구성 루프의 생성 턴 상한(폭주 방지)
APPROVAL_TIMEOUT_S = 120       # 서버 구성 승인 버튼 대기 시간(초) — 지나면 자동 취소
SYSTEM_PROMPT = "너는 Aiso, 사용자 PC에서 로컬로 도는 도우미다. 한국어로 간결하고 친근하게 답한다."


@functools.lru_cache(maxsize=1)  # 완전 정적 문자열 — 매 메시지 재조립하지 않고 1회 계산해 캐시
def _tools_prompt() -> str:
    import discordops  # noqa: PLC0415 — 설계 기준·형식은 discordops가 단일 출처

    return SYSTEM_PROMPT + (
        "\n\n서버 구성 도구를 쓸 수 있다: 사용자가 이 디스코드 서버의 채널 구성(팀 카테고리·채널 생성, "
        "이름 변경, 이동, 주제 설정, 삭제)을 요청하면 (1) discord_server_map으로 현재 구조를 확인하고 "
        "(2) 설계해 discord_server_apply(ops=[...])로 적용하라. "
        "설계가 끝나면 텍스트로 다시 묻지 말고 바로 discord_server_apply를 호출하라 — 호출하면 사용자에게 "
        "계획 미리보기와 승인/취소 버튼이 자동으로 뜬다. 삭제도 마찬가지다: 사용자가 이미 명확히 요청했다면 "
        "텍스트로 재확인하지 말고 바로 호출하라(승인 버튼이 최종 확인이다). "
        "#aiso(명령 채널)는 보호되어 있으니 삭제·변경 목록에 넣지 마라 — 넣어도 자동 제외된다. "
        "일반 대화에는 도구를 쓰지 말고 그냥 답하라.\n"
        "메시지 전송: discord_send(channel, message). "
        "예약 전송: discord_schedule_add(channel, text, when, repeat, kind) — when은 'HH:MM' 또는 "
        "'YYYY-MM-DD HH:MM', repeat는 once(1회)/daily(매일), kind는 message(고정 문구)/"
        "briefing(그 시각에 웹 조사로 내용을 생성 — 날씨·뉴스 브리핑용). "
        "예약 목록은 discord_schedule_list, 삭제는 discord_schedule_remove(id). "
        "예약은 앱이 켜져 있는 동안만 발화된다. "
        "전송·예약도 서버 구성과 똑같다: 호출하면 소유자에게 승인 버튼이 자동으로 뜨니 '~할까요?'라고 "
        "텍스트로 되묻지 말고, 채널·내용·시각이 있으면 곧바로 discord_send/discord_schedule_add를 호출하라. "
        "정보가 부족할 때만 딱 한 번 물어라. 반복이 불명확하면 once. "
        "한국어 시각은 24시간제로 변환하라(오후 11시 45분=23:45; 오전/오후가 없으면 다가오는 가장 가까운 시각).\n\n"
        + discordops.DESIGN_GUIDE + "\n\n"
        + "ops의 각 항목은 반드시 다음 필드명을 그대로 써라(op·parent·type 같은 다른 이름은 인식되지 않는다): "
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


class _State:
    def __init__(self) -> None:
        self.client: "discord.Client | None" = None
        self.task: "asyncio.Task | None" = None
        self.generate: GenerateFn | None = None
        self.step: StepFn | None = None    # 툴콜 지원 한 턴 생성(서버 구성 루프용)
        self.research: ResearchFn | None = None  # 웹 조사 생성(브리핑 예약용)
        self.sched_task: "asyncio.Task | None" = None  # 예약 러너
        self.tree: "app_commands.CommandTree | None" = None
        self.data_dir: str = ""
        # 런타임에 자동 판별/관리되는 동적 상태
        self.owner_id: str = ""          # application_info 제작자
        self.app_id: str = ""            # 봇 애플리케이션 ID(=봇 user id) — 초대 링크 생성용
        self.guild_id: str = ""          # 고정된 단일 서버
        self.channel_id: str = ""        # 자동 생성 명령 채널
        self.allowlist: set[str] = set() # 슬래시로 동적 관리
        self.synced: bool = False
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
        "guild_id": _S.guild_id,
        "channel_id": _S.channel_id,
        "allowlist": sorted(_S.allowlist),
        "last_error": _S.last_error,
    }


def bound_guild():
    """고정된 단일 서버의 라이브 길드 객체 — 없으면 None. (discordops가 서버 구성에 사용)"""
    if not is_running() or not _S.guild_id.isdigit():
        return None
    return _S.client.get_guild(int(_S.guild_id))


def command_channel_id() -> str:
    return _S.channel_id


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
        _S.guild_id = str(d.get("guild_id") or "")
        _S.channel_id = str(d.get("channel_id") or "")
        _S.allowlist = {str(x) for x in (d.get("allowlist") or [])}
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
                {"guild_id": _S.guild_id, "channel_id": _S.channel_id, "allowlist": sorted(_S.allowlist)},
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
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    view_send = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    owner_m = await _resolve_member(guild, _S.owner_id)
    if owner_m is not None:
        ow[owner_m] = view_send
    guild_owner = guild.owner or await _resolve_member(guild, str(guild.owner_id or ""))
    if guild_owner is not None:
        ow[guild_owner] = view_send
    for uid in list(_S.allowlist):
        m = await _resolve_member(guild, uid)
        if m is not None:
            ow[m] = view_send
    return ow


async def _refresh_command_overwrites() -> None:
    """허용목록이 바뀌면 명령 채널 권한을 다시 적용해 새 허용자가 채널을 볼 수 있게 한다."""
    guild = bound_guild()
    if guild is None or not _S.channel_id.isdigit():
        return
    ch = guild.get_channel(int(_S.channel_id))
    if ch is None:
        return
    try:
        await ch.edit(overwrites=await _lock_overwrites(guild), reason="Aiso 허용목록 갱신")
    except Exception:  # noqa: BLE001 — 권한 부족 등은 조용히 무시(다음 재연결 시 반영)
        pass


async def _ensure_command_channel(guild: "discord.Guild") -> None:
    """소유자+봇만 보이는 잠금 명령 채널을 확보한다(있으면 재사용, 없으면 생성)."""
    # 저장된 채널이 아직 유효하면 그대로 사용(우리가 만든 잠금 채널로 신뢰)
    if _S.channel_id:
        existing = guild.get_channel(int(_S.channel_id)) if _S.channel_id.isdigit() else None
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
            _S.channel_id = str(ch.id)
            _save_state()
            return
    # 없으면 잠금 채널 생성 — @everyone 숨김, 봇·소유자·서버주인만 열람
    try:
        ch = await guild.create_text_channel(
            COMMAND_CHANNEL, overwrites=await _lock_overwrites(guild), reason="Aiso 명령 채널 자동 생성"
        )
        _S.channel_id = str(ch.id)
        _save_state()
        await ch.send(
            "👋 Aiso 봇이 연결되었습니다. 이 채널에서 말을 걸면 로컬 모델이 답합니다. "
            "\"팀 서버로 꾸며줘\"처럼 서버 구성도 요청할 수 있습니다."
        )
    except discord.Forbidden:
        _S.last_error = "채널 생성 권한(Manage Channels)이 없습니다 — 초대 권한을 확인하세요."
    except Exception as e:  # noqa: BLE001
        _S.last_error = f"명령 채널 생성 실패: {e}"


async def _bind_guild(guild: "discord.Guild") -> None:
    """단일 서버로 고정하고 명령 채널을 확보한 뒤 슬래시 커맨드를 동기화한다."""
    _S.guild_id = str(guild.id)
    _save_state()
    await _ensure_command_channel(guild)
    if _S.tree is not None and not _S.synced:
        try:
            # 명령은 전역(guild 인자 없이)으로 등록돼 있다. 길드 sync에 포함되려면 먼저 전역 명령을
            # 이 길드로 복사해야 한다 — 안 하면 빈 배열이 올라가 /allow가 하나도 안 뜬다.
            guild_obj = discord.Object(id=guild.id)
            _S.tree.copy_global_to(guild=guild_obj)
            await _S.tree.sync(guild=guild_obj)
            _S.synced = True
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
    snap = discordops.snapshot_guild(guild, _S.channel_id)
    clean, skipped, error_msg = discordops.prepare_ops(ops, snap)  # 보호대상 분리 + 검증(server_apply와 공유)
    if error_msg:
        return error_msg
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
    ch_id, ch_name = got
    draft, derr = discordsched.build_job(
        channel_id=ch_id, channel_name=ch_name, kind=a["kind"], text=a["text"],
        when=a["when"], repeat=a["repeat"],
    )
    if derr:
        return f"[거부] {derr}"
    if not await _ask_owner_approval(channel, discordsched.render_add_preview(draft)):
        return _REJECTED
    return "예약이 등록되었습니다.\n" + discordsched.render_job(discordsched.commit_job(draft))


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
    if name == "discord_schedule_list":
        return await discordsched.schedule_list()
    if name == "discord_schedule_remove":
        # 삭제는 버튼 없이 소유자 본인 요청만 허용(허용목록 사용자가 남의 예약을 지우지 못하게)
        if not _S.owner_id or str(author_id) != _S.owner_id:
            return "[거부] 예약 삭제는 소유자만 할 수 있습니다."
        return await discordsched.schedule_remove(**(args or {}))
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

    tools = [
        discordops.MAP_SCHEMA, discordops.APPLY_SCHEMA, discordops.SEND_SCHEMA,
        discordsched.SCHEDULE_ADD_SCHEMA, discordsched.SCHEDULE_LIST_SCHEMA,
        discordsched.SCHEDULE_REMOVE_SCHEMA,
    ]
    allowed_tool_names = {
        str((tool.get("function") or {}).get("name") or "") for tool in tools
    }
    convo = list(convo)
    completed_provider_calls: dict[str, tuple[str, str]] = {}
    for _ in range(MAX_TOOL_TURNS):
        async with _S.gen_lock:  # 생성만 직렬화한다 — 뒤이은 승인 대기는 락 밖에서
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

    @allow.command(name="add", description="허용 사용자를 추가합니다")
    @app_commands.describe(user="추가할 사용자")
    async def allow_add(interaction: "discord.Interaction", user: "discord.User") -> None:
        if not _owner_only(interaction):
            await interaction.response.send_message("소유자만 사용할 수 있습니다.", ephemeral=True)
            return
        _S.allowlist.add(str(user.id))
        _save_state()
        await _refresh_command_overwrites()  # 새 허용자가 #aiso 채널을 볼 수 있게 권한 갱신
        await interaction.response.send_message(f"✅ 허용 추가: {user} (`{user.id}`)", ephemeral=True)

    @allow.command(name="remove", description="허용 사용자를 제거합니다")
    @app_commands.describe(user="제거할 사용자")
    async def allow_remove(interaction: "discord.Interaction", user: "discord.User") -> None:
        if not _owner_only(interaction):
            await interaction.response.send_message("소유자만 사용할 수 있습니다.", ephemeral=True)
            return
        _S.allowlist.discard(str(user.id))
        _save_state()
        await _refresh_command_overwrites()  # 제거된 사용자의 채널 접근 회수
        await interaction.response.send_message(f"🗑 허용 제거: {user} (`{user.id}`)", ephemeral=True)

    @allow.command(name="list", description="허용 사용자 목록을 봅니다")
    async def allow_list(interaction: "discord.Interaction") -> None:
        if not _owner_only(interaction):
            await interaction.response.send_message("소유자만 사용할 수 있습니다.", ephemeral=True)
            return
        ids = sorted(_S.allowlist)
        body = "\n".join(f"• <@{i}> (`{i}`)" for i in ids) if ids else "(없음)"
        await interaction.response.send_message(f"허용 사용자:\n{body}", ephemeral=True)

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
        # 이미 서버에 들어가 있으면(재시작 등) 첫 서버로 고정·명령채널 확보
        guilds = list(client.guilds)
        if guilds:
            keep = None
            if _S.guild_id:
                keep = discord.utils.get(guilds, id=int(_S.guild_id)) if _S.guild_id.isdigit() else None
            keep = keep or guilds[0]
            for g in guilds:
                if g.id != keep.id:
                    try:
                        await g.leave()  # 단일 서버 초과분 퇴장
                    except Exception:  # noqa: BLE001
                        pass
            await _bind_guild(keep)

    @client.event
    async def on_guild_join(guild: "discord.Guild") -> None:
        # 최대 1개 서버 — 이미 다른 서버에 고정돼 있으면 새 초대는 퇴장
        if _S.guild_id and _S.guild_id.isdigit() and int(_S.guild_id) != guild.id:
            try:
                sys_ch = guild.system_channel
                if sys_ch is not None:
                    await sys_ch.send("이미 다른 서버에 연결되어 있어 이 서버에서는 나갑니다 (봇당 1개 서버).")
            except Exception:  # noqa: BLE001
                pass
            try:
                await guild.leave()
            except Exception:  # noqa: BLE001
                pass
            return
        await _bind_guild(guild)

    @client.event
    async def on_message(message: "discord.Message") -> None:
        if client.user is not None and message.author.id == client.user.id:
            return
        if message.author.bot:
            return
        if not is_authorized(_S.owner_id, _S.channel_id, _S.allowlist, message.author.id, message.channel.id):
            return  # 비인가·비지정채널 → 무응답
        text = (message.content or "").strip()
        if not text:
            return
        hist = _S.history[message.channel.id]
        use_tools = _S.step is not None  # 서버 구성 도구를 쓸 수 있으면 툴 루프로
        messages = [{"role": "system", "content": _tools_prompt() if use_tools else SYSTEM_PROMPT}]
        for role, content in hist:
            messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": text})
        try:
            async with message.channel.typing():
                if use_tools:
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

BRIEFING_SYSTEM = (
    "너는 Aiso다. 아래 지시에 따라 디스코드 채널에 보낼 브리핑을 작성하라. "
    "간결한 마크다운(굵게·목록)으로, 인사말 없이 본문만. 한국어로 쓴다."
)


async def _run_job(job: dict) -> None:
    """예약 1건 실행 — missed는 실행 대신 명령 채널에 안내한다."""
    import discordops  # noqa: PLC0415
    import discordsched  # noqa: PLC0415

    guild = bound_guild()
    if guild is None:
        return
    if job.get("missed"):
        # 앱이 꺼져 있어 놓친 예약 → 명령 채널에 안내(일회성은 소진됨, 매일은 다음 회차 예정)
        cmd = guild.get_channel(int(_S.channel_id)) if _S.channel_id.isdigit() else None
        if cmd is not None:
            tail = "다음 회차에 다시 발화합니다." if job.get("repeat") == "daily" else "이 예약은 소진되어 삭제되었습니다."
            try:
                await cmd.send(f"⏰ 놓친 예약 안내 — 앱이 꺼져 있어 발화하지 못했습니다.\n{discordsched.render_job(job)}\n{tail}")
            except Exception:  # noqa: BLE001
                pass
        return
    if job.get("kind") == "briefing":
        messages = [
            {"role": "system", "content": BRIEFING_SYSTEM},
            {"role": "user", "content": f"{job.get('text')}\n(현재 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')})"},
        ]
        try:
            async with _S.gen_lock:  # 채팅과 생성을 직렬화(12B 단일 모델)
                if _S.research is not None:
                    body = await asyncio.wait_for(_S.research(messages), timeout=BRIEFING_TIMEOUT_S)
                elif _S.generate is not None:
                    body = await asyncio.wait_for(_S.generate(messages), timeout=BRIEFING_TIMEOUT_S)
                else:
                    return
        except asyncio.TimeoutError:
            body = "(브리핑 생성이 시간을 초과해 중단되었습니다)"
        except Exception as e:  # noqa: BLE001
            body = f"(브리핑 생성 실패: {e})"
        text = f"📋 **예약 브리핑** — {job.get('text', '')[:60]}\n\n{body}"
    else:
        text = str(job.get("text") or "")
    await discordops.send_message_live(guild, job.get("channel_id", ""), text)


async def _sched_runner(client: "discord.Client") -> None:
    import discordsched  # noqa: PLC0415

    await client.wait_until_ready()
    while not client.is_closed():
        try:
            for job in discordsched.pop_due():
                await _run_job(job)
        except Exception as e:  # noqa: BLE001 — 러너는 죽지 않는다
            print(f"[discord] 예약 러너 오류: {e}")
        await asyncio.sleep(SCHED_TICK_S)


async def apply_config(
    config: dict, generate: GenerateFn, step: "StepFn | None" = None,
    research: "ResearchFn | None" = None,
) -> None:
    """설정을 적용해 봇을 (재)시작하거나 중지한다. 토큰·활성만 넘기면 나머지는 자동.

    step이 주어지면 채팅이 서버 구성·전송·예약 도구를 쓰는 툴 루프로 동작하고,
    research가 주어지면 브리핑 예약이 발화 시각에 웹 조사로 내용을 생성한다."""
    await stop()
    _S.generate = generate
    _S.step = step
    _S.research = research
    _S.data_dir = str(config.get("data_dir") or os.path.join(str(Path.home()), ".aiso", "discord"))
    _load_state()  # 저장된 guild·channel·allowlist 복원
    import discordsched  # noqa: PLC0415
    discordsched.configure(_S.data_dir)  # 예약 저장소 초기화(봇을 안 켜도 목록 API는 동작)
    if not config.get("enabled") or not str(config.get("token") or "").strip():
        return
    token = str(config["token"]).strip()
    _S.synced = False
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
    _S.synced = False
