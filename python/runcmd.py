"""하네스 명령 실행 — 작업 폴더에서 임의의 셸 명령을 돌려 결과를 돌려준다.

runcode.py와 같은 이유로 동기 subprocess를 asyncio.to_thread에서 실행한다
(Windows + uvicorn --reload 프로액터 루프에서 asyncio 서브프로세스는 NotImplementedError).

보안: 셸 명령은 파일 툴처럼 경로로 가둘 수 없다(절대경로·cd ..로 작업 폴더를 벗어날 수 있음).
따라서 실제 안전장치는 경로 confine이 아니라 상위(agent) 루프의 **권한 모드**다.
run_command는 수동·읽기 모드에서 승인을 요구하고, 자동 모드에서는 사용자가 명시적으로 무승인 실행을 선택한다.
"""

from __future__ import annotations

import asyncio
import locale
import os
import subprocess
from pathlib import Path

from runcode import _bundled_bin, _find_python, run_process_capped
from process_env import sanitized_child_environment
from tools import ToolError

CMD_TIMEOUT_DEFAULT = 60
CMD_TIMEOUT_MAX = 300
MAX_CMD_OUTPUT = 30_000  # stdout+stderr 합산 표시 상한(문자)

# Windows 전용 플래그 (타 OS에서는 0으로 무해)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

RUN_COMMAND_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": (
            "작업 폴더에서 셸 명령을 실행하고 표준출력·표준에러와 종료코드를 돌려준다. "
            "테스트 실행(pytest·npm test), 빌드, git 상태 확인, 패키지 설치 등 파일 툴로 못 하는 작업에 쓴다. "
            "명령은 작업 폴더를 현재 위치로 실행되며, 실행 전 사용자 승인이 필요할 수 있다. "
            "대화형(입력 대기) 명령은 멈추니 피하고 비대화형 플래그(예: -y, --yes)를 써라."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "실행할 명령 한 줄. 파이프(|)·연결(&&)·리다이렉트(>) 사용 가능."},
                "timeout": {"type": "integer", "description": "최대 실행 시간(초). 기본 60, 최대 300."},
            },
            "required": ["command"],
        },
    },
}


def _decode(b: bytes) -> str:
    if not b:
        return ""
    try:
        return b.decode("utf-8")  # UTF-8을 유도한 도구(python 등)
    except UnicodeDecodeError:
        enc = locale.getpreferredencoding(False) or "cp949"  # cmd 콘솔 코드페이지(한국어=cp949)
        return b.decode(enc, errors="replace")


def _cap(s: str) -> str:
    if len(s) <= MAX_CMD_OUTPUT:
        return s
    half = MAX_CMD_OUTPUT // 2
    return f"{s[:half]}\n…(출력이 길어 중간 {len(s) - MAX_CMD_OUTPUT}자 생략)…\n{s[-half:]}"


def _cmd_env(root: Path) -> dict:
    env = sanitized_child_environment(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    extra = []
    b = _bundled_bin()
    if b:
        extra.append(str(b))  # 번들 grep/sed/sh/g++ 사용 가능하게
    try:
        extra.append(str(Path(_find_python()).parent))  # 'python'이 진짜 인터프리터로
    except Exception:  # noqa: BLE001
        pass
    if extra:
        env["PATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PATH", "")
    return env


def _run_cmd_sync(root: Path, command: str, timeout: int) -> str:
    comspec = os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")
    # raw 문자열로 넘긴다 — 리스트 [cmd,/c,command]는 subprocess가 재인용해 중첩 따옴표
    # (예: python -c "…")를 깨뜨린다. `/s`는 바깥 따옴표만 벗기고 내부는 그대로 둔다.
    cmdline = f'"{comspec}" /d /s /c "{command}"'
    captured = run_process_capped(
        cmdline,
        cwd=str(root),
        env=_cmd_env(root),
        timeout=timeout,
        max_output_bytes=MAX_CMD_OUTPUT,
        creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_GROUP,
    )
    if captured.timed_out:
        return f"⏱ 명령이 시간을 초과했습니다 ({timeout}초). 무한 루프나 입력 대기를 확인하세요.\n$ {command}"

    out, err = _decode(captured.stdout), _decode(captured.stderr)
    parts = [f"$ {command}", f"(종료코드 {captured.returncode})"]
    if out.strip():
        parts.append("[stdout]\n" + out.rstrip())
    if err.strip():
        parts.append("[stderr]\n" + err.rstrip())
    if not out.strip() and not err.strip():
        parts.append("(출력 없음)")
    status = "✅" if captured.returncode == 0 else "❌"
    return status + " " + _cap("\n".join(parts))


async def run_command(root: Path, command: str = "", timeout: int | None = None, **_ignore) -> str:
    if not isinstance(command, str) or not command.strip():
        raise ToolError("실행할 명령(command)이 비어 있거나 문자열이 아닙니다.")
    try:
        t = CMD_TIMEOUT_DEFAULT if timeout is None else int(timeout)
    except (TypeError, ValueError):
        t = CMD_TIMEOUT_DEFAULT
    t = max(1, min(t, CMD_TIMEOUT_MAX))
    return await asyncio.to_thread(_run_cmd_sync, root, command, t)
