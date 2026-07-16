# -*- coding: utf-8 -*-
"""디스코드 예약 코어 — 등록·영속·발화 시각 계산. discord.py 미의존(순수 파이썬).

- 예약은 '등록 시점'에 승인을 받는다(에이전트 탭=승인 다이얼로그, 디스코드=소유자 버튼).
  발화 시각에는 사람이 없을 수 있으므로 발화 자체는 승인 없이 실행된다.
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


# ── 표시 ────────────────────────────────────────────────────────────────
def _fmt_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def render_job(j: dict) -> str:
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
            "등록에는 사용자 승인이 필요하다. 발화는 앱이 켜져 있는 동안만 일어난다."
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
