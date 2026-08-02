# -*- coding: utf-8 -*-
"""세션 최적화 감사에서 확정·적용한 개선들의 회귀 방지 테스트.

- runtime model preparation: block_count 없는 모델도 결과(None)를 캐시해 show 조회를 반복하지 않는다.
- prepare_ops: 보호대상 분리 + 검증을 한 곳에서(두 입구 공유).
- build_job/commit_job: 등록 전 완전 검증(개수·길이) + 선계산 시각 그대로 커밋(드리프트 없음).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # python/ 를 import 경로에

import discordops as ops  # noqa: E402
import discordsched as sched  # noqa: E402
from llm.providers import ollama as ollama_provider  # noqa: E402

NOW = datetime(2026, 7, 16, 22, 0)


# ── runtime model preparation: 확정 결과(None 포함) 캐시 — HTTP 반복 방지 ──
class _FakeResp:
    def __init__(self, info):
        self._info = info

    def raise_for_status(self):
        return None

    def json(self):
        return {"model_info": self._info}


class _FakeClient:
    calls = 0

    def __init__(self, info):
        self._info = info

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _FakeClient.calls += 1
        return _FakeResp(self._info)


def test_model_preparation_caches_none_no_repeat_http(monkeypatch):
    ollama_provider.OllamaAdapter.reset_cache()
    _FakeClient.calls = 0
    # block_count 키가 없는 모델 — 예전엔 매 호출마다 POST /api/show를 반복했다
    monkeypatch.setattr(ollama_provider.httpx, "AsyncClient", lambda *a, **k: _FakeClient({"foo": 1}))
    adapter = ollama_provider.OllamaAdapter("h")
    r1 = asyncio.run(adapter.prepare_model("no-block-model"))
    r2 = asyncio.run(adapter.prepare_model("no-block-model"))
    assert r1.state["layers"] is None and r2.state["layers"] is None
    assert _FakeClient.calls == 1  # 두 번째는 캐시 히트 → HTTP 0회


def test_model_preparation_caches_positive(monkeypatch):
    ollama_provider.OllamaAdapter.reset_cache()
    _FakeClient.calls = 0
    monkeypatch.setattr(ollama_provider.httpx, "AsyncClient", lambda *a, **k: _FakeClient({"gemma.block_count": 42}))
    adapter = ollama_provider.OllamaAdapter("h")
    assert asyncio.run(adapter.prepare_model("m")).state["layers"] == 42
    assert asyncio.run(adapter.prepare_model("m")).state["layers"] == 42
    assert _FakeClient.calls == 1


# ── prepare_ops: 두 입구 공유 헬퍼 ──────────────────────────────────────
def _snap():
    return {
        "guild_name": "s", "command_channel_id": "100",
        "categories": [{"id": "10", "name": "기획", "type": "category"}],
        "channels": [
            {"id": "100", "name": "aiso", "type": "text", "category_id": ""},
            {"id": "101", "name": "일반", "type": "text", "category_id": ""},
        ],
    }


def test_prepare_ops_all_protected_returns_notice():
    clean, skipped, err = ops.prepare_ops([{"action": "delete", "target": "aiso"}], _snap())
    assert clean is None and skipped and err and "적용할 것이 없습니다" in err


def test_prepare_ops_validation_error_includes_hint():
    clean, _skipped, err = ops.prepare_ops([{"action": "delete", "target": "없는채널"}], _snap())
    assert clean is None and err and "[거부]" in err and "action" in err  # FORMAT_HINT 포함


def test_prepare_ops_ok_splits_protected():
    clean, skipped, err = ops.prepare_ops(
        [{"action": "delete", "target": "일반"}, {"action": "delete", "target": "aiso"}], _snap()
    )
    assert err is None and len(clean) == 1 and len(skipped) == 1
    assert ops.format_skipped_report(skipped).startswith("\n제외됨(보호)")
    assert ops.format_skipped_report([]) == ""


# ── build_job/commit_job: 등록 전 완전검증 + 시각 드리프트 없음 ──────────
def test_build_job_validates_before_commit(tmp_path):
    sched.configure(str(tmp_path))
    # 길이 초과는 커밋 없이 draft 단계에서 거부
    draft, err = sched.build_job(channel_id="1", channel_name="c", kind="message",
                                 text="가" * (sched.TEXT_MAX + 1), when="22:55", repeat="once", now=NOW)
    assert draft is None and err and "너무 깁니다" in err
    assert sched.jobs() == []  # 커밋 안 됨


def test_build_job_count_cap_before_commit(tmp_path):
    sched.configure(str(tmp_path))
    for _ in range(sched.MAX_JOBS):
        sched.add_job(channel_id="1", channel_name="c", kind="message", text="x",
                      when="22:55", repeat="once", now=NOW)
    draft, err = sched.build_job(channel_id="1", channel_name="c", kind="message", text="x",
                                 when="22:55", repeat="once", now=NOW)
    assert draft is None and err and "최대" in err


def test_commit_uses_draft_next_run_no_reparse(tmp_path):
    """승인 전 계산한 발화 시각(draft.next_run)이 그대로 등록된다 — 재파싱 드리프트 없음."""
    sched.configure(str(tmp_path))
    draft, err = sched.build_job(channel_id="1", channel_name="공지", kind="message",
                                 text="집합", when="22:55", repeat="once", now=NOW)
    assert err is None and draft["next_run"] == "2026-07-16T22:55"
    job = sched.commit_job(draft, now=NOW)
    assert job["next_run"] == draft["next_run"]  # 미리보기와 등록이 동일
    assert sched.jobs()[0]["id"] == job["id"]


def test_pick_first_shared_helper():
    assert ops.pick_first({"a": " x ", "b": "y"}, "a", "b") == "x"
    assert ops.pick_first({"a": "  ", "b": "y"}, "a", "b") == "y"  # 빈 값 건너뜀
    assert ops.pick_first({}, "a") == ""
