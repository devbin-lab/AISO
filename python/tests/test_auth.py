# -*- coding: utf-8 -*-
"""사이드카 인증(X-Aiso-Token) + CORS 잠금 특성화 테스트.

악성 웹페이지가 동적 포트를 스캔해 cross-origin으로 /agent(파일 툴)·/chat을 호출하지
못하도록 하는 보안 속성을 고정한다. allow_origins=["*"]로 되돌리거나 미들웨어를 빼면
여기서 회귀가 잡힌다.

main.py는 import 시점에 AISO_AUTH_TOKEN을 읽으므로, main import보다 먼저 토큰을 심는다.
모든 단언은 네트워크를 타지 않는다(401은 핸들러 진입 전, /preview는 잘못된 경로라 즉시 반환).
"""
from __future__ import annotations

import os
import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python/ 를 import 경로에

TOKEN = "test-token-abc123"
os.environ["AISO_AUTH_TOKEN"] = TOKEN

import main  # noqa: E402  (토큰 심은 뒤 import — 미들웨어가 이 토큰으로 잠긴다)
import comfy_client  # noqa: E402

try:
    from fastapi.testclient import TestClient  # noqa: E402
except Exception:  # pragma: no cover - httpx 미설치 환경
    TestClient = None

pytestmark = pytest.mark.skipif(TestClient is None, reason="TestClient(httpx) 미설치")

BOGUS_WS = "C:/definitely/not/a/real/workspace"


@pytest.fixture
def client():
    return TestClient(main.app)


# ── 토큰 없는 민감 엔드포인트는 잠긴다 ─────────────────────────
def test_agent_without_token_is_401(client):
    r = client.post("/agent", json={"messages": [], "workspace": BOGUS_WS})
    assert r.status_code == 401


def test_preview_without_token_is_401(client):
    r = client.post("/preview", json={"workspace": BOGUS_WS})
    assert r.status_code == 401


def test_comfy_health_without_token_is_401(client):
    r = client.get("/comfy/health")
    assert r.status_code == 401


def test_comfy_checkpoints_without_token_is_401(client):
    r = client.get("/comfy/checkpoints")
    assert r.status_code == 401


def test_comfy_image_without_token_is_401(client):
    r = client.get("/comfy/image", params={"filename": "result.png"})
    assert r.status_code == 401


def test_wrong_token_is_401(client):
    r = client.post("/preview", headers={"X-Aiso-Token": "nope"}, json={"workspace": BOGUS_WS})
    assert r.status_code == 401


# ── 올바른 토큰은 핸들러까지 통과한다(네트워크 없이) ───────────
def test_correct_token_reaches_handler(client):
    r = client.post("/preview", headers={"X-Aiso-Token": TOKEN}, json={"workspace": BOGUS_WS})
    assert r.status_code == 200
    assert r.json()["ok"] is False  # 잘못된 경로 → 핸들러가 ok:False로 반환(인증은 통과)


# ── /f/ 정적 미리보기는 예외(iframe은 헤더를 못 실음) ──────────
def test_preview_file_route_is_exempt(client):
    r = client.get("/f/index.html")  # 미설정이라 404지만, 핵심은 401이 아니라는 것
    assert r.status_code != 401


# ── CORS: 허용 오리진만 ACAO, 악성 오리진은 차단 ──────────────
def test_preflight_allowed_origin(client):
    r = client.options(
        "/agent",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-aiso-token,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_preflight_null_origin_allowed(client):
    # prod: Electron file:// 로더 → Origin: null. 토큰 헤더 프리플라이트가 통과해야 한다.
    r = client.options(
        "/agent",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-aiso-token,content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "null"


def test_preflight_malicious_origin_no_acao(client):
    r = client.options(
        "/agent",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in r.headers


def test_cors_wraps_auth_401(client):
    # CORS가 인증 미들웨어보다 바깥이라, 401 응답에도 허용 오리진이면 ACAO가 붙는다.
    r = client.post("/preview", headers={"Origin": "http://localhost:5173"}, json={"workspace": BOGUS_WS})
    assert r.status_code == 401
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ── ComfyUI 라우트 오류 매핑(인증 환경을 구성한 이 모듈에서 main 라우트를 검증) ──
def test_comfy_checkpoints_api_failure_maps_to_safe_502(monkeypatch):
    request = httpx.Request("GET", "http://127.0.0.1:8188/models/checkpoints")

    async def fake_get_json(_base_url, _route):
        response = httpx.Response(500, request=request, text="secret server body")
        raise httpx.HTTPStatusError("secret server body", request=request, response=response)

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.comfy_checkpoints())
    assert exc.value.status_code == 502
    assert exc.value.detail == "ComfyUI가 HTTP 500 오류를 반환했습니다."
    assert "secret" not in exc.value.detail


def test_comfy_route_invalid_url_maps_to_400_without_network(monkeypatch):
    called = False

    async def fake_get_json(_base_url, _route):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(comfy_client, "_get_json", fake_get_json)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.comfy_health("http://evil.example:8188"))
    assert exc.value.status_code == 400
    assert called is False


def test_comfy_image_proxy_returns_only_validated_bytes(monkeypatch):
    async def fake_fetch(base_url, filename, subfolder, storage_type):
        assert base_url == "http://127.0.0.1:8188"
        assert (filename, subfolder, storage_type) == ("result.png", "Aiso/job", "output")
        return b"\x89PNG\r\n", "image/png"

    monkeypatch.setattr(main, "fetch_comfy_output_image", fake_fetch)
    response = asyncio.run(
        main.comfy_image(
            filename="result.png",
            subfolder="Aiso/job",
            storage_type="output",
            base_url="http://127.0.0.1:8188",
        )
    )
    assert response.body == b"\x89PNG\r\n"
    assert response.media_type == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_comfy_image_proxy_maps_safe_client_error_to_502(monkeypatch):
    async def fake_fetch(*_args):
        raise comfy_client.ComfyAPIError("ComfyUI 결과 이미지 경로가 올바르지 않습니다.")

    monkeypatch.setattr(main, "fetch_comfy_output_image", fake_fetch)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            main.comfy_image(
                filename="../secret.png",
                storage_type="output",
                base_url="http://127.0.0.1:8188",
            )
        )
    assert exc.value.status_code == 502
    assert "secret" not in exc.value.detail
