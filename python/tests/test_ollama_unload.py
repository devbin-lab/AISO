# -*- coding: utf-8 -*-
"""긴급 GPU 확보(/ollama/unload) 특성화 테스트.

로드된 모델을 /api/ps로 찾아 각 모델에 prompt:''+keep_alive:0을 보내 언로드한다는
계약을 고정한다. keep_alive를 0이 아닌 값으로 바꾸거나 /api/ps 조회를 빼면 여기서 잡힌다.
네트워크는 타지 않는다(runtime 어댑터의 HTTP client를 가짜로 교체).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python/ 를 import 경로에

import main  # noqa: E402
from llm.providers import ollama as ollama_provider  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
except Exception:  # pragma: no cover - httpx 미설치 환경
    TestClient = None

pytestmark = pytest.mark.skipif(TestClient is None, reason="TestClient(httpx) 미설치")


def _auth_headers() -> dict:
    # 토큰이 설정돼 있으면(다른 테스트가 먼저 심었을 수 있음) 통과 헤더를 붙인다.
    return {"X-Aiso-Token": main.AUTH_TOKEN} if main.AUTH_TOKEN else {}


class _Resp:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """httpx.AsyncClient 대역 — /api/ps는 미리 정한 모델 목록, POST는 기록만 한다."""

    posts: list[dict] = []

    def __init__(self, models: list[dict]):
        self._models = models

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url: str):
        assert url.endswith("/api/ps")
        return _Resp({"models": self._models})

    async def post(self, url: str, json=None):
        _FakeClient.posts.append({"url": url, "json": json})
        return _Resp({"done": True, "done_reason": "unload"})


def _install(monkeypatch, models: list[dict]) -> None:
    _FakeClient.posts = []
    monkeypatch.setattr(ollama_provider.httpx, "AsyncClient", lambda *a, **k: _FakeClient(models))


def test_unload_sends_keepalive_zero_per_loaded_model(monkeypatch):
    _install(monkeypatch, [{"name": "gemma4:12b"}, {"model": "gpt-oss:20b"}])
    client = TestClient(main.app)
    r = client.post("/ollama/unload", json={}, headers=_auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body["unloaded"]) == {"gemma4:12b", "gpt-oss:20b"}
    # 각 모델에 대해 정확히 prompt:''+keep_alive:0로 언로드를 요청했는지
    assert len(_FakeClient.posts) == 2
    for p in _FakeClient.posts:
        assert p["url"].endswith("/api/generate")
        assert p["json"]["keep_alive"] == 0
        assert p["json"]["prompt"] == ""
    assert {p["json"]["model"] for p in _FakeClient.posts} == {"gemma4:12b", "gpt-oss:20b"}


def test_unload_noop_when_nothing_loaded(monkeypatch):
    _install(monkeypatch, [])
    client = TestClient(main.app)
    r = client.post("/ollama/unload", json={}, headers=_auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["unloaded"] == []
    assert _FakeClient.posts == []  # 내릴 모델이 없으면 generate 호출도 없다
