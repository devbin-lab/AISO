"""Evidence-first document ToDo analysis with central Aiso persistence.

The initial extraction is deterministic on purpose.  Smaller local models
often turn a document summary into unstable JSON; here a candidate cannot be
created unless it retains an exact source quote and a page/slide location.
An LLM can later refine a *selected* candidate, but it never becomes the sole
source of truth for what the document said.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from extract import ExtractError, extract_document_segments
from tools import ToolError, _resolve, validate_workspace


MAX_DOCUMENTS = 12
MAX_CANDIDATES = 80
_LEGACY_STORE_RELATIVE_PATH = Path(".aiso") / "document-todos.json"
_DATABASE_ENV = "AISO_DOCUMENT_TODO_DB_PATH"
_REGISTRY_ENV = "AISO_DOCUMENT_TODO_REGISTRY_PATH"
_BOOTSTRAP_WORKSPACES_ENV = "AISO_DOCUMENT_TODO_BOOTSTRAP_WORKSPACES"
_AISO_CALENDAR_WORKSPACE = "Aiso Calendar"
_SUPPORTED_EXTENSIONS = frozenset({".pdf", ".pptx", ".pptm", ".docx", ".xlsx", ".xlsm", ".hwp", ".hwpx", ".txt", ".md", ".csv"})
_DATABASE_PATH_OVERRIDE: ContextVar[Path | None] = ContextVar(
    "aiso_document_todo_database_path_override", default=None
)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIN_ESTIMATED_MINUTES = 5
_MAX_ESTIMATED_MINUTES = 24 * 60
_REPLAN_DAILY_CAPACITY_MINUTES = 120
_REPLAN_BLOCK_MINUTES = 60
_REPLAN_MAX_WORKDAYS = 14
_TASK_WORDS = (
    "구현", "구축", "제작", "개발", "설계", "작성", "정리", "확정", "준비", "제출",
    "연결", "분석", "테스트", "검증", "보완", "기획", "완성", "조사", "제작",
    "implement", "build", "design", "create", "write", "test", "prepare", "submit",
)
_NOISE_ONLY = re.compile(r"^(?:목차|개요|참고|내용|설명|페이지|slide|슬라이드)\s*$", re.IGNORECASE)
_DATE_PATTERN = re.compile(r"(?P<year>20\d{2})[.년/-]\s*(?P<month>\d{1,2})[.월/-]\s*(?P<day>\d{1,2})")
_COLON_TIME_PATTERN = re.compile(
    r"(?<!\d)(?P<hour>[01]?\d|2[0-3])[ \t]*:[ \t]*(?P<minute>[0-5]\d)(?!\d)",
    re.IGNORECASE,
)
_KOREAN_TIME_PATTERN = re.compile(
    r"(?<!\d)(?P<meridiem>오전|오후|am|pm)?[ \t]*(?P<hour>[01]?\d|2[0-3])[ \t]*시(?:[ \t]*(?P<minute>[0-5]\d)[ \t]*분?)?(?![가-힣])",
    re.IGNORECASE,
)
_CALENDAR_KOREAN_TIME_PATTERN = re.compile(
    r"(?<!\d)(?P<meridiem>오전|오후|am|pm)?[ \t]*(?P<hour>[01]?\d|2[0-3])[ \t]*시(?:[ \t]*(?P<minute>[0-5]\d)[ \t]*분?)?",
    re.IGNORECASE,
)
_CALENDAR_TIME_TOKEN_PATTERN = re.compile(
    r"(?<!\d)(?P<meridiem>오전|오후|am|pm)?[ \t]*"
    r"(?P<hour>[01]?\d|2[0-3])(?:"
    r"[ \t]*:[ \t]*(?P<colon_minute>[0-5]\d)"
    r"|[ \t]*시(?:[ \t]*(?P<korean_minute>[0-5]\d)[ \t]*분?)?"
    r")(?!\d)",
    re.IGNORECASE,
)
_PRESENTATION_LOCATION = re.compile(r"^슬라이드 \d+$")
_PRESENTATION_SKIP_TOKENS = (
    "contents", "목차", "summary", "감사합니다", "game design document", "portfolio",
)


# Calendar events are intentionally parsed in the persistence layer rather
# than delegated to a model.  A small local model only needs to forward the
# user's original sentence to ``create_todo_event``; date/time/recurrence
# interpretation then stays deterministic and auditable.
CREATE_TODO_EVENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_calendar_event",
        "description": (
            "Aiso 캘린더에 개인 일정을 등록한다. Discord 메시지 예약이 아니다. "
            "사용자가 말한 일정 문장을 instruction에 그대로 전달하면 매일·매주·매월·매년 반복, "
            "날짜와 시작·종료 시각을 해석해 Aiso 중앙 캘린더 저장소에 저장한다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "사용자가 작성한 전체 일정 등록 요청 원문.",
                    "minLength": 2,
                    "maxLength": 2000,
                }
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
    },
}

MANAGE_TODO_SCHEMA = {
    "type": "function",
    "function": {
        "name": "manage_calendar_event",
        "description": (
            "Modify, complete, reopen, or delete calendar events already stored in Aiso. "
            "A single-event change requires an exact ID or unambiguous current title. "
            "Delete every stored event only when the user's verbatim instruction explicitly says "
            "to delete all registered/saved calendar events; otherwise it fails without mutation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "instruction": {
                    "type": "string",
                    "description": "The user's complete calendar management request, verbatim.",
                },
                "todo_id": {
                    "type": "string",
                    "description": "Optional exact calendar event ID obtained from list_calendar_events.",
                },
                "target_title": {
                    "type": "string",
                    "description": "Optional current calendar event title used to resolve the target safely.",
                },
                "action": {
                    "type": "string",
                    "enum": ["update", "complete", "reopen", "delete", "delete_all"],
                    "description": "Requested operation. Omit only when it is explicit in instruction.",
                },
                "new_title": {"type": "string", "description": "Replacement title for update."},
                "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                "start_date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
                "end_date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
                "start_time": {"type": "string", "description": "24-hour time HH:MM."},
                "end_time": {"type": "string", "description": "24-hour time HH:MM."},
                "estimated_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
                "recurrence": {
                    "description": "Optional replacement recurrence; null removes recurrence.",
                    "oneOf": [
                        {"type": "null"},
                        {
                            "type": "object",
                            "properties": {
                                "frequency": {"type": "string", "enum": ["daily", "weekly", "monthly", "yearly"]},
                                "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}},
                                "day": {"type": "integer", "minimum": 1, "maximum": 31},
                                "month": {"type": "integer", "minimum": 1, "maximum": 12},
                            },
                            "required": ["frequency"],
                            "additionalProperties": False,
                        },
                    ],
                },
            },
            "required": ["instruction"],
            "additionalProperties": False,
        },
    },
}

_CALENDAR_DATE_PATTERN = re.compile(
    r"(?:(?P<year>20\d{2})\s*년\s*)?(?P<month>\d{1,2})\s*월\s*(?P<day>\d{1,2})\s*일"
)
_CALENDAR_ISO_DATE_PATTERN = re.compile(r"(?<!\d)(?P<year>20\d{2})-(?P<month>\d{1,2})-(?P<day>\d{1,2})(?!\d)")
_CALENDAR_DAY_ONLY_PATTERN = re.compile(r"(?<!\d)(?P<day>[12]?\d|3[01])일(?![가-힣])")
_CALENDAR_WEEKDAYS: dict[str, int] = {
    "일요일": 0, "일요": 0, "일": 0, "sunday": 0, "sun": 0,
    "월요일": 1, "월요": 1, "월": 1, "monday": 1, "mon": 1,
    "화요일": 2, "화요": 2, "화": 2, "tuesday": 2, "tue": 2, "tues": 2,
    "수요일": 3, "수요": 3, "수": 3, "wednesday": 3, "wed": 3,
    "목요일": 4, "목요": 4, "목": 4, "thursday": 4, "thu": 4, "thur": 4, "thurs": 4,
    "금요일": 5, "금요": 5, "금": 5, "friday": 5, "fri": 5,
    "토요일": 6, "토요": 6, "토": 6, "saturday": 6, "sat": 6,
}


def _calendar_weekdays_from_instruction(text: str) -> list[int]:
    """Extract explicit weekly days from natural Korean and English phrases.

    Korean weekday names are commonly followed by particles, for example
    ``일요일마다`` or ``월요일에는``.  The previous word-boundary-only matcher
    treated those as non-weekday text and silently anchored a weekly event to
    *today*.  Keep the short weekday forms strict enough not to mistake a
    numbered date such as ``15일`` for Sunday.
    """
    folded = text.casefold()
    suffix = r"(?=$|[\s,./!?()\[\]{}]|(?:마다|에|에는|은|는|도|만|부터|까지))"
    matches: set[int] = set()

    korean_long_forms = (
        ("일요일", 0), ("일요", 0),
        ("월요일", 1), ("월요", 1),
        ("화요일", 2), ("화요", 2),
        ("수요일", 3), ("수요", 3),
        ("목요일", 4), ("목요", 4),
        ("금요일", 5), ("금요", 5),
        ("토요일", 6), ("토요", 6),
    )
    for token, weekday in korean_long_forms:
        if re.search(rf"(?<![0-9a-z가-힣]){re.escape(token)}{suffix}", folded):
            matches.add(weekday)

    korean_short_forms = (("일", 0), ("월", 1), ("화", 2), ("수", 3), ("목", 4), ("금", 5), ("토", 6))
    for token, weekday in korean_short_forms:
        if re.search(rf"(?<![0-9a-z가-힣]){token}{suffix}", folded):
            matches.add(weekday)

    english_forms = (
        (("sunday", "sun"), 0), (("monday", "mon"), 1), (("tuesday", "tue", "tues"), 2),
        (("wednesday", "wed"), 3), (("thursday", "thu", "thur", "thurs"), 4),
        (("friday", "fri"), 5), (("saturday", "sat"), 6),
    )
    for tokens, weekday in english_forms:
        if any(re.search(rf"\b{token}\b", folded) for token in tokens):
            matches.add(weekday)
    return sorted(matches)


@dataclass(frozen=True)
class _PresentationWorkPackage:
    """A concrete implementation package detected from a design slide.

    This deliberately uses several domain facts from the same slide rather
    than one action word.  A small model then receives already-scoped work
    packages and cannot turn section headers or explanatory prose into tasks.
    """

    title: str
    priority: str
    markers: tuple[str, ...]


_PRESENTATION_WORK_PACKAGES = (
    _PresentationWorkPackage("일반전·등급전·VS CPU·친구 대국 모드 구현", "high", ("일반전", "등급전", "VS CPU", "친구 대국")),
    _PresentationWorkPackage("작위·레이팅 승급/강등과 상위 후보 풀 구현", "high", ("공작 자동 확정", "강등 보호", "마왕 후보 풀")),
    _PresentationWorkPackage("시즌 리셋·마왕 기록·명예의 전당 운영 구현", "medium", ("소프트 리셋", "명예의 전당")),
    _PresentationWorkPackage("마왕 헌액 제안·심의·제작 파이프라인 구현", "high", ("권리 양도 동의", "심의", "다음 시즌")),
    _PresentationWorkPackage("캐릭터 선택·개인 스토리·체스 가이드 메뉴 구현", "medium", ("보유 캐릭터 선택", "개인 스토리 열람", "체스 가이드")),
    _PresentationWorkPackage("직접 구매형 캐릭터 상점과 구매 전 미리보기 구현", "medium", ("직접 구매", "구매 전 미리보기", "중복 없음")),
    _PresentationWorkPackage("대국 이모티콘 쿨타임·뮤트·신고 방어 기능 구현", "medium", ("쿨타임", "뮤트 옵션", "어뷰징 방어")),
    _PresentationWorkPackage("체크·체크메이트 중심의 대국 연출과 스킵 제어 구현", "medium", ("체크", "체크메이트", "스킵 가능")),
    _PresentationWorkPackage("레이팅 기반 매칭·재접속·이탈·서버 장애 처리 구현", "high", ("레이팅 근접 우선", "연결 끊김 처리", "서버측 장애")),
    _PresentationWorkPackage("엔진 컨닝 탐지·신고 검토·단계별 제재 시스템 구현", "high", ("수 일치율 분석", "신고 + 검토 큐", "수동 검토")),
    _PresentationWorkPackage("월간·격주·주간/일일 라이브 운영 보상 루프 구현", "medium", ("월간", "격주", "주간/일일")),
    _PresentationWorkPackage("변형 체스 이벤트 규칙과 한정 보상 운영 구현", "medium", ("Chess960", "Crazyhouse", "한정 보상")),
    _PresentationWorkPackage("작위 상승 보상·승급 컷인·칭호/프레임 연출 구현", "low", ("승급 컷인", "전용 칭호", "성장 보상")),
)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _normalise_title(value: str) -> str:
    value = re.sub(r"^\s*(?:[#>*-]+|[0-9]+[.)]|[가-힣]+[.)])\s*", "", value).strip()
    value = re.sub(r"\s+", " ", value)
    value = value.strip("-–—:：| ")
    if len(value) > 110:
        value = value[:107].rstrip() + "…"
    return value


def _line_candidates(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = _normalise_title(raw)
        if not line or _NOISE_ONLY.match(line):
            continue
        # A short heading/bullet with an actual action carries a much more
        # reliable task signal than free-form body prose.
        if (
            4 <= len(line) <= 110
            and not line.endswith(("원칙", "개요", "구조", "시스템", "모델", "기획서"))
            and not line.endswith(("다.", "니다.", "한다."))
            and any(word.casefold() in line.casefold() for word in _TASK_WORDS)
        ):
            lines.append(line)
    # Documents sometimes put several actions in one extracted paragraph.
    if not lines:
        for sentence in re.split(r"(?<=[.!?。])\s+", text):
            line = _normalise_title(sentence)
            if 8 <= len(line) <= 110 and not line.endswith(("다.", "니다.", "한다.")) and any(word.casefold() in line.casefold() for word in _TASK_WORDS):
                lines.append(line)
    return lines


def _priority(text: str) -> str:
    folded = text.casefold()
    if any(token in folded for token in ("필수", "핵심", "최우선", "우선순위: 높", "우선순위 높", "high")):
        return "high"
    if any(token in folded for token in ("낮음", "low", "선택", "추후")):
        return "low"
    return "medium"


def _due_date(text: str) -> str | None:
    match = _DATE_PATTERN.search(text)
    if match is None:
        return None
    try:
        return f"{int(match.group('year')):04d}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"
    except ValueError:
        return None


def _due_time(text: str) -> str | None:
    match = _COLON_TIME_PATTERN.search(text)
    if match is not None:
        return f"{int(match.group('hour')):02d}:{int(match.group('minute')):02d}"
    match = _KOREAN_TIME_PATTERN.search(text)
    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").casefold()
    if meridiem in {"오후", "pm"} and hour < 12:
        hour += 12
    elif meridiem in {"오전", "am"} and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def _calendar_weekday(value: date) -> int:
    """Return Sunday-first weekday numbers used by the persisted API."""
    return (value.weekday() + 1) % 7


def _calendar_date_from_instruction(text: str, today: date) -> tuple[date | None, bool]:
    """Return an explicit date and whether it included a year."""
    # Relative dates are a normal way to enter one-off personal events.  They
    # must be resolved here, alongside absolute dates, so that the persistence
    # layer remains deterministic even when a small model merely forwards the
    # original instruction.
    folded = text.casefold()
    if "모레" in text or re.search(r"\bday after tomorrow\b", folded):
        return today + timedelta(days=2), False
    if "내일" in text or re.search(r"\btomorrow\b", folded):
        return today + timedelta(days=1), False
    if "오늘" in text or re.search(r"\btoday\b", folded):
        return today, False
    match = _CALENDAR_ISO_DATE_PATTERN.search(text) or _CALENDAR_DATE_PATTERN.search(text)
    if match is None:
        return None, False
    year_text = match.group("year")
    try:
        return date(int(year_text or today.year), int(match.group("month")), int(match.group("day"))), bool(year_text)
    except ValueError as error:
        raise ToolError("일정 날짜가 올바르지 않습니다.") from error


def _next_calendar_date(today: date, *, month: int, day: int) -> date:
    """Return this or next occurrence of a month/day without silently rolling dates."""
    try:
        candidate = date(today.year, month, day)
    except ValueError as error:
        raise ToolError("반복 일정의 월·일이 올바르지 않습니다.") from error
    if candidate < today:
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError as error:
            raise ToolError("반복 일정의 월·일이 올바르지 않습니다.") from error
    return candidate


def _calendar_recurrence(text: str, *, today: date, explicit_date: date | None) -> tuple[dict[str, Any] | None, date]:
    """Parse Aiso calendar recurrence into a compact, durable record.

    The result deliberately contains only recurrence data, not a rendered
    series.  The calendar expands occurrences at display time, preventing
    thousands of duplicate rows for a daily or yearly event.
    """
    folded = text.casefold()
    is_daily = any(marker in folded for marker in ("매일", "every day", "daily"))
    is_weekly = any(marker in folded for marker in ("매주", "every week", "weekly")) or bool(re.search(
        r"\bevery\s+(?:sunday|monday|tuesday|wednesday|thursday|friday|saturday|sun|mon|tue(?:s)?|wed|thu(?:r(?:s)?)?|fri|sat)\b",
        folded,
    ))
    is_monthly = any(marker in folded for marker in ("매월", "every month", "monthly"))
    is_yearly = any(marker in folded for marker in ("매년", "매해", "every year", "yearly", "annually", "annual"))
    frequency_count = sum((is_daily, is_weekly, is_monthly, is_yearly))
    if frequency_count > 1:
        raise ToolError("한 일정에는 매일·매주·매월·매년 중 하나의 반복만 지정하세요.")
    if not frequency_count:
        return None, explicit_date or today

    if is_daily:
        return {"frequency": "daily"}, explicit_date or today

    if is_weekly:
        weekdays = _calendar_weekdays_from_instruction(text)
        anchor = explicit_date or today
        if not weekdays:
            weekdays = [_calendar_weekday(anchor)]
        elif explicit_date is not None and _calendar_weekday(explicit_date) not in weekdays:
            expected = ", ".join(("일", "월", "화", "수", "목", "금", "토")[weekday] + "요일" for weekday in weekdays)
            actual = ("일", "월", "화", "수", "목", "금", "토")[_calendar_weekday(explicit_date)] + "요일"
            raise ToolError(
                f"반복 요일({expected})과 시작 날짜({explicit_date.isoformat()}, {actual})가 일치하지 않습니다. "
                "시작 날짜 또는 반복 요일을 확인하세요."
            )
        if explicit_date is None:
            offsets = [((weekday - _calendar_weekday(today)) % 7) for weekday in weekdays]
            anchor = today + timedelta(days=min(offsets))
        return {"frequency": "weekly", "weekdays": weekdays}, anchor

    explicit = explicit_date
    if is_monthly:
        day_match = _CALENDAR_DAY_ONLY_PATTERN.search(text)
        day = explicit.day if explicit else int(day_match.group("day")) if day_match else today.day
        if explicit is None:
            try:
                anchor = date(today.year, today.month, day)
            except ValueError as error:
                raise ToolError("매월 반복 일정의 일이 올바르지 않습니다.") from error
            if anchor < today:
                next_month = today.month + 1
                next_year = today.year + (1 if next_month == 13 else 0)
                next_month = 1 if next_month == 13 else next_month
                try:
                    anchor = date(next_year, next_month, day)
                except ValueError as error:
                    raise ToolError("매월 반복 일정의 일이 올바르지 않습니다.") from error
        else:
            anchor = explicit
        return {"frequency": "monthly", "day": day}, anchor

    # yearly: an omitted month/day anchors to today's anniversary, while a
    # Korean '매년 12월 25일' phrase remains exact and transparent.
    month = explicit.month if explicit else today.month
    day = explicit.day if explicit else today.day
    anchor = explicit or _next_calendar_date(today, month=month, day=day)
    if explicit is not None and anchor < today:
        anchor = _next_calendar_date(today, month=month, day=day)
    return {"frequency": "yearly", "month": month, "day": day}, anchor


def _calendar_time_range(text: str) -> tuple[str | None, str | None]:
    """Extract first and second clock tokens as a user-visible time range."""
    # Keep the source order and each token's own AM/PM marker.  Looking for
    # colon times first used to turn e.g. ``10:00 ~ 오후 8:30`` into an
    # impossible 10:00–08:30 range because the second token's ``오후`` was
    # outside the old colon-only match.
    matches = list(_CALENDAR_TIME_TOKEN_PATTERN.finditer(text))
    values: list[str] = []
    inherited_meridiem = ""
    if matches:
        inherited_meridiem = (matches[0].groupdict().get("meridiem") or "").casefold()
    for index, match in enumerate(matches[:2]):
        hour = int(match.group("hour"))
        minute = int(match.group("colon_minute") or match.group("korean_minute") or 0)
        meridiem = (match.groupdict().get("meridiem") or "").casefold()
        # Korean speakers commonly shorten ``오후 2시부터 4시까지``.  The
        # second token belongs to the same meridiem unless it explicitly says
        # otherwise; interpreting it as 04:00 makes an otherwise valid planner
        # edit fail its end-after-start contract.
        if index == 1 and not meridiem:
            meridiem = inherited_meridiem
        if meridiem in {"오후", "pm"} and hour < 12:
            hour += 12
        elif meridiem in {"오전", "am"} and hour == 12:
            hour = 0
        values.append(f"{hour:02d}:{minute:02d}")
    if not values:
        return None, None
    return values[0], values[1] if len(values) > 1 else None


def _calendar_title(instruction: str) -> str:
    """Keep the actual event name after removing schedule grammar only."""
    title = instruction.strip()
    title = _CALENDAR_ISO_DATE_PATTERN.sub(" ", title)
    title = _CALENDAR_DATE_PATTERN.sub(" ", title)
    title = _CALENDAR_DAY_ONLY_PATTERN.sub(" ", title)
    title = _COLON_TIME_PATTERN.sub(" ", title)
    title = _CALENDAR_KOREAN_TIME_PATTERN.sub(" ", title)
    title = re.sub(r"(?:오늘|내일|모레|today|tomorrow|day\s+after\s+tomorrow)", " ", title, flags=re.IGNORECASE)
    title = re.sub(
        r"(?:매일|매주|매월|매년|매해|every\s+day|every\s+week|every\s+month|every\s+year|daily|weekly|monthly|yearly|annually|annual)",
        " ", title, flags=re.IGNORECASE,
    )
    title = re.sub(
        r"(?:일요일|월요일|화요일|수요일|목요일|금요일|토요일|일요|월요|화요|수요|목요|금요|토요)(?:마다)?"
        r"|(?<![0-9a-z가-힣])(?:일|월|화|수|목|금|토)(?:마다)?(?=$|[\s,./!?])"
        r"|(?:sunday|monday|tuesday|wednesday|thursday|friday|saturday|sun|mon|tue(?:s)?|wed|thu(?:r(?:s)?)?|fri|sat)",
        " ", title, flags=re.IGNORECASE,
    )
    title = re.sub(r"(?:부터|까지|~|∼|–|—|-|오전|오후|am|pm)", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"(?:일정|스케줄|캘린더|to[ -]?do|calendar|schedule|등록(?:해주세요|해줘|해)?|추가(?:해주세요|해줘|해)?|잡아(?:주세요|줘)?|넣어(?:주세요|줘)?|만들어(?:주세요|줘)?)", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"(?:\badd\b|\bregister\b|\bcreate\b|\bput\b|\bat\b|\bon\b|\bfrom\b|\buntil\b|\bto\b|\bmy\b|\bthe\b|\ban?\b)", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"(?:을|를|에|으로|로|부터|까지)\s*$", "", title)
    title = re.sub(r"\s+", " ", title).strip(" .,:;!-–—")
    return _normalise_title(title)


def _calendar_priority(text: str) -> str:
    folded = text.casefold()
    if any(token in folded for token in ("p1", "높음", "high", "중요")):
        return "high"
    if any(token in folded for token in ("p3", "낮음", "low")):
        return "low"
    return "medium"


def _evidence_quote(lines: list[str], markers: tuple[str, ...]) -> str:
    matches = [line for line in lines if any(marker.casefold() in line.casefold() for marker in markers)]
    return "\n".join(matches[:3])


def _presentation_candidates(source_file: str, location: str, text: str) -> list[dict[str, Any]]:
    """Convert a design slide into feature-sized work packages, never headings."""
    lines = [_normalise_title(raw) for raw in text.splitlines()]
    lines = [line for line in lines if line]
    folded = "\n".join(lines).casefold()
    if not lines or any(token in folded for token in _PRESENTATION_SKIP_TOKENS):
        return []
    candidates: list[dict[str, Any]] = []
    for package in _PRESENTATION_WORK_PACKAGES:
        if not all(marker.casefold() in folded for marker in package.markers):
            continue
        quote = _evidence_quote(lines, package.markers)
        if not quote:
            continue
        candidates.append({
            "id": _candidate_id(source_file, location, quote, package.title),
            "title": package.title,
            "priority": package.priority,
            "dueDate": None,
            "dueTime": None,
            "status": "open",
            "evidence": [{"file": source_file, "location": location, "quote": quote}],
        })
    return candidates


def _candidate_id(source_file: str, location: str, quote: str, title: str) -> str:
    material = "\u241f".join((source_file, location, quote, title)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:20]


def analyze_documents(workspace: str, paths: list[str]) -> dict[str, Any]:
    """Return only source-backed candidates; never mutate the workspace."""
    root = validate_workspace(workspace)
    if not paths:
        raise ToolError("분석할 문서를 하나 이상 선택하세요.")
    if len(paths) > MAX_DOCUMENTS:
        raise ToolError(f"한 번에 최대 {MAX_DOCUMENTS}개 문서만 분석할 수 있습니다.")

    documents: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for requested_path in paths:
        if not isinstance(requested_path, str) or not requested_path.strip():
            raise ToolError("문서 경로는 작업 폴더 기준 상대 경로여야 합니다.")
        target = _resolve(root, requested_path)
        if not target.is_file():
            raise ToolError(f"문서 파일이 없습니다: {requested_path}")
        source_file = _relative(root, target)
        try:
            segments = extract_document_segments(target)
        except ExtractError as error:
            raise ToolError(f"{source_file}: {error}") from error
        documents.append({"path": source_file, "segments": len(segments)})
        presentation = target.suffix.lower() in {".pptx", ".pptm"}
        for segment in segments:
            if presentation and _PRESENTATION_LOCATION.match(segment.location):
                segment_candidates = _presentation_candidates(source_file, segment.location, segment.text)
            else:
                segment_candidates = []
                for title in _line_candidates(segment.text):
                    segment_candidates.append({
                        "id": _candidate_id(source_file, segment.location, title, title),
                        "title": title,
                        "priority": _priority(f"{title}\n{segment.text}"),
                        # A deadline must be explicitly attached to the task itself.  Scanning a
                        # whole slide mistakes labels such as "6개월/시즌" for a clock time.
                        "dueDate": _due_date(title),
                        "dueTime": _due_time(title),
                        "status": "open",
                        "evidence": [{"file": source_file, "location": segment.location, "quote": title}],
                    })
            for candidate in segment_candidates:
                title = candidate["title"]
                key = re.sub(r"[^0-9a-z가-힣]+", "", title.casefold())
                if len(key) < 3 or key in seen_titles:
                    continue
                seen_titles.add(key)
                candidates.append(candidate)
                if len(candidates) >= MAX_CANDIDATES:
                    break
            if len(candidates) >= MAX_CANDIDATES:
                break
        if len(candidates) >= MAX_CANDIDATES:
            break

    return {
        "documents": documents,
        "candidates": candidates,
        "notice": (
            "기획 문서는 기능 단위 작업 패키지로, 일정 문서는 명시된 실행 항목으로 추렸습니다. "
            "제목·목차·설명문만으로는 일정을 만들지 않았습니다."
        ),
    }


def list_documents(workspace: str) -> list[dict[str, Any]]:
    """List selectable documents without exposing files outside the workspace."""
    root = validate_workspace(workspace)
    found: list[dict[str, Any]] = []
    try:
        entries = sorted(root.rglob("*"), key=lambda path: path.as_posix().casefold())
    except OSError as error:
        raise ToolError(f"작업 폴더의 문서 목록을 읽을 수 없습니다: {error}") from error
    for entry in entries:
        if len(found) >= 200:
            break
        if ".aiso" in entry.parts or entry.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            continue
        try:
            relative = _relative(root, entry)
            safe_entry = _resolve(root, relative)
            if not safe_entry.is_file():
                continue
            found.append({"path": relative, "extension": entry.suffix.lower(), "size": entry.stat().st_size})
        except (OSError, ToolError, ValueError):
            continue
    return found


def _legacy_store_path(root: Path) -> Path:
    return root / _LEGACY_STORE_RELATIVE_PATH


def _database_path() -> Path:
    """Return Aiso's private ToDo database, never a workspace-relative path."""
    override = _DATABASE_PATH_OVERRIDE.get()
    if override is not None:
        return override
    configured = os.environ.get(_DATABASE_ENV, "").strip()
    if configured:
        try:
            return Path(configured).expanduser().resolve()
        except OSError as error:
            raise ToolError("Aiso 캘린더 데이터베이스 경로를 준비할 수 없습니다.") from error
    # Electron always supplies the private userData path.  This fallback keeps
    # direct FastAPI/test use central as well, without creating project files.
    return Path.home() / ".aiso" / "document-todos.sqlite3"


@contextmanager
def temporary_todo_database(path: str | Path):
    """Route one isolated maintenance or QA operation to its own SQLite file.

    The normal public persistence API remains central. This context-local
    override lets maintenance checks prove that API without creating fixture
    tasks in the user's real ToDo list.
    """
    target = Path(path).expanduser().resolve()
    token = _DATABASE_PATH_OVERRIDE.set(target)
    try:
        yield target
    finally:
        _DATABASE_PATH_OVERRIDE.reset(token)


def _open_database() -> sqlite3.Connection:
    path = _database_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS document_todos (
              id TEXT PRIMARY KEY,
              source_workspace TEXT NOT NULL,
              source_candidate_id TEXT NOT NULL,
              title TEXT NOT NULL,
              priority TEXT NOT NULL,
              due_date TEXT,
              due_time TEXT,
              end_time TEXT,
              start_date TEXT,
              end_date TEXT,
              estimated_minutes INTEGER,
              recurrence_json TEXT,
              status TEXT NOT NULL,
              evidence_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_document_todos_schedule
              ON document_todos(due_date, due_time, created_at, title);
            CREATE INDEX IF NOT EXISTS idx_document_todos_workspace
              ON document_todos(source_workspace, source_candidate_id);
            CREATE TABLE IF NOT EXISTS document_todo_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS document_todo_schedule_blocks (
              id TEXT PRIMARY KEY,
              todo_id TEXT NOT NULL,
              scheduled_date TEXT NOT NULL,
              minutes INTEGER NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(todo_id) REFERENCES document_todos(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_document_todo_schedule_blocks_date
              ON document_todo_schedule_blocks(scheduled_date, todo_id);
            """
        )
        # Existing Aiso installations already have a central table.  SQLite's
        # CREATE TABLE IF NOT EXISTS does not add columns, so keep this small
        # forward-only migration next to the schema rather than replacing data.
        existing_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(document_todos)")
        }
        for name, definition in (
            ("start_date", "TEXT"),
            ("end_date", "TEXT"),
            ("estimated_minutes", "INTEGER"),
            ("end_time", "TEXT"),
            ("recurrence_json", "TEXT"),
        ):
            if name not in existing_columns:
                connection.execute(f"ALTER TABLE document_todos ADD COLUMN {name} {definition}")
        return connection
    except sqlite3.Error as error:
        raise ToolError("Aiso 캘린더 데이터베이스를 열 수 없습니다.") from error


def _registry_path() -> Path | None:
    """Return Aiso's private workspace index, never a caller-provided path."""
    raw = os.environ.get(_REGISTRY_ENV, "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def _read_registry() -> dict[str, Any]:
    path = _registry_path()
    if path is None or not path.is_file():
        return {"version": 1, "workspaces": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A registry is an index only. A damaged index must not make the source
        # ToDo stores unreadable or prevent a new document analysis from saving.
        return {"version": 1, "workspaces": []}
    if not isinstance(data, dict) or not isinstance(data.get("workspaces"), list):
        return {"version": 1, "workspaces": []}
    return data


def _bootstrap_workspace_paths() -> list[str]:
    raw = os.environ.get(_BOOTSTRAP_WORKSPACES_ENV, "")
    try:
        values = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        return []
    return [value for value in values if isinstance(value, str) and value.strip()]


def _known_todo_roots() -> list[Path]:
    """Collect only roots previously used by Aiso, never search arbitrary disks."""
    entries = [*_read_registry().get("workspaces", []), *_bootstrap_workspace_paths()]
    roots: list[Path] = []
    seen: set[str] = set()
    for entry in entries:
        raw = entry.get("path") if isinstance(entry, dict) else entry
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            root = validate_workspace(raw)
        except ToolError:
            continue
        if not _legacy_store_path(root).is_file():
            continue
        key = str(root).casefold()
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def _read_legacy_store(root: Path) -> dict[str, Any]:
    path = _legacy_store_path(root)
    if not path.is_file():
        return {"version": 1, "items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ToolError("기존 문서 일정 저장소를 읽을 수 없습니다. 파일을 직접 덮어쓰지 말고 백업 후 다시 시도하세요.") from error
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ToolError("기존 문서 일정 저장소 형식이 올바르지 않습니다.")
    return data


def _todo_id(root: Path, source_candidate_id: str) -> str:
    source = f"{str(root).casefold()}\0{source_candidate_id}".encode("utf-8")
    return hashlib.sha256(source).hexdigest()[:32]


def _normalise_schedule_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise ToolError("일정 날짜는 YYYY-MM-DD 형식이어야 합니다.")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise ToolError("올바른 일정 날짜를 입력하세요.") from error
    return value


def _normalise_estimated_minutes(value: Any) -> int:
    if value is None or value == "":
        return 30
    if isinstance(value, bool):
        raise ToolError("예상 소요시간은 분 단위 숫자여야 합니다.")
    try:
        minutes = int(value)
    except (TypeError, ValueError) as error:
        raise ToolError("예상 소요시간은 분 단위 숫자여야 합니다.") from error
    if not _MIN_ESTIMATED_MINUTES <= minutes <= _MAX_ESTIMATED_MINUTES:
        raise ToolError(f"예상 소요시간은 {_MIN_ESTIMATED_MINUTES}~{_MAX_ESTIMATED_MINUTES}분으로 지정하세요.")
    return minutes


def _normalise_clock_time(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ToolError("일정 시각은 HH:MM 형식이어야 합니다.")
    return value


def _normalise_recurrence(value: Any) -> dict[str, Any] | None:
    """Validate persisted calendar recurrence without inventing missing rules."""
    if value is None or value == "":
        return None
    if not isinstance(value, dict):
        raise ToolError("반복 일정 형식이 올바르지 않습니다.")
    frequency = value.get("frequency")
    if frequency not in {"daily", "weekly", "monthly", "yearly"}:
        raise ToolError("지원하지 않는 반복 일정입니다.")
    if frequency == "daily":
        return {"frequency": "daily"}
    if frequency == "weekly":
        weekdays = value.get("weekdays")
        if not isinstance(weekdays, list) or not weekdays or any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in weekdays):
            raise ToolError("매주 반복 일정에는 요일을 하나 이상 지정하세요.")
        return {"frequency": "weekly", "weekdays": sorted(set(weekdays))}
    if frequency == "monthly":
        day = value.get("day")
        if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
            raise ToolError("매월 반복 일정의 일이 올바르지 않습니다.")
        return {"frequency": "monthly", "day": day}
    month = value.get("month")
    day = value.get("day")
    if (
        isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12
        or isinstance(day, bool) or not isinstance(day, int)
    ):
        raise ToolError("매년 반복 일정의 월·일이 올바르지 않습니다.")
    try:
        date(2024, month, day)
    except ValueError as error:
        raise ToolError("매년 반복 일정의 월·일이 올바르지 않습니다.") from error
    return {"frequency": "yearly", "month": month, "day": day}


def _normalise_todo_record(
    root: Path,
    raw: dict[str, Any],
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    title = _normalise_title(str(raw.get("title") or ""))
    evidence = raw.get("evidence")
    if not title or not isinstance(evidence, list) or not evidence:
        raise ToolError("일정에는 제목과 최소 한 개의 원문 근거가 필요합니다.")
    checked_evidence: list[dict[str, str]] = []
    for source in evidence:
        if not isinstance(source, dict):
            raise ToolError("원문 근거 형식이 올바르지 않습니다.")
        file = str(source.get("file") or "").strip()
        location = str(source.get("location") or "").strip()
        quote = str(source.get("quote") or "").strip()
        if not file or not location or not quote:
            raise ToolError("원문 근거에는 파일·위치·인용문이 모두 필요합니다.")
        checked_evidence.append({"file": file, "location": location, "quote": quote[:1000]})
    source_candidate_id = str(
        raw.get("sourceCandidateId") or raw.get("id") or
        _candidate_id(checked_evidence[0]["file"], checked_evidence[0]["location"], checked_evidence[0]["quote"], title)
    )
    now = datetime.now(timezone.utc).isoformat()
    due_date = _normalise_schedule_date(raw.get("dueDate"))
    start_date = _normalise_schedule_date(raw.get("startDate")) or due_date
    end_date = _normalise_schedule_date(raw.get("endDate")) or due_date
    if start_date and not end_date:
        end_date = start_date
    if end_date and not start_date:
        start_date = end_date
    if start_date and end_date and start_date > end_date:
        raise ToolError("작업 시작일은 종료일보다 늦을 수 없습니다.")
    return {
        "id": _todo_id(root, source_candidate_id),
        "sourceCandidateId": source_candidate_id,
        "workspace": str(root),
        "title": title,
        "priority": raw.get("priority") if raw.get("priority") in {"high", "medium", "low"} else "medium",
        # dueDate remains the backwards-compatible deadline field.  For a
        # ranged task it is always the final day of that range.
        "dueDate": end_date,
        "dueTime": _normalise_clock_time(raw.get("dueTime")),
        "endTime": _normalise_clock_time(raw.get("endTime")),
        "startDate": start_date,
        "endDate": end_date,
        "estimatedMinutes": _normalise_estimated_minutes(raw.get("estimatedMinutes")),
        "recurrence": _normalise_recurrence(raw.get("recurrence")),
        "status": raw.get("status") if raw.get("status") in {"open", "done"} else "open",
        "evidence": checked_evidence,
        "createdAt": created_at or now,
        "updatedAt": now,
    }


def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    try:
        evidence = json.loads(str(row["evidence_json"]))
    except json.JSONDecodeError as error:
        raise ToolError("Aiso 캘린더 데이터가 손상되었습니다.") from error
    if not isinstance(evidence, list):
        raise ToolError("Aiso 캘린더 데이터 형식이 올바르지 않습니다.")
    keys = set(row.keys())
    due_date = row["due_date"]
    start_date = row["start_date"] if "start_date" in keys else None
    end_date = row["end_date"] if "end_date" in keys else None
    recurrence: dict[str, Any] | None = None
    if "recurrence_json" in keys and row["recurrence_json"]:
        try:
            recurrence = _normalise_recurrence(json.loads(str(row["recurrence_json"])))
        except (json.JSONDecodeError, ToolError) as error:
            raise ToolError("Aiso 반복 일정 데이터가 손상되었습니다.") from error
    return {
        "id": str(row["id"]),
        "workspace": str(row["source_workspace"]),
        "title": str(row["title"]),
        "priority": str(row["priority"]),
        "dueDate": end_date or due_date,
        "dueTime": row["due_time"],
        "endTime": row["end_time"] if "end_time" in keys else None,
        "startDate": start_date or end_date or due_date,
        "endDate": end_date or due_date,
        "estimatedMinutes": int(row["estimated_minutes"]) if "estimated_minutes" in keys and row["estimated_minutes"] is not None else 30,
        "recurrence": recurrence,
        "status": str(row["status"]),
        "evidence": evidence,
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
    }


def _schedule_blocks_for_items(connection: sqlite3.Connection, item_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Load concrete work allocations without turning a ranged task into copies.

    A task may have a broad start/end range, or an approved recovery plan that
    explicitly splits its estimated work across days.  The latter is kept in a
    child table so the calendar can show e.g. 1 hour on Wednesday and 1 hour on
    Thursday without changing the task's source evidence or identity.
    """
    if not item_ids:
        return {}
    placeholders = ", ".join("?" for _ in item_ids)
    rows = connection.execute(
        f"""
        SELECT todo_id, scheduled_date, minutes
        FROM document_todo_schedule_blocks
        WHERE todo_id IN ({placeholders})
        ORDER BY scheduled_date, todo_id, minutes
        """,
        item_ids,
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {item_id: [] for item_id in item_ids}
    for row in rows:
        grouped[str(row["todo_id"])].append({
            "date": str(row["scheduled_date"]),
            "minutes": int(row["minutes"]),
        })
    return grouped


def _priority_rank(value: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(value, 1)


def _write_todos(
    connection: sqlite3.Connection,
    root: Path,
    items: list[dict[str, Any]],
    *,
    replace_sources: set[str] | None = None,
) -> None:
    workspace = str(root)
    if replace_sources:
        stale_ids: list[str] = []
        for row in connection.execute(
            "SELECT id, evidence_json FROM document_todos WHERE source_workspace = ?", (workspace,)
        ):
            try:
                evidence = json.loads(str(row["evidence_json"]))
            except json.JSONDecodeError:
                continue
            if isinstance(evidence, list) and any(
                isinstance(source, dict) and str(source.get("file") or "") in replace_sources
                for source in evidence
            ):
                stale_ids.append(str(row["id"]))
        connection.executemany("DELETE FROM document_todos WHERE id = ?", [(item_id,) for item_id in stale_ids])

    for raw in items:
        if not isinstance(raw, dict):
            raise ToolError("일정 항목 형식이 올바르지 않습니다.")
        source_candidate_id = str(raw.get("sourceCandidateId") or raw.get("id") or "")
        todo_id = _todo_id(root, source_candidate_id) if source_candidate_id else ""
        prior = connection.execute(
            "SELECT created_at FROM document_todos WHERE id = ?", (todo_id,)
        ).fetchone() if todo_id else None
        item = _normalise_todo_record(
            root,
            raw,
            created_at=str(prior["created_at"]) if prior is not None else None,
        )
        connection.execute(
            """
            INSERT INTO document_todos (
              id, source_workspace, source_candidate_id, title, priority, due_date,
              due_time, end_time, start_date, end_date, estimated_minutes, recurrence_json, status, evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              source_workspace = excluded.source_workspace,
              source_candidate_id = excluded.source_candidate_id,
              title = excluded.title,
              priority = excluded.priority,
              due_date = excluded.due_date,
              due_time = excluded.due_time,
              end_time = excluded.end_time,
              start_date = excluded.start_date,
              end_date = excluded.end_date,
              estimated_minutes = excluded.estimated_minutes,
              recurrence_json = excluded.recurrence_json,
              status = excluded.status,
              evidence_json = excluded.evidence_json,
              updated_at = excluded.updated_at
            """,
            (
                item["id"], item["workspace"], item["sourceCandidateId"], item["title"],
                item["priority"], item["dueDate"], item["dueTime"], item["endTime"], item["startDate"], item["endDate"],
                item["estimatedMinutes"], json.dumps(item["recurrence"], ensure_ascii=False) if item["recurrence"] else None, item["status"],
                json.dumps(item["evidence"], ensure_ascii=False), item["createdAt"], item["updatedAt"],
            ),
        )


def _insert_central_todo(item: dict[str, Any]) -> dict[str, Any]:
    """Persist one already-normalised Aiso-owned ToDo atomically."""
    _migrate_legacy_stores_once()
    connection = _open_database()
    try:
        connection.execute(
            """
            INSERT INTO document_todos (
              id, source_workspace, source_candidate_id, title, priority, due_date,
              due_time, end_time, start_date, end_date, estimated_minutes, recurrence_json,
              status, evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"], item["workspace"], item["sourceCandidateId"], item["title"], item["priority"],
                item["dueDate"], item["dueTime"], item["endTime"], item["startDate"], item["endDate"],
                item["estimatedMinutes"], json.dumps(item["recurrence"], ensure_ascii=False) if item["recurrence"] else None,
                item["status"], json.dumps(item["evidence"], ensure_ascii=False), item["createdAt"], item["updatedAt"],
            ),
        )
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise ToolError("Aiso 캘린더에 일정을 등록하지 못했습니다.") from error
    finally:
        connection.close()
    return item


def create_todo_item(
    *,
    title: str,
    priority: str = "medium",
    start_date: str | None = None,
    end_date: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    estimated_minutes: int | None = None,
    recurrence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a central ToDo from the planner UI or another trusted boundary."""
    checked_title = _normalise_title(title)
    if not checked_title:
        raise ToolError("일정 이름을 입력하세요.")
    checked_start = _normalise_schedule_date(start_date)
    checked_end = _normalise_schedule_date(end_date) or checked_start
    if checked_end and not checked_start:
        checked_start = checked_end
    if checked_start and checked_end and checked_start > checked_end:
        raise ToolError("작업 시작일은 종료일보다 늦을 수 없습니다.")
    checked_start_time = _normalise_clock_time(start_time)
    checked_end_time = _normalise_clock_time(end_time)
    if checked_start_time and checked_end_time and checked_end_time <= checked_start_time:
        raise ToolError("종료 시각은 시작 시각보다 늦어야 합니다.")
    event_id = uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "id": event_id,
        "workspace": _AISO_CALENDAR_WORKSPACE,
        "sourceCandidateId": f"manual:{event_id}",
        "title": checked_title,
        "priority": priority if priority in {"high", "medium", "low"} else "medium",
        "dueDate": checked_end,
        "dueTime": checked_start_time,
        "endTime": checked_end_time,
        "startDate": checked_start,
        "endDate": checked_end,
        "estimatedMinutes": _normalise_estimated_minutes(estimated_minutes),
        "recurrence": _normalise_recurrence(recurrence),
        "status": "open",
        "evidence": [{
            "file": "Aiso Planner",
            "location": "사용자 직접 등록",
            "quote": checked_title,
        }],
        "createdAt": now,
        "updatedAt": now,
        "scheduleBlocks": [],
    }
    return _insert_central_todo(item)


def create_calendar_todo(instruction: str, *, today: date | None = None) -> dict[str, Any]:
    """Create one central Aiso calendar event from a natural-language request.

    This intentionally has no workspace parameter.  Personal calendar events
    belong to Aiso, not to whichever project folder happened to be selected
    while the user entered the instruction.
    """
    raw = str(instruction or "").strip()
    if len(raw) < 2:
        raise ToolError("등록할 일정 내용을 입력하세요.")
    if len(raw) > 2000:
        raise ToolError("일정 지시는 2,000자 이하여야 합니다.")
    anchor_today = today or date.today()
    explicit_date, _explicit_year = _calendar_date_from_instruction(raw, anchor_today)
    recurrence, start = _calendar_recurrence(raw, today=anchor_today, explicit_date=explicit_date)
    start_time, end_time = _calendar_time_range(raw)
    if start_time and end_time and end_time <= start_time:
        raise ToolError("종료 시각은 시작 시각보다 늦어야 합니다. 자정을 넘는 일정은 두 개로 나누어 등록하세요.")
    title = _calendar_title(raw)
    if len(title) < 2:
        raise ToolError("일정 이름을 알 수 없습니다. 예: “매주 일요일 10시 알바 일정 등록해줘”처럼 이름을 포함하세요.")
    estimated = 30
    if start_time and end_time:
        start_minutes = int(start_time[:2]) * 60 + int(start_time[3:])
        end_minutes = int(end_time[:2]) * 60 + int(end_time[3:])
        estimated = end_minutes - start_minutes
    now = datetime.now(timezone.utc).isoformat()
    event_id = uuid4().hex
    item = {
        "id": event_id,
        "workspace": _AISO_CALENDAR_WORKSPACE,
        "sourceCandidateId": f"calendar:{event_id}",
        "title": title,
        "priority": _calendar_priority(raw),
        "dueDate": start.isoformat(),
        "dueTime": start_time,
        "endTime": end_time,
        "startDate": start.isoformat(),
        "endDate": start.isoformat(),
        "estimatedMinutes": estimated,
        "recurrence": recurrence,
        "status": "open",
        "evidence": [{
            "file": "Aiso Calendar",
            "location": "사용자 직접 등록",
            "quote": raw[:1000],
        }],
        "createdAt": now,
        "updatedAt": now,
        "scheduleBlocks": [],
    }
    return _insert_central_todo(item)


async def create_todo_event(instruction: str) -> str:
    """Agent tool wrapper for a centrally persisted personal schedule."""
    item = create_calendar_todo(instruction)
    recurrence = item.get("recurrence") or {}
    labels = {
        "daily": "매일",
        "weekly": "매주",
        "monthly": "매월",
        "yearly": "매년",
    }
    repeat_label = labels.get(str(recurrence.get("frequency") or ""), "한 번")
    time_label = item["dueTime"] or "시간 미정"
    if item.get("endTime"):
        time_label = f"{time_label}–{item['endTime']}"
    return (
        "Aiso 캘린더 일정 등록 완료\n"
        f"- 일정: {item['title']}\n"
        f"- 시작: {item['startDate']} {time_label}\n"
        f"- 반복: {repeat_label}\n"
        f"- 우선순위: {item['priority']}\n"
        "- Discord에는 메시지나 예약을 만들지 않았습니다."
    )


def _migrate_legacy_stores_once() -> None:
    """Import old workspace JSON exactly once, then remove the retired copies."""
    connection = _open_database()
    try:
        completed = connection.execute(
            "SELECT 1 FROM document_todo_meta WHERE key = 'legacy-workspace-json-v1'"
        ).fetchone()
        if completed is not None:
            return
        migrated_paths: list[Path] = []
        for root in _known_todo_roots():
            try:
                store = _read_legacy_store(root)
                legacy_items = [item for item in store.get("items", []) if isinstance(item, dict)]
                if legacy_items:
                    _write_todos(connection, root, legacy_items)
                migrated_paths.append(_legacy_store_path(root))
            except ToolError:
                continue
        connection.execute(
            "INSERT OR REPLACE INTO document_todo_meta (key, value) VALUES (?, ?)",
            ("legacy-workspace-json-v1", datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise ToolError("기존 일정을 Aiso 데이터베이스로 이관하지 못했습니다.") from error
    finally:
        connection.close()
    for path in migrated_paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # The central copy is already committed. A stale legacy file is
            # ignored after the migration marker and can be removed later.
            pass


def _list_items(workspace: str | None = None) -> list[dict[str, Any]]:
    _migrate_legacy_stores_once()
    connection = _open_database()
    try:
        if workspace is None:
            rows = connection.execute(
                "SELECT * FROM document_todos ORDER BY COALESCE(due_date, '9999-12-31'), COALESCE(due_time, '99:99'), created_at, title"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM document_todos WHERE source_workspace = ? ORDER BY COALESCE(due_date, '9999-12-31'), COALESCE(due_time, '99:99'), created_at, title",
                (workspace,),
            ).fetchall()
        items = [_row_to_item(row) for row in rows]
        blocks_by_item = _schedule_blocks_for_items(connection, [str(item["id"]) for item in items])
        for item in items:
            item["scheduleBlocks"] = blocks_by_item.get(str(item["id"]), [])
        return items
    except sqlite3.Error as error:
        raise ToolError("Aiso 캘린더 목록을 읽을 수 없습니다.") from error
    finally:
        connection.close()


def list_todos(workspace: str) -> dict[str, Any]:
    root = validate_workspace(workspace)
    items = _list_items(str(root))
    return {"version": 3, "items": items}


def list_saved_todos() -> dict[str, Any]:
    """Read Aiso-owned ToDos without requiring a current workspace."""
    items = _list_items()
    workspaces = list(dict.fromkeys(str(item["workspace"]) for item in items))
    return {"version": 3, "items": items, "workspaces": workspaces}


def save_todos(
    workspace: str,
    items: list[dict[str, Any]],
    *,
    replace_sources: set[str] | None = None,
) -> dict[str, Any]:
    root = validate_workspace(workspace)
    if not isinstance(items, list) or not items:
        raise ToolError("저장할 일정 항목을 하나 이상 선택하세요.")
    if len(items) > MAX_CANDIDATES:
        raise ToolError(f"한 번에 최대 {MAX_CANDIDATES}개 일정만 저장할 수 있습니다.")
    _migrate_legacy_stores_once()
    connection = _open_database()
    try:
        _write_todos(connection, root, items, replace_sources=replace_sources)
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise ToolError("Aiso 캘린더 일정을 저장하지 못했습니다.") from error
    finally:
        connection.close()
    return list_todos(str(root))


def update_todo(item_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    _migrate_legacy_stores_once()
    connection = _open_database()
    try:
        row = connection.execute("SELECT * FROM document_todos WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ToolError("수정할 일정을 찾지 못했습니다.")
        item = _row_to_item(row)
        schedule_changed = any(key in patch for key in (
            "dueDate", "startDate", "endDate", "dueTime", "endTime", "estimatedMinutes", "recurrence",
        ))
        if "title" in patch:
            title = _normalise_title(str(patch["title"]))
            if not title:
                raise ToolError("일정 이름을 입력하세요.")
            item["title"] = title
        if "status" in patch and patch["status"] in {"open", "done"}:
            item["status"] = patch["status"]
        if "dueDate" in patch:
            item["endDate"] = _normalise_schedule_date(patch["dueDate"])
            if item["endDate"] and not item.get("startDate"):
                item["startDate"] = item["endDate"]
        if "startDate" in patch:
            item["startDate"] = _normalise_schedule_date(patch["startDate"])
        if "endDate" in patch:
            item["endDate"] = _normalise_schedule_date(patch["endDate"])
        if item.get("startDate") and not item.get("endDate"):
            item["endDate"] = item["startDate"]
        if item.get("endDate") and not item.get("startDate"):
            item["startDate"] = item["endDate"]
        if item.get("startDate") and item.get("endDate") and item["startDate"] > item["endDate"]:
            raise ToolError("작업 시작일은 종료일보다 늦을 수 없습니다.")
        item["dueDate"] = item.get("endDate")
        if "dueTime" in patch:
            item["dueTime"] = _normalise_clock_time(patch["dueTime"])
        if "endTime" in patch:
            item["endTime"] = _normalise_clock_time(patch["endTime"])
        if item.get("dueTime") and item.get("endTime") and str(item["endTime"]) <= str(item["dueTime"]):
            raise ToolError("종료 시각은 시작 시각보다 늦어야 합니다.")
        if "estimatedMinutes" in patch:
            item["estimatedMinutes"] = _normalise_estimated_minutes(patch["estimatedMinutes"])
        if "priority" in patch and patch["priority"] in {"high", "medium", "low"}:
            item["priority"] = patch["priority"]
        if "recurrence" in patch:
            item["recurrence"] = _normalise_recurrence(patch["recurrence"])
        item["updatedAt"] = datetime.now(timezone.utc).isoformat()
        connection.execute(
            "UPDATE document_todos SET title = ?, priority = ?, due_date = ?, due_time = ?, end_time = ?, start_date = ?, end_date = ?, estimated_minutes = ?, recurrence_json = ?, status = ?, updated_at = ? WHERE id = ?",
            (
                item["title"], item["priority"], item["dueDate"], item["dueTime"], item.get("endTime"), item["startDate"], item["endDate"],
                item["estimatedMinutes"], json.dumps(item["recurrence"], ensure_ascii=False) if item["recurrence"] else None,
                item["status"], item["updatedAt"], item_id,
            ),
        )
        # A direct calendar drag or manual range edit is the user's newer
        # decision.  Clear only the old recovery allocations, not the ToDo or
        # its evidence, so stale split blocks cannot keep appearing elsewhere.
        if schedule_changed:
            connection.execute("DELETE FROM document_todo_schedule_blocks WHERE todo_id = ?", (item_id,))
        connection.commit()
        item["scheduleBlocks"] = [] if schedule_changed else _schedule_blocks_for_items(connection, [item_id]).get(item_id, [])
        return item
    except sqlite3.Error as error:
        connection.rollback()
        raise ToolError("Aiso 캘린더 일정을 수정하지 못했습니다.") from error
    finally:
        connection.close()


def _resolve_todo_target(
    items: list[dict[str, Any]],
    *,
    instruction: str,
    todo_id: str | None,
    target_title: str | None,
) -> dict[str, Any]:
    """Resolve exactly one ToDo and fail closed instead of guessing a mutation target."""
    if todo_id:
        exact = [item for item in items if str(item.get("id") or "") == todo_id.strip()]
        if len(exact) == 1:
            return exact[0]
        raise ToolError("지정한 ID의 일정을 찾지 못했습니다. 먼저 현재 캘린더 목록을 확인하세요.")

    candidates: list[dict[str, Any]] = []
    requested = str(target_title or "").strip().casefold()
    if requested:
        candidates = [
            item for item in items
            if requested == str(item.get("title") or "").strip().casefold()
            or requested in str(item.get("title") or "").strip().casefold()
        ]
    else:
        folded = instruction.casefold()
        candidates = [
            item for item in items
            if str(item.get("title") or "").strip()
            and str(item.get("title") or "").strip().casefold() in folded
        ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ToolError("수정할 일정을 특정하지 못했습니다. 현재 제목이나 일정 ID를 함께 알려주세요.")
    names = ", ".join(str(item.get("title") or "제목 없음") for item in candidates[:5])
    raise ToolError(f"수정 대상이 여러 개입니다: {names}. 정확한 제목이나 일정 ID를 지정하세요.")


_BULK_CALENDAR_DELETE_PATTERNS = (
    # This deliberately accepts only commands whose entire semantic payload is
    # "delete every calendar event".  In particular, phrases such as
    # "전체 일정 중 회의만 삭제" do not match and therefore keep the normal
    # one-item, fail-closed target resolution path.
    re.compile(
        r"^(?:(?:aiso(?:의|에)?(?:캘린더(?:의|에)?)?|내|나의))?"
        r"(?:(?:등록(?:되어|돼)?(?:있는)?|등록된|저장(?:되어|돼)?(?:있는)?|저장된|현재))?"
        r"(?:전체|모든|전부|모두)(?:캘린더)?(?:일정|스케줄|할일|todo(?:s)?|calendar(?:events?)?)"
        r"(?:을|를|들)?(?:전부|모두|다)?(?:삭제|지워|제거|없애)(?:해|줘|주세요|해줘|해주시겠어요)?$"
    ),
    re.compile(
        r"^(?:please)?(?:delete|remove|clear)(?:all|every)(?:the)?"
        r"(?:registered|saved|stored)?(?:aiso)?(?:calendar)?(?:events?|todos?)(?:please)?$"
    ),
)


def _is_explicit_bulk_calendar_delete(instruction: str) -> bool:
    """Return true only for an unqualified command to erase every Aiso event.

    The tool may receive a model-provided ``action`` field, but the user's
    original sentence is the authority for a destructive scope.  Collapsing
    harmless spacing and punctuation lets natural Korean/English commands
    through without broadening the scope into title guessing.
    """
    compact = re.sub(r"[\s.,!?…]+", "", str(instruction or "").casefold())
    return bool(compact) and any(pattern.fullmatch(compact) for pattern in _BULK_CALENDAR_DELETE_PATTERNS)


def _infer_management_action(instruction: str, action: str | None) -> str:
    if action in {"update", "complete", "reopen", "delete", "delete_all"}:
        return action
    folded = instruction.casefold()
    if any(word in folded for word in ("삭제", "지워", "제거", "delete", "remove")):
        return "delete"
    if any(word in folded for word in ("다시 열", "미완료로", "완료 취소", "reopen", "mark open")):
        return "reopen"
    if any(word in folded for word in ("완료", "끝냈", "끝남", "complete", "mark done", "finished")):
        return "complete"
    return "update"


def _next_weekday_date(instruction: str, today: date) -> date | None:
    folded = instruction.casefold()
    matches = [
        value for token, value in _CALENDAR_WEEKDAYS.items()
        if re.search(rf"(?<![a-z가-힣]){re.escape(token)}(?![a-z가-힣])", folded)
    ]
    if len(set(matches)) != 1:
        return None
    weekday = matches[0]
    offset = (weekday - _calendar_weekday(today)) % 7
    if offset == 0 and any(marker in folded for marker in ("다음", "next")):
        offset = 7
    return today + timedelta(days=offset)


async def manage_todo(
    instruction: str,
    todo_id: str | None = None,
    target_title: str | None = None,
    action: str | None = None,
    new_title: str | None = None,
    priority: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    estimated_minutes: int | None = None,
    recurrence: dict[str, Any] | None = None,
) -> str:
    """Safely manage central calendar events through an Agent tool call."""
    raw = str(instruction or "").strip()
    if not raw:
        raise ToolError("캘린더 관리 요청 원문이 필요합니다.")

    explicit_bulk_delete = _is_explicit_bulk_calendar_delete(raw)
    if action == "delete_all" and not explicit_bulk_delete:
        raise ToolError(
            "전체 삭제는 ‘등록되어 있는 전체 일정 삭제해줘’처럼 삭제 범위를 명확히 지시해야 합니다. "
            "일부 일정만 삭제하려면 정확한 일정 제목이나 ID를 지정하세요."
        )
    if explicit_bulk_delete:
        if action not in {None, "delete", "delete_all"}:
            raise ToolError("전체 일정 삭제 요청의 작업 종류가 원문과 일치하지 않아 실행하지 않았습니다.")
        deleted_count = delete_all_todos()
        if deleted_count == 0:
            return "Aiso 캘린더에는 삭제할 등록 일정이 없습니다."
        return (
            "Aiso 캘린더 전체 일정 삭제 완료\n"
            f"- 삭제한 일정: {deleted_count}개\n"
            "- 범위: Aiso 중앙 캘린더에 등록된 모든 일정\n"
            "- 연결된 시간 배정 블록도 함께 정리했습니다."
        )

    item = _resolve_todo_target(
        list_saved_todos()["items"], instruction=raw, todo_id=todo_id, target_title=target_title,
    )
    resolved_action = _infer_management_action(raw, action)
    if resolved_action == "delete":
        delete_todo(str(item["id"]))
        return f"Aiso 캘린더 일정 삭제 완료\n- 일정: {item['title']}\n- ID: {item['id']}"
    if resolved_action in {"complete", "reopen"}:
        updated = update_todo(str(item["id"]), {"status": "done" if resolved_action == "complete" else "open"})
        state = "완료" if resolved_action == "complete" else "진행 중"
        return f"Aiso 캘린더 일정 상태 변경 완료\n- 일정: {updated['title']}\n- 상태: {state}\n- ID: {updated['id']}"

    patch: dict[str, Any] = {}
    if new_title is not None:
        patch["title"] = new_title
    if priority in {"high", "medium", "low"}:
        patch["priority"] = priority
    else:
        folded = raw.casefold()
        if "p1" in folded or "최우선" in folded or "높은 우선" in folded:
            patch["priority"] = "high"
        elif "p2" in folded or "중간 우선" in folded:
            patch["priority"] = "medium"
        elif "p3" in folded or "낮은 우선" in folded:
            patch["priority"] = "low"

    explicit, _ = _calendar_date_from_instruction(raw, date.today())
    inferred_date = explicit or _next_weekday_date(raw, date.today())
    checked_start = start_date or (inferred_date.isoformat() if inferred_date else None)
    checked_end = end_date or checked_start
    if checked_start is not None:
        patch["startDate"] = checked_start
    if checked_end is not None:
        patch["endDate"] = checked_end

    inferred_start_time, inferred_end_time = _calendar_time_range(raw)
    if start_time is not None or inferred_start_time is not None:
        patch["dueTime"] = start_time if start_time is not None else inferred_start_time
    if end_time is not None or inferred_end_time is not None:
        patch["endTime"] = end_time if end_time is not None else inferred_end_time
    if estimated_minutes is not None:
        patch["estimatedMinutes"] = estimated_minutes
    if recurrence is not None:
        patch["recurrence"] = recurrence
    elif any(marker in raw.casefold() for marker in ("반복 해제", "반복 없", "반복 취소", "stop repeating", "no recurrence")):
        patch["recurrence"] = None
    elif any(marker in raw.casefold() for marker in ("매일", "매주", "매월", "매년", "daily", "weekly", "monthly", "yearly")):
        parsed_recurrence, parsed_anchor = _calendar_recurrence(raw, today=date.today(), explicit_date=inferred_date)
        patch["recurrence"] = parsed_recurrence
        patch.setdefault("startDate", parsed_anchor.isoformat())
        patch.setdefault("endDate", parsed_anchor.isoformat())

    if not patch:
        raise ToolError("변경할 내용을 찾지 못했습니다. 날짜·시간·이름·우선순위 또는 상태를 명확히 지정하세요.")
    updated = update_todo(str(item["id"]), patch)
    recurrence_label = (updated.get("recurrence") or {}).get("frequency") or "없음"
    return (
        "Aiso 캘린더 일정 수정 완료\n"
        f"- 일정: {updated['title']}\n"
        f"- 기간: {updated.get('startDate') or '미정'} ~ {updated.get('endDate') or '미정'}\n"
        f"- 시간: {updated.get('dueTime') or '미정'} ~ {updated.get('endTime') or '미정'}\n"
        f"- 우선순위: {updated['priority']}\n"
        f"- 반복: {recurrence_label}\n"
        f"- ID: {updated['id']}"
    )


def _normalise_replan_date(value: str | None) -> date:
    if value is None:
        return date.today()
    checked = _normalise_schedule_date(value)
    if checked is None:  # Kept for the type checker; None is handled above.
        return date.today()
    return date.fromisoformat(checked)


def _future_workdays(after: date, *, maximum: int = _REPLAN_MAX_WORKDAYS) -> list[str]:
    """Return the next weekday slots for a conservative recovery proposal."""
    values: list[str] = []
    current = after + timedelta(days=1)
    while len(values) < maximum:
        if current.weekday() < 5:
            values.append(current.isoformat())
        current += timedelta(days=1)
    return values


def _build_reschedule_preview(items: list[dict[str, Any]], *, as_of: date) -> dict[str, Any]:
    """Make a deterministic, reviewable plan for overdue open work.

    This intentionally does not call an LLM.  It protects a creator's calendar
    from hallucinated dates and gives every proposal a reproducible reason:
    P1 first, then its previous end date, then registration order.  The user
    still has to apply it explicitly.
    """
    as_of_key = as_of.isoformat()
    overdue = [
        item for item in items
        if item.get("status") == "open"
        and not item.get("recurrence")
        and isinstance(item.get("endDate"), str)
        and str(item["endDate"]) <= as_of_key
    ]
    overdue.sort(key=lambda item: (
        _priority_rank(str(item.get("priority") or "medium")),
        str(item.get("endDate") or "9999-12-31"),
        str(item.get("createdAt") or ""),
        str(item.get("title") or ""),
    ))
    capacities = {day: _REPLAN_DAILY_CAPACITY_MINUTES for day in _future_workdays(as_of)}
    plans: list[dict[str, Any]] = []
    for item in overdue:
        remaining = _normalise_estimated_minutes(item.get("estimatedMinutes"))
        assignments: list[dict[str, Any]] = []
        for scheduled_date, capacity in capacities.items():
            if remaining <= 0:
                break
            if capacity <= 0:
                continue
            minutes = min(remaining, capacity, _REPLAN_BLOCK_MINUTES)
            capacities[scheduled_date] -= minutes
            remaining -= minutes
            assignments.append({"date": scheduled_date, "minutes": minutes})
        plans.append({
            "todoId": str(item["id"]),
            "title": str(item["title"]),
            "priority": str(item["priority"]),
            "totalMinutes": _normalise_estimated_minutes(item.get("estimatedMinutes")),
            "assignments": assignments,
            "unallocatedMinutes": remaining,
        })
    return {
        "asOf": as_of_key,
        "dailyCapacityMinutes": _REPLAN_DAILY_CAPACITY_MINUTES,
        "plans": plans,
        "totalMinutes": sum(int(plan["totalMinutes"]) for plan in plans),
        "unallocatedMinutes": sum(int(plan["unallocatedMinutes"]) for plan in plans),
    }


def preview_reschedule(as_of: str | None = None) -> dict[str, Any]:
    """Preview a missed-work recovery plan without writing to the database."""
    return _build_reschedule_preview(_list_items(), as_of=_normalise_replan_date(as_of))


def apply_reschedule(as_of: str | None = None) -> dict[str, Any]:
    """Apply the current deterministic recovery proposal after user approval."""
    proposal = preview_reschedule(as_of)
    if proposal["unallocatedMinutes"]:
        raise ToolError("향후 14개 평일 안에 미완료 작업을 모두 배정할 수 없습니다. 작업량이나 기간을 조정한 뒤 다시 제안하세요.")
    plans = proposal["plans"]
    if not plans:
        return {"proposal": proposal, "items": list_saved_todos()["items"]}

    now = datetime.now(timezone.utc).isoformat()
    connection = _open_database()
    try:
        for plan in plans:
            todo_id = str(plan["todoId"])
            assignments = list(plan["assignments"])
            if not assignments:
                continue
            row = connection.execute(
                "SELECT id, status, estimated_minutes FROM document_todos WHERE id = ?", (todo_id,)
            ).fetchone()
            if row is None or str(row["status"]) != "open":
                raise ToolError("일정 제안 대상이 바뀌었습니다. 다시 제안을 확인하세요.")
            if _normalise_estimated_minutes(row["estimated_minutes"]) != int(plan["totalMinutes"]):
                raise ToolError("일정 제안 대상의 작업량이 바뀌었습니다. 다시 제안을 확인하세요.")
            connection.execute("DELETE FROM document_todo_schedule_blocks WHERE todo_id = ?", (todo_id,))
            for assignment in assignments:
                scheduled_date = _normalise_schedule_date(assignment.get("date"))
                minutes = _normalise_estimated_minutes(assignment.get("minutes"))
                if scheduled_date is None:
                    raise ToolError("일정 제안 날짜가 올바르지 않습니다.")
                block_id = hashlib.sha256(f"{todo_id}\0{scheduled_date}".encode("utf-8")).hexdigest()[:32]
                connection.execute(
                    """
                    INSERT INTO document_todo_schedule_blocks (id, todo_id, scheduled_date, minutes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (block_id, todo_id, scheduled_date, minutes, now, now),
                )
            start_date = str(assignments[0]["date"])
            end_date = str(assignments[-1]["date"])
            connection.execute(
                "UPDATE document_todos SET start_date = ?, end_date = ?, due_date = ?, updated_at = ? WHERE id = ?",
                (start_date, end_date, end_date, now, todo_id),
            )
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise ToolError("제안한 캘린더 일정을 적용하지 못했습니다.") from error
    finally:
        connection.close()
    return {"proposal": proposal, "items": list_saved_todos()["items"]}


def delete_todo(item_id: str) -> None:
    """Permanently remove one user-selected item from Aiso's central ToDo store."""
    _migrate_legacy_stores_once()
    connection = _open_database()
    try:
        cursor = connection.execute("DELETE FROM document_todos WHERE id = ?", (item_id,))
        if cursor.rowcount != 1:
            raise ToolError("삭제할 일정을 찾지 못했습니다.")
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise ToolError("Aiso 캘린더 일정을 삭제하지 못했습니다.") from error
    finally:
        connection.close()


def delete_all_todos() -> int:
    """Permanently remove every Aiso-owned calendar event in one transaction.

    This intentionally has no workspace selector: the central calendar is the
    only scope exposed by ``manage_calendar_event``.  Callers must still pass
    the strict natural-language scope check in ``manage_todo`` before reaching
    this function.  SQLite foreign keys remove associated schedule blocks.
    """
    _migrate_legacy_stores_once()
    connection = _open_database()
    try:
        row = connection.execute("SELECT COUNT(*) AS count FROM document_todos").fetchone()
        count = int(row["count"] if row is not None else 0)
        connection.execute("DELETE FROM document_todos")
        connection.commit()
        return count
    except sqlite3.Error as error:
        connection.rollback()
        raise ToolError("Aiso 캘린더 전체 일정을 삭제하지 못했습니다.") from error
    finally:
        connection.close()
