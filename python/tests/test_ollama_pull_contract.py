# -*- coding: utf-8 -*-
"""/ollama/pull 완료 판정 계약.

Ollama의 `/api/pull`은 **오류가 나도 HTTP 200을 준다.** 그래서 상태 코드로는 성공을
구분할 수 없고, 스트림 안의 `{"status": "success"}` 표식이 유일한 완료 근거다.

예전에는 success 없이 스트림이 끝나도 `done`으로 보고했다. 그러면 내려받기가 중간에
끊겨도 사용자는 설치가 끝난 줄 알고 넘어가고, 정작 채팅에서 "모델 없음"으로 실패한다.
첫 설치 온보딩(임베딩 모델 원클릭 pull)이 정확히 이 경로를 탄다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402

try:
    from fastapi.testclient import TestClient
except Exception:  # pragma: no cover - 의존성 없는 환경
    TestClient = None

pytestmark = pytest.mark.skipif(TestClient is None, reason="TestClient(httpx) 미설치")

TOKEN = "t" * 32


@pytest.fixture
def client(monkeypatch):
    auth_middleware = next(
        middleware
        for middleware in main.app.user_middleware
        if middleware.cls is main.TokenAuthMiddleware
    )
    monkeypatch.setattr(main, "AUTH_TOKEN", TOKEN)
    monkeypatch.setitem(auth_middleware.kwargs, "token", TOKEN)
    main.app.middleware_stack = None
    try:
        yield TestClient(main.app)
    finally:
        main.app.middleware_stack = None


class _FakeStream:
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


def _install_fake_ollama(monkeypatch, lines, status_code=200):
    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, _method, _url, **_kwargs):
            return _FakeStream(lines, status_code)

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)


def _events(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _pull(client) -> list[dict]:
    response = client.post(
        "/ollama/pull",
        headers={"X-Aiso-Token": TOKEN},
        json={"model": "bge-m3", "ollama_host": "http://127.0.0.1:11434"},
    )
    assert response.status_code == 200
    return _events(response)


def test_success_marker_reports_done(client, monkeypatch):
    _install_fake_ollama(monkeypatch, [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "downloading", "total": 100, "completed": 100}),
        json.dumps({"status": "success"}),
    ])
    events = _pull(client)
    assert events[-1]["type"] == "done"
    assert events[-1]["model"] == "bge-m3"


def test_stream_ending_without_success_is_an_error(client, monkeypatch):
    """중간에 끊긴 내려받기를 완료로 보고하지 않는다."""
    _install_fake_ollama(monkeypatch, [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "downloading", "total": 1000, "completed": 120}),
        # success 없이 끝
    ])
    events = _pull(client)
    assert events[-1]["type"] == "error", events
    assert "done" not in {event["type"] for event in events}


def test_in_stream_error_still_reports_error(client, monkeypatch):
    """Ollama가 200 OK로 주는 스트림 내부 오류는 그대로 오류다(기존 계약 유지)."""
    _install_fake_ollama(monkeypatch, [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"error": "model not found"}),
    ])
    events = _pull(client)
    assert events[-1]["type"] == "error"
    assert "model not found" in events[-1]["error"]


def test_empty_stream_is_an_error(client, monkeypatch):
    """아무것도 못 받은 경우도 완료가 아니다."""
    _install_fake_ollama(monkeypatch, [])
    events = _pull(client)
    assert events[-1]["type"] == "error"
