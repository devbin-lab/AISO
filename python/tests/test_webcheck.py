# -*- coding: utf-8 -*-
"""run_web의 격리 작업자, 단계형 DSL, 실제 Edge 상호작용 계약을 고정한다."""
from __future__ import annotations

import asyncio
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import types

import pytest

import webcheck


_EDGE_PATHS = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


def _edge_integration_available() -> bool:
    if not any(path.is_file() for path in _EDGE_PATHS):
        return False
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


requires_edge = pytest.mark.skipif(
    not _edge_integration_available(),
    reason="Playwright 또는 Microsoft Edge가 설치되어 있지 않습니다.",
)


def test_run_web_times_out(monkeypatch, tmp_path):
    html = tmp_path / "index.html"
    html.write_text("<html><body></body></html>", encoding="utf-8")

    # 실제 격리 작업자의 Edge 시작까지 포함해 프로세스 트리 제한을 검증한다.
    monkeypatch.setattr(webcheck, "RUN_WEB_TIMEOUT", 0.2)

    report, shot = asyncio.run(webcheck.run_web(tmp_path, "index.html"))
    assert "시간 초과" in report
    assert shot is None


def test_worker_temp_cleanup_retries_windows_sharing_violation(monkeypatch, tmp_path):
    temp = tmp_path / "worker"
    temp.mkdir()
    calls = 0
    real_rmtree = webcheck.shutil.rmtree

    def flaky_rmtree(path):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError(32, "sharing violation", str(path), 32)
        real_rmtree(path)

    monkeypatch.setattr(webcheck.shutil, "rmtree", flaky_rmtree)
    monkeypatch.setattr(webcheck.time, "sleep", lambda _seconds: None)

    webcheck._cleanup_worker_temp_dir(temp)

    assert calls == 3
    assert not temp.exists()


def test_worker_temp_cleanup_does_not_fail_completed_validation_on_persistent_lock(monkeypatch, tmp_path, caplog):
    temp = tmp_path / "worker"
    temp.mkdir()

    def locked_rmtree(path):
        raise OSError(32, "sharing violation", str(path), 32)

    monkeypatch.setattr(webcheck.shutil, "rmtree", locked_rmtree)
    monkeypatch.setattr(webcheck.time, "sleep", lambda _seconds: None)

    webcheck._cleanup_worker_temp_dir(temp)

    assert temp.exists()
    assert "cleanup deferred" in caplog.text


def test_run_web_returns_normally_when_fast(monkeypatch, tmp_path):
    """상한 안에 끝나면 정상 결과를 그대로 돌려준다."""
    html = tmp_path / "index.html"
    html.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(webcheck, "_resolve", lambda root, path: html)

    def fast(root, path, actions, steps, cancel_event):
        assert root == tmp_path
        assert path == "index.html"
        assert actions is None
        assert steps is None
        assert isinstance(cancel_event, threading.Event)
        assert not cancel_event.is_set()
        return ("✅ 정상 실행", "SHOTDATA")

    monkeypatch.setattr(webcheck, "_run_web_process", fast)
    report, shot = asyncio.run(webcheck.run_web(tmp_path, "index.html"))
    assert report == "✅ 정상 실행"
    assert shot == "SHOTDATA"


def test_headless_browser_receives_sanitized_environment(monkeypatch, tmp_path):
    """웹 검증용 Edge 프로세스도 sidecar/NVIDIA 키를 상속하지 않는다."""
    html = tmp_path / "index.html"
    html.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setenv("AISO_AUTH_TOKEN", "sidecar-canary")
    monkeypatch.setenv("NVIDIA_API_KEY", "nvidia-canary")
    monkeypatch.setenv("AISO_VISIBLE_LOCALE", "ko-KR")
    captured: dict[str, object] = {}

    def launch(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after launch options")

    class FakePlaywrightContext:
        def __enter__(self):
            chromium = types.SimpleNamespace(launch=launch)
            return types.SimpleNamespace(chromium=chromium)

        def __exit__(self, *_args):
            return False

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: FakePlaywrightContext()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    report, shot = webcheck._run_web_sync(html, "index.html")

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert "AISO_AUTH_TOKEN" not in child_env
    assert "NVIDIA_API_KEY" not in child_env
    assert child_env["AISO_VISIBLE_LOCALE"] == "ko-KR"
    assert "검증 불가" in report
    assert shot is None


def test_run_web_schema_exposes_steps_dsl_and_keeps_legacy_actions():
    """모델이 새 단계형 계약과 종전 키보드 계약을 모두 발견할 수 있어야 한다."""
    parameters = webcheck.RUN_WEB_SCHEMA["function"]["parameters"]
    properties = parameters["properties"]

    assert "actions" in properties
    assert "steps" in properties
    assert parameters["required"] == ["path"]

    serialized = json.dumps(properties["steps"], ensure_ascii=False)
    for token in (
        "click",
        "role",
        "name",
        "testid",
        "css",
        "x_ratio",
        "y_ratio",
        "press",
        "wait",
        "visible",
        "text",
        "state",
        "canvas_changed",
        "canvas_unchanged",
    ):
        assert token in serialized


def test_run_web_forwards_legacy_actions_and_steps(monkeypatch, tmp_path):
    """steps 추가가 기존 actions를 버리거나 바꾸지 않도록 공개 진입점을 고정한다."""
    html = tmp_path / "index.html"
    html.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(webcheck, "_resolve", lambda root, path: html)
    captured: dict[str, object] = {}

    def fake(root, path, actions, steps, cancel_event):
        captured.update(
            root=root,
            path=path,
            actions=actions,
            steps=steps,
            cancel_event=cancel_event,
        )
        return ("정상", "SHOT")

    monkeypatch.setattr(webcheck, "_run_web_process", fake)
    legacy = [{"key": "ArrowLeft", "times": 2}]
    steps = [{"action": "press", "key": "ArrowRight", "times": 3}]

    report, shot = asyncio.run(
        webcheck.run_web(tmp_path, "index.html", actions=legacy, steps=steps)
    )

    assert (report, shot) == ("정상", "SHOT")
    assert captured["root"] == tmp_path
    assert captured["path"] == "index.html"
    assert captured["actions"] == legacy
    assert captured["steps"] == steps
    assert isinstance(captured["cancel_event"], threading.Event)
    assert not captured["cancel_event"].is_set()


def _write_interactive_fixture(path: Path) -> None:
    path.write_text(
        """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>Aiso interaction contract</title>
  <style>
    body { margin: 0; background: rgb(245, 245, 245); color: #111; }
    #stage { width: 40px; height: 40px; }
    #paused[hidden] { display: none; }
  </style>
</head>
<body>
  <button id="start">게임 시작</button>
  <button data-testid="pause-game">일시정지</button>
  <button id="resume">재개</button>
  <p id="status">ready</p>
  <p id="paused" hidden>paused panel</p>
  <p id="delayed">waiting</p>
  <canvas id="stage" width="40" height="40"></canvas>
  <script>
    const canvas = document.querySelector('#stage');
    const ctx = canvas.getContext('2d');
    window.__AISO_TEST__ = { state: 'ready', x: 1, paused: false };
    function paint() {
      ctx.clearRect(0, 0, 40, 40);
      ctx.fillStyle = '#1f6feb';
      ctx.fillRect(window.__AISO_TEST__.x, 4, 8, 8);
    }
    paint();
    document.querySelector('#start').addEventListener('click', () => {
      window.__AISO_TEST__.state = 'running';
      document.querySelector('#status').textContent = 'running';
      document.body.style.background = 'rgb(255, 230, 180)';
    });
    document.querySelector('[data-testid="pause-game"]').addEventListener('click', () => {
      window.__AISO_TEST__.state = 'paused';
      window.__AISO_TEST__.paused = true;
      document.querySelector('#status').textContent = 'game paused';
      document.querySelector('#paused').hidden = false;
    });
    document.querySelector('#resume').addEventListener('click', () => {
      window.__AISO_TEST__.state = 'running';
      window.__AISO_TEST__.paused = false;
      document.querySelector('#status').textContent = 'resumed';
      setTimeout(() => {
        document.querySelector('#delayed').textContent = 'wait completed';
      }, 50);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowRight') {
        window.__AISO_TEST__.x += 10;
        paint();
      }
      if (event.code === 'KeyP') {
        window.__AISO_TEST__.paused = true;
      }
    });
    canvas.addEventListener('click', (event) => {
      window.__AISO_TEST__.canvasRegion =
        event.offsetX >= 20 && event.offsetY >= 20 ? 'bottom-right' : 'other';
    });
  </script>
</body>
</html>
""",
        encoding="utf-8",
    )


@requires_edge
def test_edge_steps_cover_click_press_wait_assertions_and_post_action_screenshot(tmp_path):
    """모든 조작은 로컬 Edge 한 번의 배치 안에서 실행되고 마지막 화면이 반환되어야 한다."""
    html = tmp_path / "index.html"
    _write_interactive_fixture(html)

    baseline_report, baseline_shot = webcheck._run_web_sync(html, "index.html")
    assert baseline_shot is not None, baseline_report

    steps = [
        {"action": "click", "by": "role", "role": "button", "name": "게임 시작"},
        {"assert": "text", "by": "css", "selector": "#status", "equals": "running"},
        {"assert": "state", "path": "window.__AISO_TEST__.state", "equals": "running"},
        {"action": "click", "by": "testid", "value": "pause-game"},
        {"assert": "visible", "by": "css", "selector": "#paused"},
        {"assert": "text", "by": "css", "selector": "#status", "contains": "paused"},
        {"action": "click", "by": "css", "selector": "#resume"},
        {
            "action": "click",
            "by": "css",
            "selector": "#stage",
            "x_ratio": 0.75,
            "y_ratio": 0.75,
        },
        {
            "assert": "state",
            "path": "window.__AISO_TEST__.canvasRegion",
            "equals": "bottom-right",
        },
        {"action": "press", "key": "ArrowRight", "times": 1},
        {"assert": "canvas_changed"},
        {"action": "press", "key": "KeyP", "times": 1},
        {"action": "wait", "ms": 120},
        {"assert": "canvas_unchanged"},
        {"assert": "text", "by": "css", "selector": "#delayed", "contains": "completed"},
    ]

    report, final_shot = webcheck._run_web_sync(
        html,
        "index.html",
        steps=steps,
    )

    assert "단계 검증" in report
    assert "15/15" in report
    assert "실패" not in report
    assert final_shot is not None
    assert base64.b64decode(final_shot).startswith(b"\x89PNG\r\n\x1a\n")
    # 시작 버튼이 배경색을 바꾼다. 조작 전 스크린샷을 재사용하면 이 비교가 실패한다.
    assert final_shot != baseline_shot


@requires_edge
def test_public_run_web_worker_round_trips_an_interaction_scenario(tmp_path):
    """공개 진입점도 별도 작업자에서 steps와 마지막 화면을 그대로 왕복해야 한다."""
    html = tmp_path / "index.html"
    _write_interactive_fixture(html)

    report, shot = asyncio.run(
        webcheck.run_web(
            tmp_path,
            "index.html",
            steps=[
                {"action": "click", "by": "role", "role": "button", "name": "게임 시작"},
                {"assert": "state", "path": "window.__AISO_TEST__.state", "equals": "running"},
            ],
        )
    )

    assert "status=PASS" in report
    assert "2/2" in report
    assert shot is not None
    assert base64.b64decode(shot).startswith(b"\x89PNG\r\n\x1a\n")


@requires_edge
def test_edge_missing_selector_is_an_explicit_failure_with_final_screenshot(tmp_path):
    html = tmp_path / "index.html"
    _write_interactive_fixture(html)

    report, shot = webcheck._run_web_sync(
        html,
        "index.html",
        steps=[{"action": "click", "by": "css", "selector": "#does-not-exist"}],
    )

    assert "단계 1" in report
    assert "실패" in report
    assert "#does-not-exist" in report
    assert shot is not None


@requires_edge
def test_edge_rejects_executable_state_paths_instead_of_evaluating_them(tmp_path):
    """state.path는 점으로 잇는 식별자만 허용하며 JS 표현식 실행 통로가 아니어야 한다."""
    html = tmp_path / "index.html"
    _write_interactive_fixture(html)
    unsafe_path = "window.__AISO_TEST__.state = 'pwned'"

    report, shot = webcheck._run_web_sync(
        html,
        "index.html",
        steps=[{"assert": "state", "path": unsafe_path, "equals": "pwned"}],
    )

    assert "단계 1" in report
    assert "실패" in report
    assert "상태 경로" in report
    assert shot is not None


@requires_edge
def test_edge_missing_state_path_never_equals_null(tmp_path):
    """존재하지 않는 값의 JavaScript undefined를 Python None으로 오인하면 안 된다."""
    html = tmp_path / "index.html"
    _write_interactive_fixture(html)

    report, shot = webcheck._run_web_sync(
        html,
        "index.html",
        steps=[{
            "assert": "state",
            "path": "window.__AISO_TEST__.missing",
            "equals": None,
        }],
    )

    assert "status=FAIL" in report
    assert "상태 경로가 존재하지 않습니다" in report
    assert shot is not None


@pytest.mark.parametrize(
    ("validation_status", "expected_ok"),
    [("PASS", True), ("FAIL", False), ("INCONCLUSIVE", False)],
)
def test_verify_endpoint_forwards_steps_and_maps_validation_status(
    monkeypatch,
    tmp_path,
    validation_status,
    expected_ok,
):
    import main

    captured: dict[str, object] = {}

    async def fake_run_web(root, path, actions=None, steps=None):
        captured.update(root=root, path=path, actions=actions, steps=steps)
        return (
            f"[WEB_VALIDATION v1]\nstatus={validation_status} level=function\nsummary=test",
            "SHOT",
        )

    monkeypatch.setattr(main, "validate_workspace", lambda _workspace: tmp_path)
    monkeypatch.setattr(main, "run_web", fake_run_web)
    request = main.VerifyRequest(
        workspace=str(tmp_path),
        path="index.html",
        actions=[{"key": "Enter"}],
        steps=[{"assert": "visible", "by": "css", "selector": "#app"}],
    )

    response = asyncio.run(main.verify(request))

    assert response["ok"] is expected_ok
    assert response["status"] == validation_status
    assert response["screenshot"] == "SHOT"
    assert captured == {
        "root": tmp_path,
        "path": "index.html",
        "actions": [{"key": "Enter"}],
        "steps": [{"assert": "visible", "by": "css", "selector": "#app"}],
    }


class _NetworkCanaryHandler(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):  # noqa: N802
        type(self).hits += 1
        body = b"network should have been blocked"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


@requires_edge
def test_edge_blocks_network_popups_and_downloads_inside_the_validation_sandbox(tmp_path):
    """검증 조작은 외부 부작용 없이 로컬 페이지 안에서만 끝나야 한다."""
    _NetworkCanaryHandler.hits = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _NetworkCanaryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    canary_url = f"http://127.0.0.1:{server.server_port}/aiso-canary"

    html = tmp_path / "security.html"
    html.write_text(
        f"""<!doctype html>
<html><body>
  <button id="network">network</button>
  <button id="popup">popup</button>
  <a id="download" download="aiso-canary.txt" href="data:text/plain,blocked">download</a>
  <p id="popup-state">waiting</p>
  <script>
    document.querySelector('#network').onclick = () => {{
      const image = new Image();
      image.src = {json.dumps(canary_url)};
      document.body.appendChild(image);
    }};
    document.querySelector('#popup').onclick = () => {{
      const popup = window.open('about:blank', '_blank');
      setTimeout(() => {{
        document.querySelector('#popup-state').textContent =
          (!popup || popup.closed) ? 'popup blocked' : 'popup open';
      }}, 80);
    }};
  </script>
</body></html>
""",
        encoding="utf-8",
    )

    try:
        report, shot = webcheck._run_web_sync(
            html,
            "security.html",
            steps=[
                {"action": "click", "by": "css", "selector": "#network"},
                {"action": "wait", "ms": 100},
                {"action": "click", "by": "css", "selector": "#popup"},
                {"action": "wait", "ms": 150},
                {
                    "assert": "text",
                    "by": "css",
                    "selector": "#popup-state",
                    "contains": "blocked",
                },
                {"action": "click", "by": "css", "selector": "#download"},
                {"action": "wait", "ms": 100},
            ],
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert _NetworkCanaryHandler.hits == 0
    assert "네트워크 차단" in report
    assert "팝업 차단" in report
    assert "다운로드 차단" in report
    assert "실패" not in report
    assert shot is not None


@requires_edge
def test_strict_validation_blocks_non_web_local_subresources_without_leaking_content(tmp_path):
    secret = "AISO_WEB_SECRET_7F31"
    (tmp_path / "private.env").write_text(
        f'window.WORKSPACE_SECRET="{secret}";',
        encoding="utf-8",
    )
    (tmp_path / "app.js").write_text(
        "document.querySelector('#state').textContent='app loaded';",
        encoding="utf-8",
    )
    html = tmp_path / "index.html"
    html.write_text(
        """<!doctype html><p id="state">waiting</p>
<script src="app.js"></script>
<script src="private.env"></script>
<script>if (window.WORKSPACE_SECRET) console.error(window.WORKSPACE_SECRET);</script>
""",
        encoding="utf-8",
    )

    report, shot = webcheck._run_web_sync(
        html,
        "index.html",
        steps=[{
            "assert": "text",
            "by": "css",
            "selector": "#state",
            "contains": "app loaded",
        }],
        allowed_root=tmp_path,
        strict_local_assets=True,
    )

    assert "status=INCONCLUSIVE" in report
    assert "security=로컬 파일 차단 1건" in report
    assert secret not in report
    assert "1 PASS" in report
    assert shot is not None
