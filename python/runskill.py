# -*- coding: utf-8 -*-
"""스킬(재사용 자동화 도구) 제작·실행.

이 에이전트는 사용자의 프로그램 코드를 대신 짜 주지 않는다(문서는 .md만 허용).
대신 반복 자동화가 필요하면 여기 create_skill로 '스킬'을 만들어 앱 전용 폴더에 저장하고,
run_skill로 도구처럼 실행한다. 스킬은 파이썬 코드를 담을 수 있는 유일한 경로다.

저장 구조:  <skills_dir>/<이름>/main.py         — 실행 진입점(파이썬)
            <skills_dir>/<이름>/skill.json     — 메타(이름·설명·생성시각)

실행 규약:  main.py는 실행되면 결과를 표준출력(print)으로 낸다. 입력이 필요하면
            sys.argv[1]에 JSON 문자열로 전달되니 json.loads로 읽는다(없으면 인자 없음).

skills_dir는 Electron이 환경변수 AISO_SKILLS_DIR로 지정한다(앱 userData/skills).
Electron 없이 독립 실행(테스트·개발)할 땐 ~/.aiso/skills로 폴백한다.

보안: 스킬은 임의 파이썬을 실행하므로 run_command와 같은 최고 승인등급(ALWAYS)으로 다룬다
(읽기·수동 모드에서 승인 필요, 자동 모드에서만 무승인). 이름은 경로 구분자·상위참조가
불가능하도록 [\\w-]로 제한해 skills_dir 밖으로 새지 않게 한다.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from tools import ToolError
from process_env import sanitized_child_environment

SKILL_RUN_TIMEOUT = 60          # 스킬 실행 상한(초) — 브리핑·수집 등 네트워크 여유
MAX_CODE_BYTES = 200_000        # 스킬 코드 크기 상한
MAX_SKILL_OUTPUT = 8_000        # 실행 결과 표시 상한(문자)
_NAME_RE = re.compile(r"[\w-]{1,64}", re.UNICODE)  # 영/숫자/한글/_/- (경로문자·점 불가)

# Windows 전용 플래그 (타 OS에서는 0으로 무해)
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def skills_dir() -> Path:
    """스킬 저장 폴더. AISO_SKILLS_DIR(앱이 지정) > ~/.aiso/skills 폴백."""
    env = os.environ.get("AISO_SKILLS_DIR")
    base = Path(env) if env else (Path.home() / ".aiso" / "skills")
    return base


def _valid_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ToolError(
            "스킬 이름은 영문·숫자·한글·밑줄(_)·하이픈(-)만, 1~64자여야 합니다"
            "(공백·점·슬래시 불가). 예: morning_briefing, 알람"
        )
    return name


def _skill_folder(name: str) -> Path:
    """이름을 검증하고 skills_dir 바로 아래 폴더 경로를 돌려준다(밖으로 새지 않음)."""
    name = _valid_name(name)
    base = skills_dir()
    folder = (base / name).resolve()
    if folder.parent != base.resolve():
        raise ToolError(f"잘못된 스킬 경로: {name}")
    return folder


def list_skills() -> list[dict]:
    """만들어진 스킬 목록 [{name, description, created}]. (에이전트 프롬프트 주입·표시용)"""
    base = skills_dir()
    if not base.is_dir():
        return []
    out: list[dict] = []
    for folder in sorted(base.iterdir()):
        if not folder.is_dir() or not (folder / "main.py").is_file():
            continue
        desc, created = "", ""
        manifest = folder / "skill.json"
        if manifest.is_file():
            try:
                m = json.loads(manifest.read_text(encoding="utf-8"))
                desc = str(m.get("description", ""))
                created = str(m.get("created", ""))
            except (ValueError, OSError):
                pass
        out.append({"name": folder.name, "description": desc, "created": created})
    return out


# ── create_skill ────────────────────────────────────────────────────────────
CREATE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_skill",
        "description": (
            "재사용 가능한 자동화 '스킬'(파이썬 프로그램)을 만들어 앱 전용 폴더에 저장한다. "
            "사용자의 프로그램 코드를 대신 작성하는 용도가 아니라, 알람·브리핑·데이터 수집처럼 "
            "반복 실행할 작업을 도구화하는 용도다. 같은 이름이 있으면 덮어써(갱신) 디버깅에 쓴다. "
            "규약: 코드(main.py)는 실행되면 결과를 print로 출력한다. 입력이 필요하면 "
            "sys.argv[1]에 JSON 문자열로 전달되니 `import sys, json; args = json.loads(sys.argv[1]) "
            "if len(sys.argv) > 1 else {}` 로 읽어라. 표준 라이브러리 위주로 작성하되, HTTP 요청은 "
            "urllib.request(표준) 또는 설치된 requests·httpx를 쓴다. 소리는 winsound(윈도우 표준)를 쓴다. "
            "만든 뒤 반드시 run_skill로 실행해 정상 동작을 확인하라(에러 나면 고쳐 다시 만든다)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "스킬 이름(영문·숫자·한글·_·-, 1~64자). 예: morning_briefing",
                },
                "description": {
                    "type": "string",
                    "description": "이 스킬이 하는 일 한 줄 설명(필수). 설정탭·목록에 그대로 표시된다.",
                },
                "code": {
                    "type": "string",
                    "description": "스킬 본문(파이썬). 실행되면 결과를 print로 출력해야 한다.",
                },
            },
            "required": ["name", "description", "code"],
        },
    },
}


def _write_skill(name: str, description: str, code: str) -> str:
    folder = _skill_folder(name)
    if not isinstance(description, str) or not description.strip():
        raise ToolError(
            "스킬 설명(description)을 반드시 한 줄로 적어야 합니다 — 이 스킬이 무엇을 하는지 "
            "설정탭·목록에 그대로 표시됩니다. description을 채워 다시 create_skill 하세요."
        )
    if not isinstance(code, str) or not code.strip():
        raise ToolError("스킬 코드(code)가 비어 있습니다.")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ToolError(f"스킬 코드가 너무 큽니다(최대 {MAX_CODE_BYTES // 1000}KB).")
    # 문법 검증 — 저장 전에 컴파일해 잘못된 코드가 스킬로 굳는 걸 막는다(디버깅 피드백).
    try:
        compile(code, f"{name}/main.py", "exec")
    except SyntaxError as e:
        raise ToolError(
            f"스킬 코드에 문법 오류가 있어 저장하지 않았습니다: {e.msg} "
            f"(줄 {e.lineno}). 고쳐서 다시 create_skill 하세요."
        )
    existed = folder.is_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.py").write_text(code, encoding="utf-8")
    manifest = {
        "name": name,
        "description": (description or "").strip(),
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "entry": "main.py",
    }
    (folder / "skill.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    verb = "갱신함" if existed else "만듦"
    return (
        f"스킬 '{name}'을(를) {verb}. 이제 run_skill(name=\"{name}\")로 실행해 "
        "정상 동작을 확인하세요."
    )


async def create_skill(name: str = "", description: str = "", code: str = "", **_ignore) -> str:
    """스킬을 만들거나 갱신한다(문법 검증 포함)."""
    return await asyncio.to_thread(_write_skill, name, description, code)


# ── run_skill ────────────────────────────────────────────────────────────────
RUN_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_skill",
        "description": (
            "저장된 스킬을 이름으로 실행하고 표준출력·표준에러를 돌려준다. create_skill로 만든 "
            "스킬을 도구처럼 쓴다. 입력이 필요한 스킬이면 args에 객체를 넘겨라(스킬의 sys.argv[1]에 "
            "JSON으로 전달된다). 실행은 승인이 필요할 수 있다(자동 모드 제외)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "실행할 스킬 이름."},
                "args": {
                    "type": "object",
                    "description": "스킬에 넘길 입력(선택). 스킬의 sys.argv[1]에 JSON 문자열로 전달됨.",
                },
            },
            "required": ["name"],
        },
    },
}


def _fmt(name: str, rc: int, out: str, err: str) -> str:
    out, err = (out or "")[-MAX_SKILL_OUTPUT:], (err or "")[-MAX_SKILL_OUTPUT:]
    if rc == 0:
        body = f"출력:\n{out.strip()}" if out.strip() else "(출력 없음)"
        return f"✅ 스킬 '{name}' 실행 성공.\n{body}"
    parts = []
    if out.strip():
        parts.append(out.strip())
    if err.strip() and err.strip() not in "\n".join(parts):
        parts.append(err.strip())
    detail = "\n".join(parts) or "(출력 없음)"
    return (
        f"❌ 스킬 '{name}' 실패 (종료코드 {rc}).\n{detail}\n"
        "→ 위 에러를 보고 create_skill로 코드를 고친 뒤 다시 run_skill 하세요."
    )


def _skill_env() -> dict:
    return sanitized_child_environment(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


def _run_skill_sync(folder: Path, name: str, argjson: str) -> str:
    main_py = folder / "main.py"
    # create_skill이 compile로 검증한 인터프리터(사이드카 = sys.executable)와 '같은' 인터프리터로
    # 실행한다 — 시스템 파이썬을 쓰면 '컴파일은 됐는데 실행은 실패'(버전·패키지 불일치)가 난다.
    try:
        proc = subprocess.Popen(
            [sys.executable, str(main_py), argjson],
            cwd=str(folder),
            stdin=subprocess.DEVNULL,  # 입력 대기(EOF) 방지
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_skill_env(),
            creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_GROUP,
        )
    except FileNotFoundError as e:
        return f"[실행 불가] 파이썬 실행기를 찾을 수 없습니다: {e}"
    try:
        out, err = proc.communicate(timeout=SKILL_RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        # 스킬이 손자 프로세스를 띄우면 직접 자식만 죽여선 파이프가 안 닫혀 무한 대기한다.
        # 트리 전체를 강제 종료(taskkill /T)한 뒤 짧게만 회수한다.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
            env=sanitized_child_environment(),
        )
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        return (
            f"⏱ 스킬 '{name}' 실행이 시간을 초과했습니다({SKILL_RUN_TIMEOUT}초). "
            "무한 루프·입력 대기(input)·백그라운드 프로세스 등을 확인하세요."
        )
    return _fmt(name, proc.returncode, out, err)


async def run_skill(name: str = "", args: dict | None = None, **_ignore) -> str:
    """저장된 스킬을 실행한다(경로 검증은 여기서, 실행은 스레드에서)."""
    folder = _skill_folder(name)
    if not (folder / "main.py").is_file():
        avail = ", ".join(s["name"] for s in list_skills()) or "(없음)"
        raise ToolError(f"'{name}' 스킬이 없습니다. 사용 가능한 스킬: {avail}. 먼저 create_skill로 만드세요.")
    try:
        argjson = json.dumps(args or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        argjson = "{}"
    return await asyncio.to_thread(_run_skill_sync, folder, folder.name, argjson)
