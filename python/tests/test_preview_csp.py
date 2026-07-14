# -*- coding: utf-8 -*-
"""미리보기(/f) CSP — 유출(egress) 채널 차단 정책이 응답 헤더로 실제 전달되는지 고정.

CSP는 브라우저가 강제하므로 여기선 '정책이 올바르게 붙어 나가는지'를 확인한다(네트워크 없음).
정책이 약해지는 회귀(외부 와일드카드 허용·connect-src 개방 등)를 잡는다.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import main


def _hdr() -> dict:
    # 인증 토큰이 설정돼 있으면 /preview 호출에 실어준다(/f 는 인증 예외).
    return {"X-Aiso-Token": main.AUTH_TOKEN} if main.AUTH_TOKEN else {}


def test_preview_response_carries_csp():
    client = TestClient(main.app)
    d = tempfile.mkdtemp()
    (Path(d) / "index.html").write_text("<h1>hi</h1>", encoding="utf-8")

    r = client.post("/preview", json={"workspace": d}, headers=_hdr())
    assert r.json().get("ok") is True

    r2 = client.get("/f/index.html")  # /f 는 인증 예외
    assert r2.status_code == 200
    csp = r2.headers.get("content-security-policy", "")
    # 핵심 egress 차단 지시어
    assert "default-src 'self'" in csp   # 외부 도메인 리소스 차단(자기 오리진만)
    assert "connect-src 'self'" in csp   # fetch·XHR·WS·beacon 외부 전송 차단
    assert "object-src 'none'" in csp
    # 자체완결 미리보기(인라인 JS)는 허용돼야 정상 동작
    assert "'unsafe-inline'" in csp
    # 외부 와일드카드가 없어야(유출 채널 개방 회귀 방지)
    assert "*" not in csp


def test_preview_csp_policy_constant_locked():
    """정책 상수 자체가 개방 방향으로 약해지지 않았는지."""
    csp = main.PREVIEW_CSP
    assert "connect-src 'self'" in csp and "*" not in csp
