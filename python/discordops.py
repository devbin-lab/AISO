# -*- coding: utf-8 -*-
"""디스코드 서버 구성 코어 — 구조 조회(map)·작업 검증(validate)·적용(apply).

에이전트 탭(REGISTRY 툴 discord_server_map/apply)과 디스코드 #aiso 채널(툴콜 루프)이
이 한 모듈을 공유한다. discord.py는 모듈 로드 시 import하지 않는다(함수 안 지연 import) —
discord 미설치 환경에서도 toolspec/agent가 스키마·검증(순수 파이썬)을 쓸 수 있어야 한다.

안전 규칙:
- 명령 채널(#aiso)을 대상으로 하는 모든 작업은 거부 — 봇의 유일한 제어 통로 보호.
- 한 번에 최대 MAX_OPS개. 삭제는 복구 불가이므로 호출 측이 반드시 승인을 받은 뒤 적용한다
  (에이전트 탭=승인 다이얼로그(자동 모드 포함 강제), 디스코드=소유자 승인 버튼).
"""
from __future__ import annotations

MAX_OPS = 40      # 한 번에 적용할 수 있는 작업 수 상한(폭주 방지)
NAME_MAX = 95     # 채널·카테고리 이름 길이 상한(디스코드 100보다 보수적)
TOPIC_MAX = 1024  # 채널 주제 길이 상한(디스코드 규격)
REASON = "Aiso 서버 구성"  # 감사 로그(Audit Log)에 남는 사유

ACTIONS = (
    "create_category",
    "create_text_channel",
    "create_voice_channel",
    "rename",
    "move",
    "set_topic",
    "delete",
)

MAP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discord_server_map",
        "description": (
            "연결된 디스코드 서버의 현재 구조(카테고리·채널 목록)를 조회한다. "
            "서버 구성을 바꾸기 전에 반드시 먼저 호출해 현재 상태를 파악하라."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

APPLY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discord_server_apply",
        "description": (
            "디스코드 서버의 채널 구조를 바꾸는 작업 목록(ops)을 순서대로 적용한다 — "
            "카테고리/채널 생성·이름변경·이동·주제설정·삭제. 적용 전 사용자 승인이 필요하며, "
            "반드시 discord_server_map으로 현재 구조를 확인한 뒤 사용하라. "
            "같은 배치에서 먼저 만든 카테고리를 뒤의 채널 생성에서 바로 쓸 수 있다."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "description": f"순서대로 적용할 작업 목록 (최대 {MAX_OPS}개)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": list(ACTIONS),
                                "description": "작업 종류",
                            },
                            "name": {"type": "string", "description": "create_*: 새로 만들 이름"},
                            "category": {
                                "type": "string",
                                "description": "create_text/voice_channel·move: 소속시킬 카테고리 이름(생략=무소속)",
                            },
                            "target": {
                                "type": "string",
                                "description": "rename·move·set_topic·delete: 대상 채널/카테고리의 이름 또는 ID",
                            },
                            "new_name": {"type": "string", "description": "rename: 새 이름"},
                            "topic": {"type": "string", "description": "create_text_channel·set_topic: 채널 주제"},
                        },
                        "required": ["action"],
                    },
                }
            },
            "required": ["ops"],
        },
    },
}


def _norm(s) -> str:
    return str(s or "").strip()


def pick_first(args: dict, *keys) -> str:
    """dict에서 후보 키를 순서대로 보며 첫 '비어있지 않은' stripped 값을 고른다.

    약한 모델의 필드명 편차(channel/channel_name/target …)를 흡수하는 정규화 헬퍼 — 전송·예약 공용."""
    for k in keys:
        v = _norm(args.get(k))
        if v:
            return v
    return ""


# 약한 로컬 모델(특히 gpt-oss)이 스키마 필드명을 제멋대로 바꿔 내는 것을 흡수한다.
# 실측: action↔op↔type, category↔parent↔category_name, create_channel+type↔create_text/voice_channel.
_ACTION_WORDS = frozenset(ACTIONS) | {"create_channel"}


def _canonical_op(op) -> dict:
    """모델이 낸 op의 필드명 편차를 표준 스키마(action/name/category/target/new_name/topic)로 정규화."""
    if not isinstance(op, dict):
        return op
    o = dict(op)
    # action 필드: action | op | (action처럼 생긴 type 값)
    action = _norm(o.get("action")) or _norm(o.get("op"))
    type_val = _norm(o.get("type"))
    if not action and type_val in _ACTION_WORDS:
        action, type_val = type_val, ""  # type이 실은 action 역할
    # create_channel(+채널종류) → create_text/voice_channel
    if action == "create_channel":
        kind = (type_val or _norm(o.get("channel_type"))).lower()
        action = "create_voice_channel" if kind in ("voice", "음성", "voice_channel") else "create_text_channel"
    o["action"] = action
    # category 필드: category | parent | category_name | parent_name | parent_category
    if not _norm(o.get("category")):
        cat = _norm(
            o.get("parent") or o.get("category_name") or o.get("parent_name") or o.get("parent_category")
        )
        if cat:
            o["category"] = cat
    # target 필드: target | id | channel | channel_id | channel_name | target_name
    if not _norm(o.get("target")):
        tgt = _norm(
            o.get("id") or o.get("channel") or o.get("channel_id")
            or o.get("channel_name") or o.get("target_name")
        )
        if tgt:
            o["target"] = tgt
    # new_name 필드: new_name | newName | new | rename_to | to
    if not _norm(o.get("new_name")):
        nn = _norm(o.get("newName") or o.get("new") or o.get("rename_to") or o.get("to"))
        if nn:
            o["new_name"] = nn
    return o


# 서버 설계 기준 — 12B급 로컬 모델이 '얇은 구조'(카테고리 2개, 채널 5~6개)로 끝내지 않도록
# 실제 팀이 바로 쓸 수 있는 업무용 구조의 구체적 체크리스트를 준다. 두 입구(에이전트 탭·디스코드) 공유.
DESIGN_GUIDE = (
    "서버 설계 기준 — 실제 팀이 바로 쓸 수 있는 업무용 구조로 설계하라:\n"
    "1) 「운영」 카테고리를 먼저: #공지(topic: 팀 공지사항), #회의록, #자유잡담.\n"
    "2) 직군별 카테고리로 나눠라 — 게임 개발팀이면 기획 / 아트 / 프로그래밍 / 사운드 / QA·테스트 / 빌드·배포처럼 "
    "실제 업무 직군을 모두 포함(다른 팀이면 그 팀의 직군으로).\n"
    "3) 각 직군 카테고리에 최소 2개: #<직군>-소통(논의), #<직군>-자료실(산출물·레퍼런스 공유). "
    "필요하면 작업 전용 채널을 추가(예: #버그-리포트, #빌드-알림).\n"
    "4) 음성: 공용 「회의실」 음성 1개 + 필요한 직군에 작업/회의 음성.\n"
    "5) 모든 텍스트 채널의 topic에 채널 용도를 한 줄로 적어라.\n"
    "6) 이름은 특별한 요청이 없으면 한국어로. 총 채널 15~25개 수준(카테고리 5~7개)."
)

# 검증 실패 시 모델에게 돌려줄 올바른 형식 예시 — 약한 모델이 다음 턴에 스스로 교정하도록.
FORMAT_HINT = (
    '올바른 형식으로 다시 시도하세요. 각 op은 정확히 이 필드명을 씁니다(다른 이름 금지): '
    '{"action":"create_category","name":"카테고리명"} / '
    '{"action":"create_text_channel","name":"채널명","category":"카테고리명"} / '
    '{"action":"create_voice_channel","name":"채널명","category":"카테고리명"} / '
    '{"action":"rename","target":"기존이름또는ID","new_name":"새이름"} / '
    '{"action":"move","target":"채널","category":"카테고리명"} / '
    '{"action":"delete","target":"채널"}. '
    'action 대신 op/type을, category 대신 parent를 쓰지 마세요.'
)


def _find(snapshot: dict, target) -> tuple[dict | None, str | None]:
    """이름 또는 ID로 채널/카테고리를 찾는다. 반환: (항목, 오류메시지)."""
    # 모델이 '#채널' 또는 '# 채널'(# 뒤 공백)로 주기도 하므로 #과 공백을 모두 벗긴다.
    t = _norm(_norm(target).lstrip("#"))
    if not t:
        return None, "대상(target)이 비어 있습니다"
    pool = list(snapshot.get("categories") or []) + list(snapshot.get("channels") or [])
    by_id = [x for x in pool if x.get("id") == t]
    if by_id:
        return by_id[0], None
    by_name = [x for x in pool if x.get("name") == t]
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        ids = ", ".join(x.get("id", "?") for x in by_name)
        return None, f"'{t}' 이름이 여러 개입니다 — ID로 지정하세요 (해당 ID: {ids})"
    return None, f"'{t}'을(를) 찾을 수 없습니다 (discord_server_map으로 현재 구조를 확인하세요)"


def _find_category(snapshot: dict, name, pending: set[str]) -> tuple[dict | None, str | None]:
    """카테고리 전용 탐색 — 이 배치에서 먼저 만들기로 한(pending) 이름도 유효로 인정."""
    n = _norm(name)
    if not n:
        return None, None  # 무소속
    if n in pending:
        return {"id": "", "name": n, "type": "category"}, None
    cats = [c for c in (snapshot.get("categories") or []) if c.get("name") == n or c.get("id") == n]
    if len(cats) == 1:
        return cats[0], None
    if len(cats) > 1:
        return None, f"카테고리 '{n}'이(가) 여러 개입니다 — ID로 지정하세요"
    return None, f"카테고리 '{n}'이(가) 없습니다 (먼저 create_category로 만들거나 이름을 확인하세요)"


def split_protected(ops, snapshot: dict) -> tuple[list, list[str]]:
    """보호 대상(#aiso 명령 채널)을 건드리는 op을 배치에서 분리한다.

    "전부 삭제해줘" 같은 대량 요청에 모델이 명령 채널까지 포함하는 것은 자연스러운 실수라,
    배치 전체를 거부하는 대신 해당 op만 빼고 진행한다(제외 사실은 미리보기·결과에 명시).
    validate_ops의 보호 오류는 백스톱으로 그대로 남는다."""
    if not isinstance(ops, list):
        return ops, []
    cmd_id = _norm(snapshot.get("command_channel_id"))
    if not cmd_id:
        return ops, []
    cmd_item, _ = _find(snapshot, cmd_id)
    cmd_name = (cmd_item or {}).get("name", "")
    kept: list = []
    skipped: list[str] = []
    for raw_op in ops:
        if not isinstance(raw_op, dict):
            kept.append(raw_op)
            continue
        op = _canonical_op(raw_op)
        action = _norm(op.get("action"))
        # 명령 채널을 대상으로 하는 변경·삭제 → 제외
        if action in ("rename", "move", "set_topic", "delete"):
            item, _err = _find(snapshot, op.get("target"))
            if item is not None and item.get("id") == cmd_id:
                skipped.append(f"#{cmd_name} — 명령 채널이라 {action} 불가(보호됨)")
                continue
        # 명령 채널 이름을 새로 만들거나 그 이름으로 바꾸는 것 → 제외
        if cmd_name and (
            (action.startswith("create_") and _norm(op.get("name")) == cmd_name)
            or (action == "rename" and _norm(op.get("new_name")) == cmd_name)
        ):
            skipped.append(f"'{cmd_name}' — 명령 채널 이름이라 사용 불가(보호됨)")
            continue
        kept.append(op)
    return kept, skipped


def validate_ops(ops, snapshot: dict) -> tuple[list[dict], list[str]]:
    """작업 목록을 검증·정규화한다. 반환: (정제된 ops, 오류 목록). 오류가 있으면 적용하면 안 된다."""
    errors: list[str] = []
    if not isinstance(ops, list) or not ops:
        return [], ["ops는 1개 이상의 작업 배열이어야 합니다"]
    if len(ops) > MAX_OPS:
        return [], [f"작업이 너무 많습니다 ({len(ops)}개) — 한 번에 최대 {MAX_OPS}개"]

    cmd_id = _norm(snapshot.get("command_channel_id"))
    # 명령 채널의 '이름'도 보호 — 봇은 재연결 시 이 이름으로 채널을 재채택하므로,
    # 같은 이름을 새로 만들거나 그 이름으로 바꾸면 보호 대상이 뒤섞일 수 있다.
    cmd_name = ""
    if cmd_id:
        cmd_item, _ = _find(snapshot, cmd_id)
        cmd_name = (cmd_item or {}).get("name", "")
    pending_cats: set[str] = set()  # 이 배치에서 만들기로 한 카테고리 이름
    existing_cat_names = {c.get("name") for c in (snapshot.get("categories") or [])}
    clean: list[dict] = []

    def _protected_name(i: int, name: str) -> bool:
        if cmd_name and name == cmd_name:
            errors.append(f"{i}번: '{cmd_name}'은(는) 명령 채널 이름이라 만들거나 바꿔 쓸 수 없습니다(혼동 방지)")
            return True
        return False

    def _check_target(i: int, op: dict) -> dict | None:
        item, err = _find(snapshot, op.get("target"))
        if err:
            errors.append(f"{i}번: {err}")
            return None
        if cmd_id and item.get("id") == cmd_id:
            errors.append(f"{i}번: 명령 채널(#{item.get('name')})은 변경·삭제할 수 없습니다(보호됨)")
            return None
        return item

    for i, raw_op in enumerate(ops, 1):
        if not isinstance(raw_op, dict):
            errors.append(f"{i}번: 작업은 객체여야 합니다")
            continue
        op = _canonical_op(raw_op)  # 필드명 편차(op/parent/type 등)를 표준으로 흡수
        action = _norm(op.get("action"))
        if action not in ACTIONS:
            errors.append(f"{i}번: 알 수 없는 작업 '{action}' (가능: {', '.join(ACTIONS)})")
            continue

        if action == "create_category":
            name = _norm(op.get("name"))
            if not name or len(name) > NAME_MAX:
                errors.append(f"{i}번: 카테고리 이름이 비었거나 너무 깁니다 (1~{NAME_MAX}자)")
                continue
            if _protected_name(i, name):
                continue
            if name in existing_cat_names or name in pending_cats:
                errors.append(f"{i}번: 카테고리 '{name}'이(가) 이미 있습니다(이름 중복은 혼동을 부르니 거부)")
                continue
            pending_cats.add(name)
            clean.append({"action": action, "name": name})

        elif action in ("create_text_channel", "create_voice_channel"):
            name = _norm(op.get("name"))
            if not name or len(name) > NAME_MAX:
                errors.append(f"{i}번: 채널 이름이 비었거나 너무 깁니다 (1~{NAME_MAX}자)")
                continue
            if _protected_name(i, name):
                continue
            _, cerr = _find_category(snapshot, op.get("category"), pending_cats)
            if cerr:
                errors.append(f"{i}번: {cerr}")
                continue
            topic = _norm(op.get("topic"))[:TOPIC_MAX]
            out = {"action": action, "name": name, "category": _norm(op.get("category"))}
            if action == "create_text_channel" and topic:
                out["topic"] = topic
            clean.append(out)

        elif action == "rename":
            item = _check_target(i, op)
            if item is None:
                continue
            new_name = _norm(op.get("new_name"))
            if not new_name or len(new_name) > NAME_MAX:
                errors.append(f"{i}번: 새 이름(new_name)이 비었거나 너무 깁니다 (1~{NAME_MAX}자)")
                continue
            if _protected_name(i, new_name):
                continue
            clean.append({"action": action, "target": item["id"], "new_name": new_name})

        elif action == "move":
            item = _check_target(i, op)
            if item is None:
                continue
            if item.get("type") == "category":
                errors.append(f"{i}번: 카테고리는 이동할 수 없습니다 (채널만 이동 가능)")
                continue
            _, cerr = _find_category(snapshot, op.get("category"), pending_cats)
            if cerr:
                errors.append(f"{i}번: {cerr}")
                continue
            clean.append({"action": action, "target": item["id"], "category": _norm(op.get("category"))})

        elif action == "set_topic":
            item = _check_target(i, op)
            if item is None:
                continue
            if item.get("type") != "text":
                errors.append(f"{i}번: 주제(topic)는 텍스트 채널에만 설정할 수 있습니다")
                continue
            clean.append({"action": action, "target": item["id"], "topic": _norm(op.get("topic"))[:TOPIC_MAX]})

        elif action == "delete":
            item = _check_target(i, op)
            if item is None:
                continue
            clean.append({"action": action, "target": item["id"]})

    return (clean, errors) if not errors else ([], errors)


def prepare_ops(ops, snapshot: dict) -> tuple["list | None", list[str], "str | None"]:
    """보호대상 분리 + 검증을 한 번에 — 두 적용 입구(에이전트 탭·디스코드)가 공유한다.

    반환 (clean, skipped, error_msg): error_msg가 있으면 적용하지 말고 그대로 사용자에게 보인다.
    clean이 None이면 적용할 것이 없다(전부 보호 대상)."""
    ops2, skipped = split_protected(ops, snapshot)
    if not ops2 and skipped:
        return None, skipped, (
            "[안내] 요청한 작업이 전부 보호 대상(명령 채널)뿐이라 적용할 것이 없습니다:\n- " + "\n- ".join(skipped)
        )
    clean, errors = validate_ops(ops2, snapshot)
    if errors:
        return None, skipped, (
            "[거부] 작업 목록에 문제가 있습니다:\n- " + "\n- ".join(errors) + "\n\n" + FORMAT_HINT
        )
    return clean, skipped, None


def format_skipped_report(skipped: list[str]) -> str:
    """적용 결과 뒤에 붙이는 '제외됨(보호)' 꼬리(여러 줄). 없으면 빈 문자열."""
    return ("\n제외됨(보호):\n- " + "\n- ".join(skipped)) if skipped else ""


# ── 표시(렌더링) ─────────────────────────────────────────────────────────
def render_map(snapshot: dict) -> str:
    """서버 구조를 사람이 읽는 텍스트로 — 에이전트 프롬프트와 디스코드 응답 양쪽에서 쓴다."""
    cmd_id = _norm(snapshot.get("command_channel_id"))
    cats = list(snapshot.get("categories") or [])
    chans = list(snapshot.get("channels") or [])
    icon = {"text": "#", "voice": "🔊", "etc": "•"}

    def _line(ch: dict) -> str:
        mark = " ★명령채널(보호됨·삭제/변경 불가, 작업 목록에 넣지 말 것)" if cmd_id and ch.get("id") == cmd_id else ""
        return f"  {icon.get(ch.get('type'), '•')} {ch.get('name')} (id {ch.get('id')}){mark}"

    lines = [f"서버: {snapshot.get('guild_name') or '(이름 없음)'} — 카테고리 {len(cats)}개, 채널 {len(chans)}개"]
    loose = [c for c in chans if not c.get("category_id")]
    if loose:
        lines.append("📁 (무소속)")
        lines += [_line(c) for c in loose]
    for cat in cats:
        lines.append(f"📁 {cat.get('name')} (id {cat.get('id')})")
        lines += [_line(c) for c in chans if c.get("category_id") == cat.get("id")]
    return "\n".join(lines)


def render_ops_preview(ops: list[dict], snapshot: dict) -> str:
    """승인 전 미리보기 — 삭제는 복구 불가임을 눈에 띄게 표시한다."""

    def _disp(target: str) -> str:
        item, _ = _find(snapshot, target)
        return item.get("name") if item else str(target)

    lines: list[str] = []
    deletes = 0
    for i, op in enumerate(ops, 1):
        a = op.get("action")
        if a == "create_category":
            lines.append(f"{i}. ➕ 카테고리 생성: {op.get('name')}")
        elif a == "create_text_channel":
            extra = f" (카테고리: {op.get('category')})" if op.get("category") else ""
            topic = f" — 주제: {op.get('topic')}" if op.get("topic") else ""
            lines.append(f"{i}. ➕ 텍스트 채널 생성: #{op.get('name')}{extra}{topic}")
        elif a == "create_voice_channel":
            extra = f" (카테고리: {op.get('category')})" if op.get("category") else ""
            lines.append(f"{i}. ➕ 음성 채널 생성: 🔊{op.get('name')}{extra}")
        elif a == "rename":
            lines.append(f"{i}. ✏ 이름 변경: {_disp(op.get('target'))} → {op.get('new_name')}")
        elif a == "move":
            dest = op.get("category") or "무소속"
            lines.append(f"{i}. 📁 이동: {_disp(op.get('target'))} → {dest}")
        elif a == "set_topic":
            t = op.get("topic")
            lines.append(f"{i}. ✏ 주제 설정: #{_disp(op.get('target'))} → {t if t else '(주제 비움)'}")
        elif a == "delete":
            deletes += 1
            lines.append(f"{i}. 🗑 **삭제(복구 불가)**: {_disp(op.get('target'))}")
    head = f"적용할 작업 {len(ops)}건:"
    tail = f"\n\n⚠ 삭제 {deletes}건은 되돌릴 수 없습니다." if deletes else ""
    return head + "\n" + "\n".join(lines) + tail


# ── 라이브 연결(지연 import — discord 미설치 환경 보호) ──────────────────
def _bot():
    try:
        import discordbot  # noqa: PLC0415 — 지연 import(모듈 로드 시 discord 의존 회피)
        return discordbot
    except Exception:  # noqa: BLE001
        return None


def available() -> bool:
    """봇이 켜져 있고 서버에 연결돼 있어 서버 구성 도구를 쓸 수 있는가."""
    db = _bot()
    if db is None:
        return False
    try:
        return db.is_running() and db.bound_guild() is not None
    except Exception:  # noqa: BLE001
        return False


def _live_guild():
    """(guild, command_channel_id) 또는 (None, 사용자용 오류 문자열)."""
    db = _bot()
    if db is None or not db.is_running():
        return None, "[불가] 디스코드 봇이 연결되어 있지 않습니다. 설정 탭에서 디스코드 봇을 켜세요."
    guild = db.bound_guild()
    if guild is None:
        return None, "[불가] 봇이 아직 서버에 들어가 있지 않습니다. 초대 후 다시 시도하세요."
    return (guild, db.command_channel_id()), None


def snapshot_guild(guild, command_channel_id: str) -> dict:
    """라이브 길드 → 순수 dict 스냅샷(검증·렌더링이 이 형태만 다룬다)."""
    import discord  # noqa: PLC0415

    cats = [{"id": str(c.id), "name": c.name, "type": "category"} for c in guild.categories]
    chans = []
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue
        if isinstance(ch, discord.TextChannel):
            t = "text"
        elif isinstance(ch, discord.VoiceChannel):
            t = "voice"
        else:
            t = "etc"  # 포럼·스테이지 등 — 이름변경/이동/삭제는 가능
        chans.append(
            {
                "id": str(ch.id),
                "name": ch.name,
                "type": t,
                "category_id": str(ch.category_id) if ch.category_id else "",
            }
        )
    return {
        "guild_name": guild.name,
        "command_channel_id": _norm(command_channel_id),
        "categories": cats,
        "channels": chans,
    }


async def apply_ops_live(guild, ops: list[dict], snapshot: dict) -> str:
    """검증된 ops를 순서대로 적용한다. 개별 실패는 건너뛰고 결과를 모아 보고한다."""
    import discord  # noqa: PLC0415

    created_cats: dict[str, object] = {}  # 이 배치에서 만든 카테고리 이름 → 객체
    ok: list[str] = []
    fail: list[str] = []

    def _live(target_id: str):
        obj = guild.get_channel(int(target_id)) if str(target_id).isdigit() else None
        return obj

    def _cat_obj(name: str):
        """카테고리 이름 → 라이브 객체(배치 내 생성분 우선). 없으면 (None, 오류)."""
        n = _norm(name)
        if not n:
            return None, None  # 무소속
        if n in created_cats:
            return created_cats[n], None
        item, err = _find_category(snapshot, n, set())
        if err or item is None or not item.get("id"):
            return None, err or f"카테고리 '{n}'이(가) 없습니다"
        obj = _live(item["id"])
        if obj is None:
            return None, f"카테고리 '{n}'이(가) 사라졌습니다"
        return obj, None

    for i, op in enumerate(ops, 1):
        a = op["action"]
        try:
            if a == "create_category":
                c = await guild.create_category(op["name"], reason=REASON)
                created_cats[op["name"]] = c
                ok.append(f"{i}. 카테고리 생성: {op['name']}")
            elif a in ("create_text_channel", "create_voice_channel"):
                cat, cerr = _cat_obj(op.get("category"))
                if cerr:
                    fail.append(f"{i}. {cerr}")
                    continue
                if a == "create_text_channel":
                    ch = await guild.create_text_channel(
                        op["name"], category=cat, topic=op.get("topic") or None, reason=REASON
                    )
                else:
                    ch = await guild.create_voice_channel(op["name"], category=cat, reason=REASON)
                ok.append(f"{i}. 채널 생성: {ch.name}")
            elif a == "rename":
                obj = _live(op["target"])
                if obj is None:
                    fail.append(f"{i}. 대상이 사라졌습니다 (id {op['target']})")
                    continue
                old = obj.name
                await obj.edit(name=op["new_name"], reason=REASON)
                ok.append(f"{i}. 이름 변경: {old} → {op['new_name']}")
            elif a == "move":
                obj = _live(op["target"])
                if obj is None:
                    fail.append(f"{i}. 대상이 사라졌습니다 (id {op['target']})")
                    continue
                cat, cerr = _cat_obj(op.get("category"))
                if cerr:
                    fail.append(f"{i}. {cerr}")
                    continue
                await obj.edit(category=cat, reason=REASON)
                ok.append(f"{i}. 이동: {obj.name} → {op.get('category') or '무소속'}")
            elif a == "set_topic":
                obj = _live(op["target"])
                if obj is None:
                    fail.append(f"{i}. 대상이 사라졌습니다 (id {op['target']})")
                    continue
                await obj.edit(topic=op.get("topic") or "", reason=REASON)
                ok.append(f"{i}. 주제 설정: {obj.name}")
            elif a == "delete":
                obj = _live(op["target"])
                if obj is None:
                    fail.append(f"{i}. 대상이 사라졌습니다 (id {op['target']})")
                    continue
                name = obj.name
                await obj.delete(reason=REASON)
                ok.append(f"{i}. 삭제: {name}")
        except discord.Forbidden:
            fail.append(f"{i}. 권한 부족({a}) — 봇 역할에 채널 관리 권한이 있는지 확인하세요")
        except Exception as e:  # noqa: BLE001 — 개별 실패는 배치 전체를 죽이지 않는다
            fail.append(f"{i}. 실패({a}): {e}")

    lines = [f"서버 구성 적용 완료 — 성공 {len(ok)}건, 실패 {len(fail)}건"]
    lines += ok
    if fail:
        lines.append("실패:")
        lines += fail
    return "\n".join(lines)


# ── 메시지 전송 ─────────────────────────────────────────────────────────
SEND_TEXT_MAX = 4000    # 한 번에 보낼 메시지 길이 상한(2000자 단위 분할 전송)
_MSG_CHUNK = 2000       # 디스코드 단일 메시지 상한

SEND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discord_send",
        "description": (
            "연결된 디스코드 서버의 텍스트 채널에 메시지를 보낸다(예: #공지에 안내 올리기). "
            "보내기 전 사용자 승인이 필요하다. 예약 전송이 필요하면 discord_schedule_add를 쓰라."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "보낼 텍스트 채널의 이름 또는 ID"},
                "message": {"type": "string", "description": "보낼 메시지 내용"},
            },
            "required": ["channel", "message"],
        },
    },
}


def canonical_send_args(args: dict) -> dict:
    """전송 인자의 필드명 편차(channel_name/target, text/content 등)를 표준으로 정규화."""
    a = dict(args or {})
    return {
        "channel": pick_first(a, "channel", "channel_name", "target", "room"),
        "message": pick_first(a, "message", "text", "content", "body"),
    }


def resolve_text_channel(channel) -> tuple["tuple[str, str] | None", "str | None"]:
    """채널 이름/ID → (id, name). 봇이 연결된 서버의 텍스트 채널만 허용."""
    got, err = _live_guild()
    if err:
        return None, err
    guild, cmd_id = got
    snap = snapshot_guild(guild, cmd_id)
    item, ferr = _find(snap, channel)
    if ferr:
        return None, ferr
    if item.get("type") != "text":
        return None, f"'{item.get('name')}'은(는) 텍스트 채널이 아닙니다 — 메시지는 텍스트 채널로만 보낼 수 있습니다"
    return (item["id"], item["name"]), None


def validate_send(channel, message) -> tuple["tuple[str, str, str] | None", "str | None"]:
    """전송 인자 검증. 반환 ((채널id, 채널명, 본문), 오류)."""
    body = str(message or "").strip()
    if not body:
        return None, "보낼 메시지가 비어 있습니다"
    if len(body) > SEND_TEXT_MAX:
        return None, f"메시지가 너무 깁니다({len(body)}자) — 최대 {SEND_TEXT_MAX}자"
    got, err = resolve_text_channel(channel)
    if err:
        return None, err
    ch_id, ch_name = got
    return (ch_id, ch_name, body), None


async def send_message_live(guild, channel_id: str, message: str) -> str:
    """검증된 대상에 실제 전송(2000자 분할)."""
    obj = guild.get_channel(int(channel_id)) if str(channel_id).isdigit() else None
    if obj is None:
        return "[불가] 채널이 사라졌습니다 — discord_server_map으로 다시 확인하세요"
    try:
        for i in range(0, len(message), _MSG_CHUNK):
            await obj.send(message[i : i + _MSG_CHUNK])
    except Exception as e:  # noqa: BLE001 — 권한 부족 등
        return f"[실패] 전송 오류: {e}"
    return f"#{obj.name} 채널에 메시지를 보냈습니다."


async def server_send(channel=None, message=None, **_ignored) -> str:
    """에이전트 탭 핸들러 — 승인은 에이전트 루프가 처리(자동 모드 포함 강제)."""
    a = canonical_send_args({"channel": channel, "message": message, **_ignored})
    got, err = validate_send(a["channel"], a["message"])
    if err:
        return f"[거부] {err}"
    ch_id, _ch_name, body = got
    lg, gerr = _live_guild()
    if gerr:
        return gerr
    guild, _cmd = lg
    return await send_message_live(guild, ch_id, body)


# ── 에이전트 탭 툴 핸들러(ASYNC_PLAIN) ──────────────────────────────────
async def server_map(**_ignored) -> str:
    got, err = _live_guild()
    if err:
        return err
    guild, cmd_id = got
    return render_map(snapshot_guild(guild, cmd_id))


async def server_apply(ops=None, **_ignored) -> str:
    got, err = _live_guild()
    if err:
        return err
    guild, cmd_id = got
    snap = snapshot_guild(guild, cmd_id)
    clean, skipped, error_msg = prepare_ops(ops, snap)  # 보호대상 분리 + 검증(두 입구 공유)
    if error_msg:
        return error_msg
    return (await apply_ops_live(guild, clean, snap)) + format_skipped_report(skipped)
