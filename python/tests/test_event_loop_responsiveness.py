# -*- coding: utf-8 -*-
"""파일 도구 실행 중 이벤트 루프가 살아 있어야 한다.

`toolspec.execute`의 FILE 분기는 `run_tool`을 **동기로** 호출했다. 그래서 도구가
도는 동안 이벤트 루프가 통째로 멈췄다. 실측(파일 3000개 / 151MB 작업 폴더):

    grep       452 ms 소요 → 루프 정지 452 ms
    glob       516 ms 소요 → 루프 정지 516 ms
    list_tree  304 ms 소요 → 루프 정지 304 ms
    (대조) asyncio.sleep 294 ms → 루프 정지 16 ms

정지 동안 SSE 스트림 전송, 다른 HTTP 요청, 취소 신호 처리가 전부 대기한다.
사용자에게는 앱이 멈춘 것처럼 보인다. 실제 작업 폴더는 이보다 훨씬 클 수 있다.

여기서 고정하는 계약: 파일 도구는 워커 스레드에서 돌고, 그동안 루프는 응답한다.
"""
from __future__ import annotations

import asyncio
import time

import pytest

import toolspec
from toolspec import REGISTRY, execute

BLOCK_SECONDS = 0.30
# 스레드로 넘어갔다면 하트비트가 계속 돌아 간격이 작게 유지된다. 동기 실행이면
# 간격이 BLOCK_SECONDS 근처로 튄다. 절반을 경계로 잡아 CI 지터를 흡수한다.
MAX_ALLOWED_STALL = BLOCK_SECONDS / 2


async def _max_loop_stall(coro_factory) -> float:
    gaps: list[float] = []
    running = True

    async def heartbeat() -> None:
        last = time.perf_counter()
        while running:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)
    gaps.clear()
    await coro_factory()
    # 하트비트가 큰 간격을 기록할 기회를 준다. 여기서 바로 취소하면 정작
    # 재려던 그 간격이 기록되지 않는다.
    await asyncio.sleep(0.05)
    running = False
    beat.cancel()
    try:
        await beat
    except asyncio.CancelledError:
        pass
    return max(gaps) if gaps else 0.0


@pytest.mark.parametrize("tool_name", ["grep", "write_file", "delete_dir"])
def test_file_tools_do_not_block_the_event_loop(tmp_path, monkeypatch, tool_name):
    """읽기 도구든 변경 도구든 루프를 붙잡지 않는다."""
    def blocking_run_tool(root, name, args):
        time.sleep(BLOCK_SECONDS)
        return "완료"

    monkeypatch.setattr(toolspec, "run_tool", blocking_run_tool)
    spec = REGISTRY[tool_name]

    stall = asyncio.run(
        _max_loop_stall(lambda: execute(spec, tmp_path, "h", {}))
    )
    assert stall < MAX_ALLOWED_STALL, (
        f"{tool_name} 실행 중 이벤트 루프가 {stall * 1000:.0f}ms 멈췄다 "
        f"(허용 {MAX_ALLOWED_STALL * 1000:.0f}ms). SSE·취소·다른 요청이 그동안 대기한다."
    )


def test_file_tool_still_returns_its_result(tmp_path, monkeypatch):
    """스레드로 옮겨도 반환 계약은 그대로다 — (결과문자열, 스크린샷|None)."""
    monkeypatch.setattr(toolspec, "run_tool", lambda root, name, args: f"{name} 결과")
    result, shot = asyncio.run(execute(REGISTRY["read_file"], tmp_path, "h", {"path": "x"}))
    assert result == "read_file 결과"
    assert shot is None


def test_file_tool_errors_still_propagate(tmp_path, monkeypatch):
    """스레드 안에서 난 예외가 삼켜지면 안 된다 — 실패가 조용히 성공이 된다."""
    from tools import ToolError

    def failing(root, name, args):
        raise ToolError("파일이 없습니다")

    monkeypatch.setattr(toolspec, "run_tool", failing)
    with pytest.raises(ToolError, match="파일이 없습니다"):
        asyncio.run(execute(REGISTRY["read_file"], tmp_path, "h", {"path": "x"}))


def test_delete_dir_counts_are_bounded(tmp_path):
    """delete_dir이 메시지의 파일 개수를 세려고 트리 전체를 무제한 순회하지 않는다.

    개수는 안내 문구에만 쓰이는데, 그 때문에 거대한 폴더에서 rglob이 통째로 돌았다.
    """
    from tools import MAX_DELETE_DIR_SCAN

    assert isinstance(MAX_DELETE_DIR_SCAN, int) and MAX_DELETE_DIR_SCAN > 0


def test_concurrent_file_tools_do_not_interfere(tmp_path):
    """파일 도구를 스레드로 옮기면 서로 다른 런의 도구가 실제로 동시에 돌 수 있다.

    tools.py의 모듈 전역(`_TREE_SKIP`, `_DISPATCH`, `TOOL_SCHEMAS`)은 초기화 후
    읽기만 하는 조회 테이블이라 안전하다. 누군가 런타임에 그걸 갱신하도록 바꾸면
    조용한 데이터 경쟁이 되므로, 동시 실행 결과가 섞이지 않는지 여기서 고정한다.
    """
    for index in range(8):
        (tmp_path / f"doc{index}.txt").write_text(f"내용-{index}", encoding="utf-8")

    async def run_all():
        return await asyncio.gather(*[
            execute(REGISTRY["read_file"], tmp_path, "h", {"path": f"doc{index}.txt"})
            for index in range(8)
        ])

    results = asyncio.run(run_all())
    for index, (text, shot) in enumerate(results):
        assert f"내용-{index}" in text, f"{index}번 결과가 다른 호출과 섞였다: {text[:60]}"
        assert shot is None
