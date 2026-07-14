# -*- coding: utf-8 -*-
"""run_web 전체 타임아웃 — 페이지 JS 무한 루프 등으로 검증이 무한 대기하지 않는지 고정.

_run_web_sync를 '느린' 함수로 대역화하고 RUN_WEB_TIMEOUT을 짧게 줄여, 상한을 넘으면
브라우저 결과를 기다리지 않고 '시간 초과' 오류를 돌려주는지 검증한다(네트워크·브라우저 없음).
"""
from __future__ import annotations

import asyncio
import time

import webcheck


def test_run_web_times_out(monkeypatch, tmp_path):
    html = tmp_path / "index.html"
    html.write_text("<html><body></body></html>", encoding="utf-8")

    # 경로 검증은 건너뛰고(파일은 실제로 존재) 타임아웃 로직만 본다.
    monkeypatch.setattr(webcheck, "_resolve", lambda root, path: html)
    monkeypatch.setattr(webcheck, "RUN_WEB_TIMEOUT", 0.2)

    def slow(target, rel, actions):
        time.sleep(1.0)  # RUN_WEB_TIMEOUT(0.2s)보다 훨씬 김 = 무한 대기 흉내
        return ("완료", "SHOT")

    monkeypatch.setattr(webcheck, "_run_web_sync", slow)

    report, shot = asyncio.run(webcheck.run_web(tmp_path, "index.html"))
    assert "시간 초과" in report
    assert shot is None


def test_run_web_returns_normally_when_fast(monkeypatch, tmp_path):
    """상한 안에 끝나면 정상 결과를 그대로 돌려준다."""
    html = tmp_path / "index.html"
    html.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(webcheck, "_resolve", lambda root, path: html)

    def fast(target, rel, actions):
        return ("✅ 정상 실행", "SHOTDATA")

    monkeypatch.setattr(webcheck, "_run_web_sync", fast)
    report, shot = asyncio.run(webcheck.run_web(tmp_path, "index.html"))
    assert report == "✅ 정상 실행"
    assert shot == "SHOTDATA"
