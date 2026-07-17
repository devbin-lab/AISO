# -*- coding: utf-8 -*-
"""디스코드 서버 구성(discordops) 특성화 테스트.

- validate_ops: 한도·화이트리스트·#aiso 보호·이름 해석(ID/이름/모호성)·배치 내 카테고리 선참조.
- render: 미리보기의 삭제(복구 불가) 강조, 서버 맵의 보호 마크.
- 에이전트 통합: 조건부 노출(봇 연결시에만), 무폴더 허용, auto 모드에서도 apply 승인 강제.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python/ 를 import 경로에

import agent  # noqa: E402
import discordops as ops  # noqa: E402
from conftest import FakeChat, types  # noqa: E402


def _snap() -> dict:
    """테스트용 서버 스냅샷 — #aiso(명령 채널)와 팀 구조 일부."""
    return {
        "guild_name": "팀서버",
        "command_channel_id": "100",
        "categories": [{"id": "10", "name": "기획", "type": "category"}],
        "channels": [
            {"id": "100", "name": "aiso", "type": "text", "category_id": ""},
            {"id": "101", "name": "일반", "type": "text", "category_id": ""},
            {"id": "102", "name": "기획-소통", "type": "text", "category_id": "10"},
            {"id": "103", "name": "회의실", "type": "voice", "category_id": "10"},
            {"id": "104", "name": "중복", "type": "text", "category_id": ""},
            {"id": "105", "name": "중복", "type": "text", "category_id": "10"},
        ],
    }


# ── validate_ops: 기본 형태 ────────────────────────────────────────────
def test_validate_rejects_non_list_and_empty():
    for bad in (None, {}, "x", []):
        clean, errs = ops.validate_ops(bad, _snap())
        assert clean == [] and errs


def test_validate_rejects_too_many_ops():
    many = [{"action": "create_category", "name": f"c{i}"} for i in range(ops.MAX_OPS + 1)]
    clean, errs = ops.validate_ops(many, _snap())
    assert clean == [] and any("최대" in e for e in errs)


def test_validate_rejects_unknown_action():
    clean, errs = ops.validate_ops([{"action": "nuke_server"}], _snap())
    assert clean == [] and any("알 수 없는 작업" in e for e in errs)


# ── 생성: 카테고리 선참조·중복 ─────────────────────────────────────────
def test_create_channel_in_same_batch_category():
    """같은 배치에서 먼저 만든 카테고리를 뒤의 채널 생성이 바로 참조할 수 있다."""
    batch = [
        {"action": "create_category", "name": "그래픽"},
        {"action": "create_text_channel", "name": "그래픽-소통", "category": "그래픽"},
        {"action": "create_voice_channel", "name": "그래픽 회의", "category": "그래픽"},
    ]
    clean, errs = ops.validate_ops(batch, _snap())
    assert errs == [] and len(clean) == 3


def test_create_channel_unknown_category_rejected():
    clean, errs = ops.validate_ops(
        [{"action": "create_text_channel", "name": "x", "category": "없는카테고리"}], _snap()
    )
    assert clean == [] and any("없습니다" in e for e in errs)


def test_create_duplicate_category_rejected():
    clean, errs = ops.validate_ops([{"action": "create_category", "name": "기획"}], _snap())
    assert clean == [] and any("이미 있습니다" in e for e in errs)


def test_topic_only_on_text_channel():
    clean, errs = ops.validate_ops([{"action": "set_topic", "target": "회의실", "topic": "t"}], _snap())
    assert clean == [] and any("텍스트 채널" in e for e in errs)


# ── 대상 해석: 이름·ID·모호성 ──────────────────────────────────────────
def test_rename_resolves_name_to_id():
    clean, errs = ops.validate_ops([{"action": "rename", "target": "일반", "new_name": "잡담"}], _snap())
    assert errs == [] and clean[0]["target"] == "101"


def test_find_strips_hash_and_space():
    """모델이 '#일반' 또는 '# 일반'(# 뒤 공백)으로 줘도 해석된다(실측: gemma가 '# 공지' 형태로 냄)."""
    for tgt in ("#일반", "# 일반", " 일반 "):
        item, err = ops._find(_snap(), tgt)
        assert err is None and item["id"] == "101", tgt


def test_ambiguous_name_requires_id():
    clean, errs = ops.validate_ops([{"action": "delete", "target": "중복"}], _snap())
    assert clean == [] and any("여러 개" in e for e in errs)
    # ID로 지정하면 통과
    clean, errs = ops.validate_ops([{"action": "delete", "target": "105"}], _snap())
    assert errs == [] and clean[0]["target"] == "105"


def test_move_category_rejected_channel_ok():
    clean, errs = ops.validate_ops([{"action": "move", "target": "기획", "category": ""}], _snap())
    assert clean == [] and any("카테고리는 이동" in e for e in errs)
    clean, errs = ops.validate_ops([{"action": "move", "target": "일반", "category": "기획"}], _snap())
    assert errs == [] and clean[0] == {"action": "move", "target": "101", "category": "기획"}


# ── #aiso(명령 채널) 보호 — 이름으로도 ID로도 못 건드린다 ─────────────
def test_command_channel_protected_from_all_ops():
    for op in (
        {"action": "delete", "target": "aiso"},
        {"action": "delete", "target": "100"},
        {"action": "rename", "target": "aiso", "new_name": "x"},
        {"action": "move", "target": "100", "category": "기획"},
        {"action": "set_topic", "target": "aiso", "topic": "t"},
    ):
        clean, errs = ops.validate_ops([op], _snap())
        assert clean == [] and any("보호" in e for e in errs), op


# ── 약한 모델 필드명 편차 정규화(_canonical_op) — 실측 gpt-oss/gemma 출력으로 고정 ──
def _empty_snap() -> dict:
    return {"guild_name": "s", "command_channel_id": "100",
            "categories": [], "channels": [{"id": "100", "name": "aiso", "type": "text", "category_id": ""}]}


def test_normalize_gptoss_op_field_and_parent():
    """gpt-oss 실측 형태: action 대신 'op', category 대신 'parent'."""
    batch = [
        {"op": "create_category", "name": "게임 개발"},
        {"op": "create_channel", "parent": "게임 개발", "name": "코드", "type": "text"},
        {"op": "create_channel", "parent": "게임 개발", "name": "개발팀 회의", "type": "voice"},
    ]
    clean, errs = ops.validate_ops(batch, _empty_snap())
    assert errs == [] and len(clean) == 3
    assert clean[1] == {"action": "create_text_channel", "name": "코드", "category": "게임 개발"}
    assert clean[2]["action"] == "create_voice_channel"


def test_normalize_gptoss_type_as_action_and_category_name():
    """gpt-oss 실측 다른 형태: action 역할을 'type'이, category를 'category_name'이 맡음."""
    batch = [
        {"type": "create_category", "name": "기획"},
        {"type": "create_text_channel", "category_name": "기획", "name": "스케치"},
    ]
    clean, errs = ops.validate_ops(batch, _empty_snap())
    assert errs == [] and len(clean) == 2
    assert clean[0]["action"] == "create_category"
    assert clean[1] == {"action": "create_text_channel", "name": "스케치", "category": "기획"}


def test_normalize_real_gptoss_full_batch():
    """실측 gpt-oss 전체 배치(action+parent 혼합)가 정규화로 전부 통과한다."""
    batch = [
        {"action": "create_category", "name": "게임 개발"},
        {"action": "create_text_channel", "name": "코드", "parent": "게임 개발"},
        {"action": "create_voice_channel", "name": "개발팀 회의", "parent": "게임 개발"},
        {"action": "create_category", "name": "기획·디자인"},
        {"action": "create_text_channel", "name": "스케치", "parent": "기획·디자인"},
    ]
    clean, errs = ops.validate_ops(batch, _empty_snap())
    assert errs == [] and len(clean) == 5
    assert all(c.get("category") for c in clean if c["action"].startswith("create_text") or c["action"].startswith("create_voice"))


def test_normalize_rename_synonyms():
    snap = _empty_snap()
    snap["channels"].append({"id": "101", "name": "일반", "type": "text", "category_id": ""})
    clean, errs = ops.validate_ops([{"op": "rename", "channel": "일반", "to": "잡담"}], snap)
    assert errs == [] and clean[0] == {"action": "rename", "target": "101", "new_name": "잡담"}


def test_design_guide_wired_into_both_prompts():
    """전문 팀 서버 설계 기준(DESIGN_GUIDE)이 두 입구의 시스템 프롬프트에 들어간다.

    실측: 기준 없인 12B가 카테고리 2개짜리 얇은 구조를 냄 → 기준 추가 후 gemma 8/gpt-oss 7카테고리,
    topic 15/15, 운영·자료실·QA·회의 전부 포함으로 확인."""
    import discordbot

    assert "서버 설계 기준" in ops.DESIGN_GUIDE and "자료실" in ops.DESIGN_GUIDE
    assert ops.DESIGN_GUIDE in discordbot._tools_prompt()  # 디스코드 입구


def test_design_guide_in_agent_prompt_when_bot_online(env, monkeypatch):
    monkeypatch.setattr(agent.discordops, "available", lambda: True)
    fc = FakeChat([{"content": "완료"}])
    env.run(fc, approval_mode="auto")
    assert "서버 설계 기준" in fc.payloads[0]["messages"][0]["content"]  # 에이전트 입구


def test_format_hint_covers_correct_field_names():
    """정규화로도 못 살리는 이상한 op을 위해, 모델 교정용 형식 힌트에 표준 필드명이 들어 있다."""
    h = ops.FORMAT_HINT
    for token in ("action", "create_text_channel", "category", "new_name", "target"):
        assert token in h


def test_command_channel_name_protected_from_reuse():
    """재연결 시 봇이 이름으로 명령 채널을 재채택하므로, 그 이름을 새로 만들거나 그 이름으로
    바꾸는 것도 막는다(보호 대상 혼동 방지)."""
    for op in (
        {"action": "create_text_channel", "name": "aiso"},
        {"action": "create_category", "name": "aiso"},
        {"action": "rename", "target": "일반", "new_name": "aiso"},
    ):
        clean, errs = ops.validate_ops([op], _snap())
        assert clean == [] and any("명령 채널 이름" in e for e in errs), op


def test_one_bad_op_rejects_whole_batch():
    """부분 적용 방지 — 하나라도 오류면 전체 배치를 거부한다(clean 비움)."""
    batch = [
        {"action": "create_category", "name": "QA"},
        {"action": "delete", "target": "aiso"},  # 보호 위반
    ]
    clean, errs = ops.validate_ops(batch, _snap())
    assert clean == [] and errs


# ── 렌더링 ─────────────────────────────────────────────────────────────
def test_preview_shows_set_topic_new_text():
    """승인 미리보기는 set_topic의 새 주제 내용을 반드시 노출한다(소유자가 보고 승인하도록)."""
    snap = _snap()
    clean, errs = ops.validate_ops(
        [{"action": "set_topic", "target": "일반", "topic": "공지 전용 채널입니다"}], snap
    )
    assert errs == []
    text = ops.render_ops_preview(clean, snap)
    assert "공지 전용 채널입니다" in text  # 새 주제가 미리보기에 보인다


def test_package_bundle_keeps_busybox():
    """[회귀 가드] 패키징 필터가 busybox.exe를 제외하면 안 된다 — grep/sed/sh 등 unix 유틸이
    전부 busybox 포워더라 제외 시 프로덕션에서 run_command가 전멸한다(과거 SEV-5 회귀)."""
    import json as _json

    pkg = Path(__file__).resolve().parent.parent.parent / "package.json"
    data = _json.loads(pkg.read_text(encoding="utf-8"))
    tools_entry = next(
        e for e in data["build"]["extraResources"] if e.get("from") == "tools"
    )
    joined = " ".join(tools_entry.get("filter", []))
    assert "busybox.exe" not in joined, "busybox.exe를 번들에서 제외하면 grep/sed/sh가 깨진다"


def test_preview_emphasizes_irreversible_delete():
    clean, errs = ops.validate_ops(
        [{"action": "delete", "target": "일반"}, {"action": "create_category", "name": "QA"}], _snap()
    )
    assert errs == []
    text = ops.render_ops_preview(clean, _snap())
    assert "복구 불가" in text and "삭제 1건은 되돌릴 수 없습니다" in text
    assert "카테고리 생성: QA" in text


def test_render_map_marks_protected_channel():
    text = ops.render_map(_snap())
    assert "★명령채널(보호됨" in text and "기획" in text and "카테고리 1개, 채널 6개" in text


# ── split_protected: "전부 삭제" 시나리오 — 보호 대상만 빼고 배치는 살린다 ──
def test_split_protected_keeps_batch_alive_on_delete_all():
    """사용자 실사례: '전부 삭제해줘'에 모델이 #aiso까지 포함 → 배치 전체 거부 대신
    #aiso만 제외하고 나머지는 진행, 제외 사실은 노트로 남긴다."""
    batch = [
        {"action": "delete", "target": "기획"},
        {"action": "delete", "target": "일반"},
        {"action": "delete", "target": "aiso"},       # 보호 — 이것 때문에 전체가 죽으면 안 됨
        {"action": "delete", "target": "102"},
    ]
    kept, skipped = ops.split_protected(batch, _snap())
    assert len(kept) == 3 and len(skipped) == 1
    assert "aiso" in skipped[0] and "보호" in skipped[0]
    clean, errs = ops.validate_ops(kept, _snap())   # 남은 배치는 그대로 통과
    assert errs == [] and len(clean) == 3


def test_split_protected_all_protected_returns_empty():
    kept, skipped = ops.split_protected([{"action": "delete", "target": "100"}], _snap())
    assert kept == [] and len(skipped) == 1


def test_split_protected_handles_field_variants_and_name_reuse():
    """필드명 편차(op/channel)로 보호 채널을 노려도, 보호 이름 재사용도 걸러진다."""
    batch = [
        {"op": "delete", "channel": "aiso"},                     # 변형 필드로 명령 채널 삭제 시도
        {"op": "rename", "channel": "일반", "to": "aiso"},        # 보호 이름으로 개명 시도
        {"action": "create_text_channel", "name": "aiso"},       # 보호 이름 생성 시도
        {"action": "delete", "target": "일반"},                   # 정상 — 남아야 함
    ]
    kept, skipped = ops.split_protected(batch, _snap())
    assert len(kept) == 1 and kept[0]["action"] == "delete"
    assert len(skipped) == 3


def test_split_protected_passthrough_without_command_channel():
    snap = _snap()
    snap["command_channel_id"] = ""
    batch = [{"action": "delete", "target": "일반"}]
    kept, skipped = ops.split_protected(batch, snap)
    assert kept == batch and skipped == []


# ── 에이전트 통합 ──────────────────────────────────────────────────────
def test_registry_and_workspace_free_registration():
    """REGISTRY 등록 + 무폴더 허용 + AGENT_TOOLS(스냅샷) 불변을 특성화."""
    from toolspec import AGENT_TOOLS, REGISTRY

    assert "discord_server_map" in REGISTRY and "discord_server_apply" in REGISTRY
    assert {"discord_server_map", "discord_server_apply"} <= agent.WORKSPACE_FREE_TOOLS
    exposed = {t["function"]["name"] for t in AGENT_TOOLS}
    assert "discord_server_map" not in exposed and "discord_server_apply" not in exposed
    # 등급: map=SAFE(read 통과), apply=DELETE(read 승인)
    assert agent.needs_approval("discord_server_map", "read") is False
    assert agent.needs_approval("discord_server_apply", "read") is True


def test_tools_hidden_when_bot_offline(env):
    """봇이 꺼져 있으면(available=False) 도구도 프롬프트 힌트도 노출되지 않는다."""
    fc = FakeChat([{"content": "완료"}])
    env.run(fc, approval_mode="auto")  # 테스트 환경: 봇 미실행 → available()=False
    tool_names = {t["function"]["name"] for t in fc.payloads[0]["tools"]}
    assert "discord_server_apply" not in tool_names
    sys_text = fc.payloads[0]["messages"][0]["content"]
    assert "디스코드 서버 구성" not in sys_text


def test_tools_exposed_when_bot_online(env, monkeypatch):
    fc = FakeChat([{"content": "완료"}])
    monkeypatch.setattr(agent.discordops, "available", lambda: True)
    env.run(fc, approval_mode="auto")
    tool_names = {t["function"]["name"] for t in fc.payloads[0]["tools"]}
    assert {"discord_server_map", "discord_server_apply"} <= tool_names
    assert "디스코드 서버 구성" in fc.payloads[0]["messages"][0]["content"]


def test_apply_forces_approval_even_in_auto_mode(env, monkeypatch):
    """서버 변경(apply)은 자동 모드에서도 반드시 승인 — 거부하면 실행되지 않는다."""
    monkeypatch.setattr(agent.discordops, "available", lambda: True)
    fc = FakeChat([
        {"calls": [("discord_server_apply", {"ops": [{"action": "create_category", "name": "QA"}]})]},
        {"content": "완료"},
    ])
    evs = env.drive(fc, approve=False, approval_mode="auto")
    t = types(evs)
    assert "approval_request" in t  # auto인데도 승인 요청이 떴다
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["ok"] is False and tr.get("rejected") is True


def test_apply_approved_in_auto_runs_handler(env, monkeypatch):
    """승인하면 핸들러 실행 — 봇 미연결 환경에선 친절한 [불가] 안내가 결과로 온다."""
    monkeypatch.setattr(agent.discordops, "available", lambda: True)
    fc = FakeChat([
        {"calls": [("discord_server_apply", {"ops": [{"action": "create_category", "name": "QA"}]})]},
        {"content": "완료"},
    ])
    evs = env.drive(fc, approve=True, approval_mode="auto")
    tr = next(e for e in evs if e["type"] == "tool_result")
    assert tr["ok"] is True and "[불가]" in tr["output"]  # 봇 미실행 → 실행은 되되 불가 안내


def test_available_false_without_running_bot():
    assert ops.available() is False
