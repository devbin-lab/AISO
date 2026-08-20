# -*- coding: utf-8 -*-
"""디스코드 예약 코어 — 등록·영속·발화 시각 계산. discord.py 미의존(순수 파이썬).

- 예약 등록의 실행 권한은 에이전트 탭에서 선택한 권한 모드를 따른다.
  디스코드 명령 채널의 소유자 승인과 실제 발화는 별도 흐름으로 처리한다.
- 발화는 앱(사이드카)이 켜져 있는 동안만 일어난다. 꺼져 있던 동안 놓친 예약은
  다음 시작 때 명령 채널에 안내한다(일회성은 소진, 매일 반복은 다음 회차로).
- 저장은 discord data_dir의 schedules.json — 임시파일에 쓰고 교체(원자적).
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

SCHEDULES_FILE = "schedules.json"
MAX_JOBS = 20          # 등록 가능한 예약 수 상한(폭주 방지)
TEXT_MAX = 2000        # 메시지 본문/브리핑 지시 길이 상한
MISSED_GRACE_S = 600   # 이보다 오래 지난 발화는 '놓침'으로 처리(실행 대신 안내)
MAX_REPORT_CHANNELS = 10
MAX_REPORT_INSTRUCTION = 1000
MIN_REPORT_INTERVAL_HOURS = 1
MAX_REPORT_INTERVAL_HOURS = 168

KINDS = ("message", "briefing")
REPEATS = ("once", "daily")

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DATETIME_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})$")

# ── 저장소(모듈 싱글턴 — discordbot.apply_config가 configure로 초기화) ──
_DIR: "Path | None" = None
_JOBS: list[dict] = []


def configure(data_dir: str) -> None:
    global _DIR, _JOBS
    _DIR = Path(data_dir) if data_dir else None
    _JOBS = _load()


def _path() -> "Path | None":
    return (_DIR / SCHEDULES_FILE) if _DIR else None


def _load() -> list[dict]:
    p = _path()
    if not p or not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    if not isinstance(data, list):  # null·숫자·객체 등으로 손상 → 빈 목록으로 복구(순회 TypeError 방지)
        return []
    return [j for j in data if isinstance(j, dict) and j.get("id")]


def _save() -> None:
    p = _path()
    if not p:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(_JOBS, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)  # 원자적 교체 — 저장 중 크래시에도 파일이 깨지지 않게
    except OSError:
        pass


def jobs() -> list[dict]:
    return [dict(j) for j in _JOBS]


def remove(job_id: str) -> bool:
    global _JOBS
    jid = str(job_id or "").strip()
    before = len(_JOBS)
    _JOBS = [j for j in _JOBS if j.get("id") != jid]
    if len(_JOBS) != before:
        _save()
        return True
    return False


def update_job(job_id: str, changes: dict) -> bool:
    """Persist runtime state for an existing schedule job."""
    jid = str(job_id or "").strip()
    if not jid or not isinstance(changes, dict):
        return False
    for index, job in enumerate(_JOBS):
        if job.get("id") != jid:
            continue
        updated = dict(job)
        updated.update(changes)
        _JOBS[index] = updated
        _save()
        return True
    return False


# ── 시각 파싱·반복 계산 ─────────────────────────────────────────────────
def parse_when(when, repeat: str, *, now: "datetime | None" = None) -> tuple["datetime | None", "str | None"]:
    """'HH:MM' 또는 'YYYY-MM-DD HH:MM' → 첫 발화 시각. 반환 (시각, 오류)."""
    now = now or datetime.now()
    w = str(when or "").strip()
    m = _TIME_RE.match(w)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None, f"시각이 올바르지 않습니다: '{w}'"
        cand = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)  # 오늘 이미 지난 시각 → 다음 발생(내일)
        return cand, None
    m = _DATETIME_RE.match(w)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)))
        except ValueError:
            return None, f"날짜가 올바르지 않습니다: '{w}'"
        if dt <= now:
            if repeat == "daily":  # 매일 반복이면 그 시각의 다음 발생으로
                cand = now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
                return (cand + timedelta(days=1)) if cand <= now else cand, None
            return None, f"이미 지난 시각입니다: '{w}'"
        return dt, None
    return None, f"when 형식이 올바르지 않습니다: '{w}' — 'HH:MM'(예: 22:55) 또는 'YYYY-MM-DD HH:MM'(예: 2026-07-17 08:00)"


def _parse_next_run(raw) -> "datetime | None":
    """저장된 next_run을 naive datetime으로 안전 파싱. 비문자열·형식오류·tz-aware를 모두 흡수한다.

    (fromisoformat은 비문자열에 TypeError를, tz-aware 값은 naive now와 비교 시 TypeError를 낸다 —
    둘 다 여기서 막지 않으면 pop_due가 매 틱 예외로 죽어 모든 예약이 굶는다.)"""
    if not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def advance_daily(next_run: datetime, now: datetime) -> datetime:
    """매일 반복의 다음 발화 시각 — 예정 시각 기준으로 하루씩 더해 드리프트를 막는다."""
    nr = next_run + timedelta(days=1)
    while nr <= now:
        nr += timedelta(days=1)
    return nr


def advance_interval(next_run: datetime, now: datetime, interval_hours: int) -> datetime:
    """Advance an hourly interval without replaying every missed tick."""
    hours = max(MIN_REPORT_INTERVAL_HOURS, min(MAX_REPORT_INTERVAL_HOURS, int(interval_hours)))
    nr = next_run + timedelta(hours=hours)
    while nr <= now:
        nr += timedelta(hours=hours)
    return nr


def pop_due(*, now: "datetime | None" = None) -> list[dict]:
    """발화 시각이 된 잡을 꺼낸다. 반복은 next_run을 갱신해 유지, 일회성은 제거(소진).

    발화 시각을 MISSED_GRACE_S 넘게 지난 잡은 missed=True로 반환한다(실행 대신 안내용).

    의도적 at-most-once: 여기서 먼저 소진/갱신·저장한 뒤 호출부가 전송한다. 전송 전 앱 종료 등으로
    그 회차를 놓칠 수는 있어도, 같은 메시지를 채널에 두 번 쏘지는 않는다(자율 발신에선 중복이 더 나쁘다)."""
    global _JOBS
    now = now or datetime.now()
    fired: list[dict] = []
    keep: list[dict] = []
    changed = False
    for j in _JOBS:
        nr = _parse_next_run(j.get("next_run"))
        if nr is None:
            changed = True  # 깨진(비문자열·형식오류) next_run → 잡을 버린다
            continue
        interval_hours: int | None = None
        if j.get("repeat") == "interval" or j.get("kind") == "channel_report":
            try:
                interval_hours = int(j.get("interval_hours"))
            except (TypeError, ValueError):
                changed = True
                continue
            if not MIN_REPORT_INTERVAL_HOURS <= interval_hours <= MAX_REPORT_INTERVAL_HOURS:
                changed = True
                continue
        if nr > now:
            keep.append(j)
            continue
        missed = (now - nr).total_seconds() > MISSED_GRACE_S
        fired.append({**j, "missed": missed})
        changed = True
        if j.get("repeat") == "daily":
            j2 = dict(j)
            j2["next_run"] = advance_daily(nr, now).isoformat(timespec="minutes")
            keep.append(j2)
        elif j.get("repeat") == "interval" and j.get("kind") == "channel_report":
            j2 = dict(j)
            j2["next_run"] = advance_interval(
                nr, now, interval_hours or MIN_REPORT_INTERVAL_HOURS
            ).isoformat(timespec="minutes")
            keep.append(j2)
        # once는 소진 — keep하지 않음
    if changed:
        _JOBS = keep
        _save()
    return fired


# ── 등록(검증 포함) ─────────────────────────────────────────────────────
def canonical_add_args(args: dict) -> dict:
    """약한 모델의 필드명 편차를 표준(channel/text/when/repeat/kind)으로 정규화."""
    from discordops import pick_first  # noqa: PLC0415 — 정규화 헬퍼는 discordops가 단일 출처(discord 미의존)

    a = dict(args or {})
    out = {
        "channel": pick_first(a, "channel", "channel_name", "target", "room"),
        "text": pick_first(a, "text", "message", "content", "prompt", "body"),
        "when": pick_first(a, "when", "time", "at", "datetime", "date"),
        "repeat": pick_first(a, "repeat", "recurrence", "cycle", "frequency").lower(),
        "kind": pick_first(a, "kind", "type", "mode").lower(),
    }
    if out["repeat"] in ("매일", "everyday", "every_day", "daily"):
        out["repeat"] = "daily"
    elif out["repeat"] in ("", "once", "one_time", "onetime", "1회", "한번", "일회"):
        out["repeat"] = "once"
    if out["kind"] in ("브리핑", "briefing", "brief", "report"):
        out["kind"] = "briefing"
    elif out["kind"] in ("", "message", "msg", "메시지", "text"):
        out["kind"] = "message"
    return out


def build_job(
    *, channel_id: str, channel_name: str, kind: str, text: str, when, repeat: str,
    now: "datetime | None" = None,
) -> tuple["dict | None", "str | None"]:
    """모든 검증을 마친 저장 직전 draft(id·created 제외)를 만든다. 등록은 하지 않는다.

    검증(개수·kind·repeat·본문·시각)을 여기 한 곳에 모아, 승인 전 미리보기와 실제 등록이 같은 결과를
    쓰게 한다(승인 후에야 '너무 많음/너무 김'으로 거부되거나, 시각이 재파싱돼 드리프트되는 것을 방지)."""
    if len(_JOBS) >= MAX_JOBS:
        return None, f"예약이 너무 많습니다(최대 {MAX_JOBS}개) — 먼저 기존 예약을 삭제하세요"
    if kind not in KINDS:
        return None, f"kind는 message(고정 메시지) 또는 briefing(그 시각에 내용 생성)이어야 합니다: '{kind}'"
    if repeat not in REPEATS:
        return None, f"repeat는 once(1회) 또는 daily(매일)여야 합니다: '{repeat}'"
    body = str(text or "").strip()
    if not body:
        return None, "text(보낼 메시지 또는 브리핑 지시)가 비어 있습니다"
    if len(body) > TEXT_MAX:
        return None, f"text가 너무 깁니다({len(body)}자) — 최대 {TEXT_MAX}자"
    first, err = parse_when(when, repeat, now=now)
    if err:
        return None, err
    return {
        "kind": kind,
        "channel_id": str(channel_id),
        "channel_name": str(channel_name),
        "text": body,
        "repeat": repeat,
        "next_run": first.isoformat(timespec="minutes"),
    }, None


def commit_job(draft: dict, *, now: "datetime | None" = None) -> dict:
    """build_job이 만든 draft에 id·created를 붙여 등록·영속한다."""
    job = {"id": uuid.uuid4().hex[:8], **draft, "created": (now or datetime.now()).isoformat(timespec="minutes")}
    _JOBS.append(job)
    _save()
    return dict(job)


def add_job(**kwargs) -> tuple["dict | None", "str | None"]:
    """검증 후 예약을 등록한다(build_job + commit_job). 반환 (잡, 오류). 에이전트 탭 핸들러가 사용."""
    draft, err = build_job(**kwargs)
    if err:
        return None, err
    return commit_job(draft, now=kwargs.get("now")), None


def canonical_channel_report_args(args: dict) -> dict:
    """Normalize common small-model aliases for the channel report tool."""
    from discordops import pick_first  # noqa: PLC0415

    raw = dict(args or {})
    channels = raw.get("channels")
    if channels is None:
        channels = raw.get("source_channels")
    if channels is None:
        channels = raw.get("channel")
    if isinstance(channels, str):
        channels = [part.strip() for part in re.split(r"[,\n]", channels) if part.strip()]
    elif isinstance(channels, (tuple, set)):
        channels = list(channels)
    if not isinstance(channels, list):
        channels = []
    return {
        "channels": [str(value).strip() for value in channels if str(value).strip()],
        "report_channel": pick_first(
            raw, "report_channel", "destination", "target_channel", "output_channel"
        ),
        "interval_hours": raw.get("interval_hours", raw.get("hours", raw.get("interval", 1))),
        "instruction": pick_first(raw, "instruction", "focus", "prompt", "text"),
    }


def build_channel_report_job(
    *,
    source_channels: list[dict],
    report_channel_id: str,
    report_channel_name: str,
    interval_hours: int,
    instruction: str = "",
    now: "datetime | None" = None,
) -> tuple["dict | None", "str | None"]:
    """Validate and build a recurring new-message-only Discord report job."""
    if len(_JOBS) >= MAX_JOBS:
        return None, f"예약이 너무 많습니다(최대 {MAX_JOBS}개). 먼저 기존 예약을 삭제하세요."
    if not source_channels:
        return None, "수집할 텍스트 채널이 없습니다."
    if len(source_channels) > MAX_REPORT_CHANNELS:
        return None, f"한 보고서에서 최대 {MAX_REPORT_CHANNELS}개 채널까지 수집할 수 있습니다."
    try:
        hours = int(interval_hours)
    except (TypeError, ValueError):
        return None, "interval_hours는 시간 단위 정수여야 합니다."
    if not MIN_REPORT_INTERVAL_HOURS <= hours <= MAX_REPORT_INTERVAL_HOURS:
        return None, (
            f"interval_hours는 {MIN_REPORT_INTERVAL_HOURS}~{MAX_REPORT_INTERVAL_HOURS} 사이여야 합니다."
        )
    note = str(instruction or "").strip()
    if len(note) > MAX_REPORT_INSTRUCTION:
        return None, f"보고서 지시는 최대 {MAX_REPORT_INSTRUCTION}자까지 입력할 수 있습니다."
    seen: set[str] = set()
    clean_sources: list[dict] = []
    for source in source_channels:
        channel_id = str(source.get("id") or "").strip()
        channel_name = str(source.get("name") or "").strip()
        if not channel_id or not channel_name or channel_id in seen:
            continue
        seen.add(channel_id)
        clean_sources.append({
            "id": channel_id,
            "name": channel_name,
            "last_message_id": str(source.get("last_message_id") or "0"),
        })
    if not clean_sources:
        return None, "유효한 수집 채널이 없습니다."
    current = now or datetime.now()
    return {
        "kind": "channel_report",
        "channel_id": str(report_channel_id),
        "channel_name": str(report_channel_name),
        "source_channels": clean_sources,
        "text": note,
        "repeat": "interval",
        "interval_hours": hours,
        "next_run": (current + timedelta(hours=hours)).isoformat(timespec="minutes"),
    }, None


async def _latest_message_id(channel) -> str:
    try:
        async for message in channel.history(limit=1):
            return str(message.id)
    except Exception:  # noqa: BLE001 - permission and API errors are reported by the caller
        raise
    return "0"


async def prepare_channel_report(
    channels=None, report_channel=None, interval_hours=1, instruction="", **_kw
) -> tuple["dict | None", "dict | None", "str | None"]:
    """검증·채널 해석·베이스라인 수집까지 마친 draft를 만든다. 등록은 하지 않는다.

    승인이 필요한 경로(디스코드 봇)가 **승인 전에** 모든 거부 사유를 확인할 수 있도록
    커밋과 분리한다. 예전에는 승인 버튼을 누른 뒤에야 채널 해석·권한·개수·주기 검증이
    돌아서 "승인했는데 거부"가 났고, 미리보기도 해석 전 값이라 실제 등록과 어긋났다
    (보고 채널을 생략하면 미리보기엔 "#(없음)"이 뜨는데 실제로는 첫 수집 채널로 등록됐다).

    `_schedule_add_with_approval`이 쓰는 build_job/commit_job 분리와 같은 형태다.
    베이스라인 읽기는 부작용 없는 read이므로 승인 전 실행이 안전하다.

    반환: (draft, meta, error). meta는 미리보기에 쓸 해석된 이름들.
    """
    import discordops  # noqa: PLC0415

    args = canonical_channel_report_args({
        "channels": channels,
        "report_channel": report_channel,
        "interval_hours": interval_hours,
        "instruction": instruction,
        **_kw,
    })
    if not args["channels"]:
        return None, None, "[거부] 수집할 채널을 하나 이상 지정하세요."
    destination = args["report_channel"] or args["channels"][0]
    report_got, report_error = discordops.resolve_text_channel(destination)
    if report_error:
        return None, None, f"[거부] 보고 채널: {report_error}"
    live, live_error = discordops.live_guild()
    if live_error:
        return None, None, live_error
    guild, _command_channel_id = live
    sources: list[dict] = []
    for requested in args["channels"]:
        got, error = discordops.resolve_text_channel(requested)
        if error:
            return None, None, f"[거부] 수집 채널 '{requested}': {error}"
        channel_id, channel_name = got
        channel = guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
        if channel is None:
            return None, None, f"[거부] 수집 채널 '#{channel_name}'을 찾을 수 없습니다."
        try:
            baseline = await _latest_message_id(channel)
        except Exception as error:  # noqa: BLE001
            return None, None, f"[거부] #{channel_name}의 메시지 기록을 읽을 수 없습니다: {error}"
        sources.append({"id": channel_id, "name": channel_name, "last_message_id": baseline})
    report_id, report_name = report_got
    draft, error = build_channel_report_job(
        source_channels=sources,
        report_channel_id=report_id,
        report_channel_name=report_name,
        interval_hours=args["interval_hours"],
        instruction=args["instruction"],
    )
    if error:
        return None, None, f"[거부] {error}"
    meta = {
        "source_names": [source["name"] for source in sources],
        "report_name": report_name,
        "interval_hours": draft["interval_hours"],
        "instruction": args["instruction"],
    }
    return draft, meta, None


def render_channel_report_registered(job: dict, meta: dict) -> str:
    source_names = ", ".join(f"#{name}" for name in meta["source_names"])
    return (
        f"채널 대화 보고 예약을 등록했습니다.\n"
        f"수집: {source_names}\n보고: #{meta['report_name']}\n"
        f"주기: {job['interval_hours']}시간마다\n"
        "등록 시점 이후의 새 메시지만 보고하며, 성공적으로 보고한 메시지는 다시 수집하지 않습니다."
    )


def render_channel_report_preview(meta: dict) -> str:
    """승인 미리보기 — 해석된 실제 채널명과 정규화된 주기를 보여 준다."""
    sources = ", ".join(f"#{name}" for name in meta["source_names"]) or "(없음)"
    preview = (
        "채널 대화 보고 예약 요청\n"
        f"수집: {sources}\n보고: #{meta['report_name']}\n"
        f"주기: {meta['interval_hours']}시간마다\n"
        "등록 이후 새 메시지만 수집하며 성공적으로 보고한 메시지는 다시 포함하지 않습니다."
    )
    if meta["instruction"]:
        preview += f"\n추가 지시: {meta['instruction']}"
    return preview


async def channel_report_add(
    channels=None, report_channel=None, interval_hours=1, instruction="", **_kw
) -> str:
    """Register an interval report and baseline each source at its current newest message.

    에이전트 탭 툴 진입점 — 반환 문자열과 시그니처를 그대로 유지한다.
    """
    draft, meta, error = await prepare_channel_report(
        channels=channels, report_channel=report_channel,
        interval_hours=interval_hours, instruction=instruction, **_kw,
    )
    if error:
        return error
    return render_channel_report_registered(commit_job(draft), meta)


# ── 표시 ────────────────────────────────────────────────────────────────
def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def render_job(j: dict) -> str:
    if j.get("kind") == "channel_report":
        sources = ", ".join(f"#{item.get('name')}" for item in j.get("source_channels", []))
        return (
            f"[{j.get('id')}] {j.get('interval_hours', 1)}시간마다 · 다음 실행 "
            f"{_fmt_dt(j.get('next_run', ''))} · {sources} → #{j.get('channel_name')} · 채널 대화 보고"
        )
    rep = "매일" if j.get("repeat") == "daily" else "1회"
    kind = "브리핑(그 시각에 내용 생성)" if j.get("kind") == "briefing" else "메시지"
    return (
        f"[{j.get('id')}] {rep} · 다음 발화 {_fmt_dt(j.get('next_run', ''))} → #{j.get('channel_name')}"
        f" · {kind}: {str(j.get('text', ''))[:80]}"
    )


def render_jobs() -> str:
    if not _JOBS:
        return "등록된 예약이 없습니다."
    return "등록된 예약 " + str(len(_JOBS)) + "건:\n" + "\n".join(render_job(j) for j in _JOBS)


def render_add_preview(j: dict) -> str:
    rep = "매일 반복" if j.get("repeat") == "daily" else "1회"
    kind = "브리핑 — 그 시각에 웹 조사로 내용을 생성해 전송" if j.get("kind") == "briefing" else "고정 메시지 전송"
    # 내용을 자르지 않는다 — 소유자가 실제 발신될 전체 내용을 보고 승인해야 한다(_ask_owner_approval이 분할 전송).
    return (
        f"예약 등록 요청 — {rep}\n"
        f"첫 발화: {_fmt_dt(j.get('next_run', ''))} → #{j.get('channel_name')}\n"
        f"종류: {kind}\n"
        f"내용: {str(j.get('text', ''))}"
    )


# ── 툴 스키마 + 에이전트 탭 핸들러(ASYNC_PLAIN, discordbot 지연 import) ──
SCHEDULE_ADD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discord_schedule_add",
        "description": (
            "디스코드 채널로 예약 전송을 등록한다. kind=message는 고정 문구를 그 시각에 보내고, "
            "kind=briefing은 그 시각에 웹 조사로 내용을 생성해 보낸다(아침 뉴스·날씨 브리핑 등). "
            "실행 권한은 선택한 권한 모드를 따르며, 발화는 앱이 켜져 있는 동안만 일어난다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "보낼 텍스트 채널 이름 또는 ID"},
                "text": {"type": "string", "description": "message: 보낼 문구 / briefing: 무엇을 브리핑할지 지시"},
                "when": {"type": "string", "description": "'HH:MM'(예: 22:55) 또는 'YYYY-MM-DD HH:MM'"},
                "repeat": {"type": "string", "enum": ["once", "daily"], "description": "once=1회, daily=매일"},
                "kind": {"type": "string", "enum": ["message", "briefing"], "description": "message=고정 문구, briefing=그 시각에 내용 생성"},
            },
            "required": ["channel", "text", "when"],
        },
    },
}

SCHEDULE_LIST_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discord_schedule_list",
        "description": "등록된 디스코드 예약 목록을 조회한다.",
        "parameters": {"type": "object", "properties": {}},
    },
}

SCHEDULE_REMOVE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discord_schedule_remove",
        "description": "등록된 디스코드 예약을 ID로 삭제한다. ID는 discord_schedule_list로 확인한다.",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "삭제할 예약 ID"}},
            "required": ["id"],
        },
    },
}

CHANNEL_REPORT_ADD_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discord_channel_report_add",
        "description": (
            "지정한 디스코드 텍스트 채널들의 새 대화만 시간 단위로 수집·요약하여 보고 채널에 전송하는 "
            "반복 예약을 등록한다. 채널별 마지막 수집 메시지 ID를 저장하므로 이전에 성공적으로 보고한 "
            "대화는 다음 보고서에 다시 포함하지 않는다. 최초 보고는 등록 이후의 메시지부터 시작한다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_REPORT_CHANNELS,
                    "description": "수집할 텍스트 채널 이름 또는 ID 목록",
                },
                "report_channel": {
                    "type": "string",
                    "description": "완성된 보고서를 보낼 텍스트 채널 이름 또는 ID",
                },
                "interval_hours": {
                    "type": "integer",
                    "minimum": MIN_REPORT_INTERVAL_HOURS,
                    "maximum": MAX_REPORT_INTERVAL_HOURS,
                    "description": "보고 주기(시간 단위). 예: 1=매시간, 6=6시간마다",
                },
                "instruction": {
                    "type": "string",
                    "description": "선택 사항. 보고서가 특히 추적할 주제나 형식",
                    "maxLength": MAX_REPORT_INSTRUCTION,
                },
            },
            "required": ["channels", "report_channel", "interval_hours"],
        },
    },
}


async def schedule_add(channel=None, text=None, when=None, repeat=None, kind=None, **_kw) -> str:
    import discordops  # noqa: PLC0415 — 채널 해석은 discordops가 단일 출처(지연 import)

    a = canonical_add_args({"channel": channel, "text": text, "when": when, "repeat": repeat, "kind": kind, **_kw})
    got, err = discordops.resolve_text_channel(a["channel"])
    if err:
        return f"[거부] {err}"
    ch_id, ch_name = got
    job, jerr = add_job(
        channel_id=ch_id, channel_name=ch_name, kind=a["kind"], text=a["text"],
        when=a["when"], repeat=a["repeat"],
    )
    if jerr:
        return f"[거부] {jerr}"
    return "예약이 등록되었습니다.\n" + render_job(job)


async def schedule_list(**_kw) -> str:
    return render_jobs()


async def schedule_remove(id=None, **_kw) -> str:  # noqa: A002 — 스키마 필드명이 id
    jid = str(id or _kw.get("job_id") or _kw.get("target") or "").strip()
    if not jid:
        return "[거부] 삭제할 예약 ID가 없습니다 — discord_schedule_list로 ID를 확인하세요"
    if remove(jid):
        return f"예약 {jid}을(를) 삭제했습니다."
    return f"[거부] 예약 {jid}을(를) 찾을 수 없습니다 — discord_schedule_list로 ID를 확인하세요"
