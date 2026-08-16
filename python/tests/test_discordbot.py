# -*- coding: utf-8 -*-
"""Discord 봇 MVP — 인가 게이트(보안 핵심)·청킹·동적 상태 영속을 고정한다.

실제 게이트웨이 없이 순수 함수만 검증: 소유자+허용목록만, 명령 채널에서만,
스노플레이크 int/str 정규화, 소유자 미판별=fail-closed. 소유자·채널은 런타임 자동 판별이라
is_authorized에 값으로 주입해 검증한다.
"""
from __future__ import annotations

import asyncio

import discordbot

OWNER = "111"
CH = "999"
ALLOW = {"222", "333"}


# ── 인가 게이트 ──────────────────────────────────────────────────────────
def test_owner_in_command_channel_allowed():
    assert discordbot.is_authorized(OWNER, CH, ALLOW, 111, 999) is True


def test_snowflake_int_vs_str_normalized():
    assert discordbot.is_authorized("111", "999", ALLOW, 111, "999") is True
    assert discordbot.is_authorized("111", "999", ALLOW, "111", 999) is True


def test_allowlisted_user_allowed():
    assert discordbot.is_authorized(OWNER, CH, ALLOW, 222, 999) is True
    assert discordbot.is_authorized(OWNER, CH, ALLOW, 333, 999) is True


def test_non_allowlisted_denied():
    assert discordbot.is_authorized(OWNER, CH, ALLOW, 444, 999) is False


def test_wrong_channel_denied_even_owner():
    assert discordbot.is_authorized(OWNER, CH, ALLOW, 111, 12345) is False


def test_no_owner_fail_closed():
    assert discordbot.is_authorized("", CH, ALLOW, 111, 999) is False
    assert discordbot.is_authorized(None, CH, ALLOW, 111, 999) is False


def test_no_channel_denied():
    assert discordbot.is_authorized(OWNER, "", ALLOW, 111, 999) is False


def test_empty_allowlist_only_owner():
    assert discordbot.is_authorized(OWNER, CH, set(), 111, 999) is True
    assert discordbot.is_authorized(OWNER, CH, set(), 222, 999) is False


# ── 메시지 청킹 ──────────────────────────────────────────────────────────
def test_chunk_under_limit_single():
    assert discordbot.chunk_message("안녕하세요") == ["안녕하세요"]


def test_chunk_splits_at_2000():
    long = "가" * 4500
    parts = discordbot.chunk_message(long)
    assert len(parts) == 3 and all(len(p) <= 2000 for p in parts)
    assert "".join(parts) == long


def test_chunk_empty_placeholder():
    assert discordbot.chunk_message("") == ["(빈 응답)"]
    assert discordbot.chunk_message("   ") == ["(빈 응답)"]


def test_latest_news_and_usage_reset_are_routed_to_web_research():
    request = "최신 OpenAI 관련 뉴스를 알려줘. 추가로 사용량 초기화 날짜도 있으면 알려줘."

    assert discordbot.requires_web_research(request) is True
    assert discordbot._chat_route(request, can_research=True, can_use_tools=True) == "research"


def test_explicit_web_verification_is_routed_to_research():
    assert discordbot.requires_web_research("인터넷에서 공식 자료를 검색해서 확인해줘") is True
    assert discordbot.requires_web_research("Search the latest release online") is True


def test_ordinary_discord_operation_stays_in_tool_loop():
    request = "현재 서버 채널 구조를 보여줘"

    assert discordbot.requires_web_research(request) is False
    assert discordbot._chat_route(request, can_research=True, can_use_tools=True) == "tools"


def test_research_is_not_selected_when_runtime_is_unavailable():
    assert discordbot._chat_route("오늘 날씨를 알려줘", can_research=False, can_use_tools=True) == "research_unavailable"


def test_plain_confirmation_request_does_not_accidentally_trigger_web_research():
    assert discordbot.requires_web_research("현재 서버 채널 구조를 확인해줘") is False


def test_research_delivery_channel_is_extracted_from_korean_request():
    assert discordbot.requested_delivery_channel("최신 뉴스를 공지 채널에다가 알려줘") == "공지"
    assert discordbot.requested_delivery_channel("조사해서 #업데이트에 보내줘") == "업데이트"
    assert discordbot.requested_delivery_channel("최신 뉴스만 알려줘") == ""


def test_research_result_is_delivered_to_requested_channel(monkeypatch):
    seen = {}

    async def fake_research(messages, response_language):
        seen["messages"] = messages
        seen["language"] = response_language
        return "공식 원문을 확인한 최신 뉴스"

    async def fake_send(channel, args):
        seen["channel"] = channel
        seen["args"] = args
        return "#공지 채널에 메시지를 보냈습니다."

    monkeypatch.setattr(discordbot._S, "research", fake_research)
    monkeypatch.setattr(discordbot, "_send_with_approval", fake_send)
    result = asyncio.run(discordbot._research_chat(
        object(),
        "최신 OpenAI 뉴스를 공지 채널에다가 알려줘",
        [{"role": "system", "content": "Discord 도구 지침"}, {"role": "user", "content": "요청"}],
    ))

    assert seen["messages"] == [{"role": "user", "content": "요청"}]
    assert seen["language"] == "ko"
    assert seen["args"] == {"channel": "공지", "message": "공식 원문을 확인한 최신 뉴스"}
    assert "#공지 채널에 메시지를 보냈습니다" in result


def test_discord_model_prompts_are_english_but_final_language_is_request_scoped():
    korean = discordbot._tools_prompt(False, "ko")
    english = discordbot._tools_prompt(False, "en")

    assert "Discord server tools are available" in korean
    assert "final answer in Korean" in korean
    assert "final answer in English" in english
    assert korean != english


def test_scheduled_model_prompts_keep_policy_english_and_localize_final_output_contract():
    briefing = discordbot.briefing_system("en")
    report = discordbot.channel_report_system("ko")

    assert "Write a briefing" in briefing and "final answer in English" in briefing
    assert "summarizing only newly collected" in report and "final answer in Korean" in report


def test_image_tool_is_exposed_only_when_a_comfy_handler_is_ready(monkeypatch):
    seen = []

    async def fake_step(_messages, tools):
        seen.append([tool["function"]["name"] for tool in tools])
        return {"content": "확인", "tool_calls": []}

    async def fake_image(_args):
        return {"data": b"png", "filename": "image.png", "summary": "ok"}

    monkeypatch.setattr(discordbot._S, "step", fake_step)
    monkeypatch.setattr(discordbot._S, "image", None)
    assert asyncio.run(discordbot._tool_chat(object(), OWNER, [{"role": "user", "content": "그림"}])) == "확인"
    monkeypatch.setattr(discordbot._S, "image", fake_image)
    result = asyncio.run(discordbot._tool_chat(object(), OWNER, [{"role": "user", "content": "그림"}]))

    assert result == "확인"
    assert "generate_image" not in seen[0]
    assert "generate_image" in seen[1]


def test_image_tool_sends_validated_comfy_output_to_discord(monkeypatch):
    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, content, **kwargs):
            self.sent.append((content, kwargs))

    async def fake_image(args):
        assert args == {"prompt": "rainy city"}
        return {"data": b"png-data", "filename": "result.png", "summary": "이미지 생성 완료"}

    monkeypatch.setattr(discordbot._S, "image", fake_image)
    channel = Channel()
    result = asyncio.run(discordbot._run_bot_tool(
        channel, OWNER, "generate_image", {"prompt": "rainy city"}
    ))

    assert "전송했습니다" in result
    assert channel.sent[0][0].startswith("🖼️")
    assert channel.sent[0][1]["file"].filename == "result.png"


# ── 동적 상태 영속(guild·channel·allowlist) ─────────────────────────────
def test_state_save_load_roundtrip(tmp_path):
    discordbot._S.data_dir = str(tmp_path)
    discordbot._S.guild_id = "555"
    discordbot._S.channel_id = "666"
    discordbot._S.allowlist = {"222", "333"}
    discordbot._save_state()

    # 상태를 비우고 다시 로드하면 복원돼야 한다
    discordbot._S.guild_id = ""
    discordbot._S.channel_id = ""
    discordbot._S.allowlist = set()
    discordbot._load_state()
    assert discordbot._S.guild_id == "555"
    assert discordbot._S.channel_id == "666"
    assert discordbot._S.allowlist == {"222", "333"}


def test_state_load_missing_is_noop(tmp_path):
    discordbot._S.data_dir = str(tmp_path)  # state.json 없음
    discordbot._S.allowlist = {"keep"}
    discordbot._load_state()  # 파일 없으면 그대로 둠(무해)
    assert discordbot._S.allowlist == {"keep"}
