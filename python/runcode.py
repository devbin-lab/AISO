"""하네스 코드 검증 — .py / .c / .cpp / .cs 를 실제로 실행·컴파일해 결과를 돌려준다.

Windows + uvicorn 루프에서 asyncio 서브프로세스가 막히므로(NotImplementedError),
동기 subprocess를 별도 스레드(asyncio.to_thread)에서 돌린다. 실행엔 타임아웃을 건다.
설치된 툴체인(python/dotnet/g++)을 자동 감지하고, 없으면 명확히 안내한다.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import winjob
from tools import ToolError, _resolve
from process_env import sanitized_child_environment

RUN_TIMEOUT = 25   # 실행 상한(초)
BUILD_TIMEOUT = 90  # 빌드+실행 상한(초, dotnet 첫 실행 대비)
MAX_PROCESS_OUTPUT_BYTES = 64 * 1024

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


class _BoundedBytes:
    """Drain a pipe completely while retaining only a bounded head and tail."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1024, int(limit))
        self._head_limit = self.limit // 2
        self._tail_limit = self.limit - self._head_limit
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_size = 0
        self.total = 0

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        remaining = chunk
        head_space = self._head_limit - len(self._head)
        if head_space > 0:
            self._head.extend(remaining[:head_space])
            remaining = remaining[head_space:]
        if remaining:
            self._tail.append(remaining)
            self._tail_size += len(remaining)
            while self._tail_size > self._tail_limit:
                first = self._tail[0]
                overflow = self._tail_size - self._tail_limit
                if len(first) <= overflow:
                    self._tail.popleft()
                    self._tail_size -= len(first)
                else:
                    self._tail[0] = first[overflow:]
                    self._tail_size -= overflow

    def result(self) -> tuple[bytes, bool]:
        tail = b"".join(self._tail)
        retained = bytes(self._head) + tail
        if self.total <= self.limit:
            return retained, False
        omitted = max(0, self.total - len(retained))
        marker = f"\n... [{omitted} bytes omitted; output limit reached] ...\n".encode("utf-8")
        return bytes(self._head) + marker + tail, True


@dataclass(frozen=True)
class CapturedProcess:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    # 봉쇄를 요청했는데 걸지 못했으면 False. 호출자는 이 사실을 결과에 드러내야 한다 —
    # 조용히 약해지는 보안 장치가 가장 나쁘다.
    contained: bool = True


def _drain_pipe(pipe, sink: _BoundedBytes) -> None:
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            sink.append(chunk)
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate the process and children without retaining their output."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
                timeout=10,
                env=sanitized_child_environment(),
            )
            return
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_process_capped(
    command: list[str] | str,
    *,
    cwd: str,
    env: dict | None,
    timeout: int,
    max_output_bytes: int = MAX_PROCESS_OUTPUT_BYTES,
    creationflags: int = 0,
    job_limits: "winjob.JobLimits | None" = None,
) -> CapturedProcess:
    """Run a child process without letting unbounded output consume backend RAM.

    ``job_limits`` 를 주면 Windows Job Object 로 봉쇄한다. 자식은 **정지 상태로**
    만들어 job 에 넣은 뒤 재개한다 — 먼저 돌게 두면 할당 전에 자식을 낳을 수 있고,
    그 자식은 제한 밖에 있게 된다. 이 순서가 봉쇄의 전부다.
    """
    job = winjob.create_job(job_limits) if job_limits is not None else None
    contained = job_limits is None or job is not None
    spawn_flags = creationflags | _CREATE_NO_WINDOW | _CREATE_NEW_GROUP
    if job is not None:
        spawn_flags |= winjob.CREATE_SUSPENDED
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=spawn_flags,
    )
    if job is not None:
        try:
            if not winjob.assign_process(job, int(proc._handle)):  # type: ignore[attr-defined]
                contained = False
        finally:
            # 할당에 실패해도 반드시 재개해야 한다. 안 그러면 정지된 채로 남아
            # 타임아웃까지 매달린다.
            winjob.resume_process(proc.pid)
    assert proc.stdout is not None and proc.stderr is not None
    stdout_sink = _BoundedBytes(max_output_bytes)
    stderr_sink = _BoundedBytes(max_output_bytes)
    readers = [
        threading.Thread(target=_drain_pipe, args=(proc.stdout, stdout_sink), daemon=True),
        threading.Thread(target=_drain_pipe, args=(proc.stderr, stderr_sink), daemon=True),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        finally:
            for reader in readers:
                reader.join(timeout=5)
    finally:
        # KILL_ON_JOB_CLOSE 덕에 핸들을 닫는 것만으로 살아남은 손자까지 함께 죽는다.
        # taskkill /T 는 부모가 이미 죽은 손자를 놓치지만 이것은 놓치지 않는다.
        winjob.close_job(job)

    stdout, stdout_truncated = stdout_sink.result()
    stderr, stderr_truncated = stderr_sink.result()
    return CapturedProcess(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        contained=contained,
    )

RUN_CODE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_code",
        "description": (
            "작성한 코드 파일을 실제로 실행·컴파일해 검증한다. Python(.py), C/C++(.c/.cpp), C#(.cs) 지원. "
            "코드를 만들거나 고친 뒤에는 반드시 이 툴로 검증하고, 에러가 나오면 고쳐서 다시 검증하라."
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "작업 폴더 기준 코드 파일 경로."}},
            "required": ["path"],
        },
    },
}


def _find_python() -> str:
    la = os.environ.get("LOCALAPPDATA")
    if la:
        base = Path(la) / "Programs" / "Python"
        if base.is_dir():
            for d in sorted(base.glob("Python3*"), reverse=True):
                exe = d / "python.exe"
                if exe.exists():
                    return str(exe)
    return sys.executable  # 폴백: 사이드카 인터프리터


def _python_process_budget(exe: str) -> int:
    """이 인터프리터로 .py 를 돌릴 때 필요한 최소 프로세스 수.

    venv 의 python.exe 는 실제 인터프리터를 다시 띄우는 **리다이렉터**라 한 개를 더 쓴다
    (실측: 시스템 python 은 한도 1로 성공, venv python 은 한도 1에서 rc=101
    "Unable to create process"). _find_python() 이 시스템 인터프리터를 못 찾아
    사이드카 자신으로 폴백한 경우에만 해당한다.

    그 경우 봉쇄가 한 칸 느슨해진다 — 검증 코드가 자식 하나를 만들 수 있다.
    한도를 1로 조이면 검증 자체가 아예 안 되므로 이쪽을 택했고, 이 사실을 여기 적어 둔다.
    """
    base = getattr(sys, "_base_executable", None)
    same = os.path.normcase(exe) == os.path.normcase(sys.executable)
    redirector = bool(base) and os.path.normcase(str(base)) != os.path.normcase(sys.executable)
    return 2 if (same and redirector) else winjob.UNTRUSTED_PROCESS_LIMIT


def _find_dotnet() -> str | None:
    w = shutil.which("dotnet")
    if w and "WindowsApps" not in w:
        return w
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    p = Path(pf) / "dotnet" / "dotnet.exe"
    return str(p) if p.exists() else (w or None)


def _bundled_bin() -> Path | None:
    """앱에 번들된 w64devkit(MinGW-w64)의 bin 디렉터리. 없으면 None.

    개발:   <프로젝트 루트>/tools/w64devkit/bin
    패키징: <resources>/tools/w64devkit/bin (Electron이 AISO_TOOLS_DIR로 지정)
    """
    roots: list[Path] = []
    env_dir = os.environ.get("AISO_TOOLS_DIR")
    if env_dir:
        roots.append(Path(env_dir))
    roots.append(Path(__file__).resolve().parent.parent)  # python/ 의 부모 = 프로젝트 루트
    for base in roots:
        b = base / "tools" / "w64devkit" / "bin"
        if (b / "g++.exe").exists():
            return b
    return None


def _find_cxx(is_c: bool) -> tuple[str | None, str | None]:
    """(컴파일러 경로, PATH에 추가할 bin 디렉터리 or None).

    번들 w64devkit(자체 탑재)을 최우선으로 쓰고, 없으면 시스템 g++/clang++로 폴백한다.
    """
    bundled = _bundled_bin()
    if bundled:
        exe = bundled / ("gcc.exe" if is_c else "g++.exe")
        if exe.exists():
            return str(exe), str(bundled)
    names = ("gcc", "clang", "g++", "clang++") if is_c else ("g++", "clang++")
    for n in names:
        w = shutil.which(n)
        if w:
            return w, None
    return None, None


def _fmt(rel: str, label: str, rc: int, out: str, err: str) -> str:
    out, err = (out or "")[-4000:], (err or "")[-4000:]
    if rc == 0:
        body = f"출력:\n{out.strip()}" if out.strip() else "(출력 없음)"
        return f"✅ {rel} 실행 성공 ({label}, 종료코드 0).\n{body}"
    # 실패: 상세 에러가 stdout·stderr에 나뉘어 있을 수 있으니 둘 다 합친다
    parts = []
    if out.strip():
        parts.append(out.strip())
    if err.strip() and err.strip() not in "\n".join(parts):
        parts.append(err.strip())
    detail = "\n".join(parts) or "(출력 없음)"
    return f"❌ {rel} 실패 ({label}, 종료코드 {rc}).\n{detail}\n→ 위 에러를 고친 뒤 run_code로 다시 검증하라."


def _run_sync(target: Path, rel: str) -> str:
    ext = target.suffix.lower()
    cwd = str(target.parent)
    env = sanitized_child_environment(
        PYTHONIOENCODING="utf-8",
        DOTNET_NOLOGO="1",
        DOTNET_CLI_TELEMETRY_OPTOUT="1",
    )

    def runp(
        cmd: list[str],
        timeout: int,
        env_override: dict | None = None,
        *,
        limits: winjob.JobLimits,
    ) -> CapturedProcess:
        return run_process_capped(
            cmd,
            cwd=cwd,
            env=env_override or env,
            timeout=timeout,
            job_limits=limits,
        )

    def report(label: str, captured: CapturedProcess) -> str:
        if captured.timed_out:
            return f"⏱ {rel} {label}이(가) 시간을 초과했습니다 (무한 루프·입력 대기 등을 확인하세요)."
        text = _fmt(
            rel,
            label,
            captured.returncode,
            captured.stdout.decode("utf-8", errors="replace"),
            captured.stderr.decode("utf-8", errors="replace"),
        )
        return text if captured.contained else text + winjob.CONTAINMENT_UNAVAILABLE_NOTE

    try:
        if ext == ".py":
            interpreter = _find_python()
            r = runp(
                [interpreter, str(target)],
                RUN_TIMEOUT,
                limits=winjob.JobLimits(active_processes=_python_process_budget(interpreter)),
            )
            return report("Python", r)

        if ext in (".cpp", ".cc", ".cxx", ".c"):
            is_c = ext == ".c"
            cxx, cxx_bin = _find_cxx(is_c)
            if not cxx:
                return (
                    "[검증 불가] C/C++ 컴파일러를 찾지 못했습니다. "
                    "번들 MinGW-w64(tools/w64devkit)가 없고 시스템에도 g++/clang++가 없습니다."
                )
            # 번들 g++는 같은 bin 안의 as/ld를 PATH로 찾으므로 bin을 PATH 선두에 얹는다
            cenv = env
            if cxx_bin:
                cenv = {**env, "PATH": cxx_bin + os.pathsep + env.get("PATH", "")}
            exe = str(Path(tempfile.gettempdir()) / f"aiso_run_{os.getpid()}_{uuid.uuid4().hex}.exe")
            try:
                std = [] if is_c else ["-std=c++17"]
                # 컴파일러는 신뢰 대상이다(우리가 번들한 g++). cc1plus·as·ld 를 낳으므로
                # 여유를 준다 — 실측 최소 3, 여기서는 4.
                comp = runp([cxx, str(target), *std, "-O0", "-o", exe], BUILD_TIMEOUT, cenv,
                            limits=winjob.COMPILER)
                if comp.timed_out:
                    return report("컴파일", comp)
                if comp.returncode != 0:
                    return report("컴파일", comp)
                # 컴파일·링크 성공. 실행을 시도하되, Windows Smart App Control(SAC)이
                # 갓 빌드된 무서명 exe 실행을 막으면(WinError 4551) 빌드 검증까지를 결과로 인정한다.
                try:
                    # 여기서부터가 신뢰할 수 없는 코드다 — 한도 1로 조여 자식 생성을 막는다.
                    r = runp([exe], RUN_TIMEOUT, cenv, limits=winjob.UNTRUSTED)
                    return report("C/C++ 실행", r)
                except OSError as e:
                    if getattr(e, "winerror", None) == 4551:
                        return (
                            f"✅ {rel} 컴파일·링크 성공 (C/C++). "
                            "이 PC의 Smart App Control(SAC) 정책이 갓 빌드된 실행 파일의 실행을 차단해 "
                            "런타임 검증은 생략했습니다 — 코드는 정상적으로 빌드됩니다."
                        )
                    return f"❌ {rel} 실행기 오류: {e}"
            finally:
                try:
                    os.remove(exe)
                except OSError:
                    pass

        if ext == ".cs":
            dn = _find_dotnet()
            if not dn:
                return "[검증 불가] .NET SDK가 설치되어 있지 않습니다."
            # `dotnet run` 은 빌드와 실행을 한 프로세스 트리에서 한다. 그러면 사용자 코드가
            # 빌드 시스템에 필요한 프로세스 여유 안에서 돌게 되어 자식 생성을 막을 수 없다.
            # 그래서 둘로 쪼갠다 — 빌드는 여유를 주고, **실행은 한도 1로 조인다**.
            # (실측: dotnet build 는 최소 3, 산출된 exe 실행은 1로 충분하다.)
            outdir = Path(tempfile.gettempdir()) / f"aiso_cs_{os.getpid()}_{uuid.uuid4().hex}"
            try:
                build = runp([dn, "build", str(target), "-o", str(outdir)], BUILD_TIMEOUT,
                             limits=winjob.BUILD_SYSTEM)
                if build.timed_out or build.returncode != 0:
                    return report("C# 빌드", build)
                built = next(iter(sorted(outdir.glob("*.exe"))), None)
                if built is None:
                    # 산출물을 못 찾으면 예전 경로로 되돌린다 — 검증을 못 하는 것보다는 낫다.
                    # 이 경로에서는 실행이 빌드 트리 안에 있어 봉쇄가 느슨하다.
                    r = runp([dn, "run", str(target)], BUILD_TIMEOUT, limits=winjob.BUILD_SYSTEM)
                    return report("C#", r)
                r = runp([str(built)], RUN_TIMEOUT, limits=winjob.UNTRUSTED)
                return report("C# 실행", r)
            finally:
                shutil.rmtree(outdir, ignore_errors=True)

        return f"[검증 불가] 실행 검증을 지원하지 않는 확장자입니다: {ext} (지원: .py, .c, .cpp, .cs)"
    except subprocess.TimeoutExpired:
        return f"⏱ {rel} 실행이 시간을 초과했습니다 (무한 루프·입력 대기 등을 확인하세요)."
    except FileNotFoundError as e:
        return f"[검증 불가] 실행기를 찾을 수 없습니다: {e}"


async def run_code(root: Path, path: str, **_ignore) -> str:
    """코드 파일을 실행 검증한다 (경로 검증은 여기서, 실행은 스레드에서)."""
    target = _resolve(root, path)
    if not target.is_file():
        raise ToolError(f"파일이 없습니다: {path}")
    return await asyncio.to_thread(_run_sync, target, path)
