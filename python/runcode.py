"""하네스 코드 검증 — .py / .c / .cpp / .cs 를 실제로 실행·컴파일해 결과를 돌려준다.

Windows + uvicorn 루프에서 asyncio 서브프로세스가 막히므로(NotImplementedError),
동기 subprocess를 별도 스레드(asyncio.to_thread)에서 돌린다. 실행엔 타임아웃을 건다.
설치된 툴체인(python/dotnet/g++)을 자동 감지하고, 없으면 명확히 안내한다.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tools import ToolError, _resolve

RUN_TIMEOUT = 25   # 실행 상한(초)
BUILD_TIMEOUT = 90  # 빌드+실행 상한(초, dotnet 첫 실행 대비)

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
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "DOTNET_NOLOGO": "1",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    }

    def runp(cmd: list[str], timeout: int, env_override: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env_override or env
        )

    try:
        if ext == ".py":
            r = runp([_find_python(), str(target)], RUN_TIMEOUT)
            return _fmt(rel, "Python", r.returncode, r.stdout, r.stderr)

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
            exe = str(Path(tempfile.gettempdir()) / f"aiso_run_{os.getpid()}.exe")
            std = [] if is_c else ["-std=c++17"]
            comp = runp([cxx, str(target), *std, "-O0", "-o", exe], BUILD_TIMEOUT, cenv)
            if comp.returncode != 0:
                return _fmt(rel, "컴파일", comp.returncode, comp.stdout, comp.stderr)
            # 컴파일·링크 성공. 실행을 시도하되, Windows Smart App Control(SAC)이
            # 갓 빌드된 무서명 exe 실행을 막으면(WinError 4551) 빌드 검증까지를 결과로 인정한다.
            try:
                r = subprocess.run(
                    [exe], cwd=cwd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT, env=cenv
                )
                result = _fmt(rel, "C/C++ 실행", r.returncode, r.stdout, r.stderr)
            except OSError as e:
                if getattr(e, "winerror", None) == 4551:
                    result = (
                        f"✅ {rel} 컴파일·링크 성공 (C/C++). "
                        "이 PC의 Smart App Control(SAC) 정책이 갓 빌드된 실행 파일의 실행을 차단해 "
                        "런타임 검증은 생략했습니다 — 코드는 정상적으로 빌드됩니다."
                    )
                else:
                    result = f"❌ {rel} 실행기 오류: {e}"
            finally:
                try:
                    os.remove(exe)
                except OSError:
                    pass
            return result

        if ext == ".cs":
            dn = _find_dotnet()
            if not dn:
                return "[검증 불가] .NET SDK가 설치되어 있지 않습니다."
            r = runp([dn, "run", str(target)], BUILD_TIMEOUT)
            return _fmt(rel, "C#", r.returncode, r.stdout, r.stderr)

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
