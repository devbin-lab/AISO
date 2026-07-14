# -*- coding: utf-8 -*-
"""web_fetch 서브리소스 SSRF 가드 — 페이지 JS가 로컬/사설망으로 보내는 요청 차단 판정 고정.

IP 리터럴·localhost 패턴은 DNS 없이 오프라인으로 판정되므로 네트워크 없이 검증 가능
(공인 호스트명은 DNS가 필요해 여기선 공인 IP 리터럴로 대체).
"""
from __future__ import annotations

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
