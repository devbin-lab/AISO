# -*- coding: utf-8 -*-
"""적대적 리뷰에서 확정된 디스코드 결함들의 회귀 방지 테스트.

- 툴 미지원 모델 폴백(#6): _discord_step이 400('does not support tools')이면 tools 없이 재시도.
- 명령 채널 잠금 재적용(#2): 이름으로 기존 채널을 채택할 때 잠금 overwrite를 다시 적용.
- 소유자 fetch 폴백(#3): members 인텐트 없이도 fetch_member로 소유자 권한을 부여.
- gen_lock 범위(#4): _tool_chat이 생성 턴에만 락을 잡고 승인 대기 중에는 놓는다.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python/ 를 import 경로에

import discordbot  # noqa: E402
import main  # noqa: E402
from ollama_util import OllamaHTTPError  # noqa: E402


# ── #6: 툴 미지원 모델 → tools 없이 폴백 ────────────────────────────────
def test_discord_step_falls_back_when_tools_unsupported(monkeypatch):
    seen_tools: list = []

    async def fake_layers(host, model):
        return None

    async def fake_stream(host, payload):
        seen_tools.append("tools" in payload)
        if "tools" in payload:  # 도구를 붙인 첫 시도 → 모델이 도구 미지원(400)
            raise OllamaHTTPError(400, '{"error":"model does not support tools"}')
        yield {"type": "content", "text": "안녕하세요"}  # 도구 없이 재시도 → 성공

    monkeypatch.setattr(main, "model_layers", fake_layers)
    monkeypatch.setattr(main, "_stream_ollama", fake_stream)

    out = asyncio.run(
        main._discord_step(
            [{"role": "user", "content": "안녕"}],
            model="m", context_length=4096, keep_alive="5m", host="h",
            tools=[{"type": "function", "function": {"name": "discord_server_map"}}],
        )
    )
    assert out["content"] == "안녕하세요" and out["tool_calls"] == []
    assert seen_tools == [True, False]  # 도구 붙인 시도 실패 → 도구 없이 재시도


def test_discord_research_discards_reset_content(monkeypatch):
    """브리핑 생성이 reset_content(폐기 신호) 이후 누적분을 버려, 폐기 초안이 자율 전송되지 않는다."""
    async def fake_research(**_kw):
        for ev in [
            {"type": "content", "text": "먼저 검색하겠습니다"},   # 서두
            {"type": "content", "text": "임시 초안(스니펫만 봄)"},  # 폐기 대상
            {"type": "reset_content"},                              # ← 여기서 위 누적 폐기
            {"type": "content", "text": "실제 최종 브리핑"},
        ]:
            yield ev

    monkeypatch.setattr(main, "run_research_chat", fake_research)
    out = asyncio.run(
        main._discord_research(
            [{"role": "user", "content": "날씨"}],
            model="m", context_length=4096, keep_alive="30m", host="h",
        )
    )
    assert out == "실제 최종 브리핑"  # 서두·폐기초안이 섞이지 않음


def test_discord_step_other_400_does_not_loop(monkeypatch):
    """tools 미지원이 아닌 다른 400은 폴백하지 않고 오류를 반환한다(무한 재귀 방지)."""
    async def fake_layers(host, model):
        return None

    async def fake_stream(host, payload):
        raise OllamaHTTPError(400, '{"error":"invalid options"}')
        yield  # pragma: no cover — async generator 표식

    monkeypatch.setattr(main, "model_layers", fake_layers)
    monkeypatch.setattr(main, "_stream_ollama", fake_stream)
    out = asyncio.run(
        main._discord_step(
            [{"role": "user", "content": "x"}],
            model="m", context_length=4096, keep_alive="5m", host="h",
            tools=[{"type": "function", "function": {"name": "discord_server_map"}}],
        )
    )
    assert "모델 오류 400" in out["content"]


# ── #4: _tool_chat은 승인 대기 중 gen_lock을 쥐지 않는다 ────────────────
def test_tool_chat_releases_lock_during_tool_execution(monkeypatch):
    """생성 턴 사이(도구 실행·승인 대기)에는 gen_lock이 풀려 있어야 한다."""
    discordbot._S.gen_lock = asyncio.Lock()
    lock_held_during_tool = {"v": None}

    async def fake_step(convo, tools):
        # 생성 중에는 락이 잡혀 있어야 한다
        assert discordbot._S.gen_lock.locked()
        if any(m.get("role") == "tool" for m in convo):
            return {"content": "완료", "tool_calls": []}  # 도구 결과 받은 뒤 → 종료
        return {"content": "", "tool_calls": [{"function": {"name": "discord_server_map", "arguments": {}}}]}

    async def fake_run_tool(channel, author_id, name, args):
        # 도구 실행(=승인 대기가 일어나는 지점) 동안 락이 풀려 있어야 한다
        lock_held_during_tool["v"] = discordbot._S.gen_lock.locked()
        return "맵 결과"

    _S = discordbot._S
    _S.step = fake_step
    monkeypatch.setattr(discordbot, "_run_bot_tool", fake_run_tool)
    reply = asyncio.run(
        discordbot._tool_chat(channel=None, author_id="1", convo=[{"role": "user", "content": "구조 보여줘"}])
    )
    assert reply == "완료"
    assert lock_held_during_tool["v"] is False  # 도구 실행 중 락 해제됨


# ── #2 검증 로직 보조: _lock_overwrites가 소유자 fetch 폴백을 쓴다(#3) ──
def test_lock_overwrites_uses_fetch_when_cache_empty(monkeypatch):
    """get_member가 None(members 인텐트 꺼짐)이어도 fetch_member로 소유자 권한을 넣는다."""
    import discord

    fetched = {"called": False}

    class FakeMember:
        def __init__(self, mid):
            self.id = mid

    class FakeGuild:
        def __init__(self):
            self.default_role = "everyone"
            self.me = "botself"
            self.owner = None
            self.owner_id = 999

        def get_member(self, uid):
            return None  # 캐시 비어 있음(members 인텐트 꺼짐 상황)

        async def fetch_member(self, uid):
            fetched["called"] = True
            return FakeMember(uid)

    discordbot._S.owner_id = "12345"
    ow = asyncio.run(discordbot._lock_overwrites(FakeGuild()))
    assert fetched["called"] is True
    # @everyone은 숨김, 소유자 멤버(fetch로 얻은)와 봇·서버주인이 열람 키로 들어간다
    keys = {k.id if isinstance(k, FakeMember) else k for k in ow.keys()}
    assert "everyone" in keys and "botself" in keys and 12345 in keys and 999 in keys
    assert ow["everyone"].view_channel is False
