# -*- coding: utf-8 -*-
"""에이전트 하네스 특성화 테스트용 공통 픽스처.

run_agent의 이벤트 스트림을 mock된 _chat_turn으로 구동해 검증한다.
새 런타임 의존성을 피하려고 pytest-asyncio 대신 asyncio.run 기반 drain을 쓴다.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python/ 를 import 경로에

import agent  # noqa: E402
from ollama_util import OllamaHTTPError  # noqa: E402  (테스트에서 재수출)

__all__ = ["OllamaHTTPError", "FakeChat", "parse_error", "load_crash", "types"]


def parse_error() -> OllamaHTTPError:
    """gpt-oss 툴콜 파싱 500 오류(재생성으로 회복 가능)를 흉내낸다."""
    return OllamaHTTPError(500, "error parsing tool call: raw='We need to respond: ...'")


def load_crash() -> OllamaHTTPError:
    """VRAM 부족 등 모델 로드 크래시(오프로드 사다리로 회복 가능)를 흉내낸다."""
    return OllamaHTTPError(500, "cudaMalloc failed: out of memory")


class FakeChat:
    """_chat_turn 대체. 턴 스펙 리스트로 스크립트한다.

    스펙(dict): content=str, thinking=str, calls=[(name, args_dict), ...],
                done_reason=str, raise=Exception|None
    스크립트를 다 쓰면 마지막 스펙을 반복한다. 각 호출의 payload를 self.payloads에 기록.
    """

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.calls = 0
        self.payloads: list[dict] = []

    def __call__(self, host, payload):
        self.payloads.append(payload)
        spec = self.script[self.calls] if self.calls < len(self.script) else self.script[-1]
        self.calls += 1
        return self._gen(spec)

    async def _gen(self, spec):
        if spec.get("raise") is not None:
            raise spec["raise"]
        if spec.get("thinking"):
            yield {"type": "thinking", "text": spec["thinking"]}
        if spec.get("content"):
            yield {"type": "content", "text": spec["content"]}
        tool_calls = [
            {"function": {"name": name, "arguments": args}}
            for name, args in spec.get("calls", [])
        ]
        yield {
            "_final": True,
            "content": spec.get("content", ""),
            "thinking": spec.get("thinking", ""),
            "tool_calls": tool_calls,
            "done_reason": spec.get("done_reason", "stop"),
        }


def types(events: list[dict]) -> list[str]:
    return [e.get("type", "?") for e in events]


async def _drive(agen, on_approval=None) -> list[dict]:
    events: list[dict] = []
    ait = agen.__aiter__()
    while True:
        try:
            ev = await ait.__anext__()
        except StopAsyncIteration:
            break
        events.append(ev)
        if ev.get("type") == "approval_request" and on_approval is not None:
            on_approval(ev)
    return events


class _Env:
    """테스트 편의 객체 — run/drive + 상태 접근."""

    SESSION = "s"

    def __init__(self, monkeypatch, workspace: Path):
        self.mp = monkeypatch
        self.ws = workspace
        self.reindex_calls: list[str] = []

    def run(self, fake_chat: FakeChat, **kwargs) -> list[dict]:
        """승인 없이 끝까지 구동(자동 drain)."""
        self.mp.setattr(agent, "_chat_turn", fake_chat)
        return asyncio.run(_drive(self._agen(fake_chat, **kwargs)))

    def drive(self, fake_chat: FakeChat, approve: bool, **kwargs) -> list[dict]:
        """approval_request가 오면 resolve_approval을 호출하며 구동."""
        self.mp.setattr(agent, "_chat_turn", fake_chat)

        def on_appr(ev):
            agent.resolve_approval(f"{self.SESSION}:{ev['id']}", approve)

        return asyncio.run(_drive(self._agen(fake_chat, **kwargs), on_appr))

    def _agen(self, fake_chat, **kwargs):
        kw = dict(
            host="h",
            workspace=str(self.ws),
            model="m",
            messages=[{"role": "user", "content": "go"}],
            session_id=self.SESSION,
        )
        kw.update(kwargs)
        return agent.run_agent(**kw)


@pytest.fixture
def env(monkeypatch):
    """기본 환경: 네트워크·RAG·재색인 무력화. 실제 파일 툴은 임시 워크스페이스에서 진짜 실행."""
    ws = Path(tempfile.mkdtemp()).resolve()

    async def fake_layers(host, model):
        return None

    monkeypatch.setattr(agent, "model_layers", fake_layers)
    monkeypatch.setattr(agent, "rag_status", lambda root: {})  # 색인 없음 → RAG 비활성

    e = _Env(monkeypatch, ws)
    monkeypatch.setattr(agent, "_fire_reindex", lambda root, host: e.reindex_calls.append(str(root)))
    return e
