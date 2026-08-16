# -*- coding: utf-8 -*-
"""스킬 시스템 + 역할 제한(.md 한정 쓰기) — 핵심 계약을 고정한다.

1) 문서 쓰기 툴(write_file/edit_file/multi_edit)은 .md만 허용, 코드 확장자는 거부.
2) create_skill: 문법 검증 후 앱 스킬 폴더에 main.py+skill.json 저장(잘못된 문법은 거부).
3) run_skill: 저장된 스킬을 실제 실행해 표준출력을 돌려주고, args를 JSON으로 전달.
4) 스킬 이름 검증: 경로 구분자·상위참조는 거부(스킬 폴더 밖으로 새지 않음).
5) 승인 등급: run_skill/create_skill=임의 실행·영속 코드(모든 모드 승인).
"""
from __future__ import annotations

import asyncio
import sys
import time

import agent
import runskill
import tools
from tools import ToolError


# ── 1) .md 한정 쓰기 게이트 ──────────────────────────────────────────────
def test_write_file_allows_md(tmp_path):
    out = tools.write_file(tmp_path, "notes.md", "# 제목\n내용")
    assert "notes.md" in out
    assert (tmp_path / "notes.md").read_text(encoding="utf-8").startswith("# 제목")


def test_write_file_rejects_code_extensions(tmp_path):
    for name in ("app.py", "index.js", "page.html", "memo.txt", "data.csv"):
        try:
            tools.write_file(tmp_path, name, "x")
            assert False, f"{name} 쓰기가 거부되지 않았다"
        except ToolError as e:
            assert ".md" in str(e)
        assert not (tmp_path / name).exists()  # 파일이 생기지 않아야 한다


def test_edit_file_rejects_non_md(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("print(1)\n", encoding="utf-8")  # 파일은 이미 있어도
    try:
        tools.edit_file(tmp_path, "a.py", "print(1)", "print(2)")
        assert False, "비-.md 편집이 거부되지 않았다"
    except ToolError as e:
        assert ".md" in str(e)
    assert p.read_text(encoding="utf-8") == "print(1)\n"  # 내용 불변


def test_multi_edit_rejects_non_md(tmp_path):
    p = tmp_path / "a.js"
    p.write_text("let x = 1\n", encoding="utf-8")
    try:
        tools.multi_edit(tmp_path, "a.js", [{"old_string": "1", "new_string": "2"}])
        assert False, "비-.md 다중편집이 거부되지 않았다"
    except ToolError as e:
        assert ".md" in str(e)


def test_edit_and_multi_edit_allow_md(tmp_path):
    p = tmp_path / "doc.md"
    p.write_text("hello world\n", encoding="utf-8")
    tools.edit_file(tmp_path, "doc.md", "hello", "안녕")
    assert "안녕 world" in p.read_text(encoding="utf-8")
    tools.multi_edit(tmp_path, "doc.md", [{"old_string": "world", "new_string": "세계"}])
    assert "세계" in p.read_text(encoding="utf-8")


# ── 2) create_skill ──────────────────────────────────────────────────────
def test_create_skill_writes_files(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    code = "print('hello from skill')\n"
    out = asyncio.run(runskill.create_skill(name="greeter", description="인사", code=code))
    assert "greeter" in out
    folder = tmp_path / "greeter"
    assert (folder / "main.py").read_text(encoding="utf-8") == code
    import json
    m = json.loads((folder / "skill.json").read_text(encoding="utf-8"))
    assert m["name"] == "greeter" and m["description"] == "인사" and m["created"]


def test_create_skill_rejects_syntax_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    try:
        asyncio.run(runskill.create_skill(name="broken", description="테스트", code="def f(:\n  pass\n"))
        assert False, "문법 오류 코드가 거부되지 않았다"
    except ToolError as e:
        assert "문법" in str(e)
    assert not (tmp_path / "broken").exists()  # 저장되지 않아야 한다


def test_create_skill_rejects_empty_code(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    try:
        asyncio.run(runskill.create_skill(name="empty", description="설명", code="   "))
        assert False, "빈 코드가 거부되지 않았다"
    except ToolError:
        pass


def test_create_skill_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    asyncio.run(runskill.create_skill(name="s", description="테스트", code="print(1)\n"))
    out = asyncio.run(runskill.create_skill(name="s", description="테스트", code="print(2)\n"))
    assert "갱신" in out
    assert (tmp_path / "s" / "main.py").read_text(encoding="utf-8") == "print(2)\n"


# ── 3) run_skill ─────────────────────────────────────────────────────────
def test_run_skill_executes_and_captures_stdout(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    asyncio.run(runskill.create_skill(name="hi", description="테스트", code="print('SKILL-OK')\n"))
    out = asyncio.run(runskill.run_skill(name="hi"))
    assert "성공" in out
    assert "SKILL-OK" in out


def test_run_skill_receives_args_as_json(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    code = (
        "import sys, json\n"
        "args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}\n"
        "print('name=' + str(args.get('name')))\n"
    )
    asyncio.run(runskill.create_skill(name="echo", description="테스트", code=code))
    out = asyncio.run(runskill.run_skill(name="echo", args={"name": "아이소"}))
    assert "name=아이소" in out


def test_run_skill_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    asyncio.run(runskill.create_skill(name="boom", description="테스트", code="raise ValueError('터짐')\n"))
    out = asyncio.run(runskill.run_skill(name="boom"))
    assert "실패" in out
    assert "터짐" in out or "ValueError" in out


def test_run_skill_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    try:
        asyncio.run(runskill.run_skill(name="nope"))
        assert False, "없는 스킬 실행이 거부되지 않았다"
    except ToolError as e:
        assert "없습니다" in str(e)


# ── 4) 이름 검증 ─────────────────────────────────────────────────────────
def test_skill_name_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    for bad in ("../evil", "a/b", "a\\b", ".", "..", "has space", "dot.name", ""):
        try:
            asyncio.run(runskill.create_skill(name=bad, code="print(1)\n"))
            assert False, f"잘못된 이름 허용됨: {bad!r}"
        except ToolError:
            pass


def test_skill_name_allows_unicode_and_dash(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    for ok in ("morning_briefing", "알람", "daily-report", "s1"):
        asyncio.run(runskill.create_skill(name=ok, description="테스트", code="print('ok')\n"))
        assert (tmp_path / ok / "main.py").exists()


# ── 5) list_skills + 승인 등급 ───────────────────────────────────────────
def test_list_skills(tmp_path, monkeypatch):
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    assert runskill.list_skills() == []
    asyncio.run(runskill.create_skill(name="a", description="첫째", code="print(1)\n"))
    asyncio.run(runskill.create_skill(name="b", description="둘째", code="print(2)\n"))
    names = {s["name"]: s["description"] for s in runskill.list_skills()}
    assert names == {"a": "첫째", "b": "둘째"}


def test_execute_dispatches_skill_tools(tmp_path, monkeypatch):
    """에이전트가 쓰는 실제 경로(toolspec.execute)로 create_skill→run_skill이 도는지 확인.

    ASYNC_PLAIN 디스패치(handler(**args)) + 레지스트리 배선까지 통째로 검증한다.
    """
    import toolspec

    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    create = toolspec.REGISTRY["create_skill"]
    run = toolspec.REGISTRY["run_skill"]

    async def go():
        r1, _ = await toolspec.execute(
            create, tmp_path, "host",
            {"name": "sum2", "description": "합", "code": "print(40 + 2)\n"},
        )
        r2, _ = await toolspec.execute(run, tmp_path, "host", {"name": "sum2"})
        return r1, r2

    made, ran = asyncio.run(go())
    assert "sum2" in made
    assert "42" in ran and "성공" in ran


def test_create_skill_requires_description(tmp_path, monkeypatch):
    """설명(description) 없이는 스킬을 만들 수 없다 — 목록에 '설명 없음'으로 남지 않게."""
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    for bad in ("", "   ", None):
        try:
            asyncio.run(runskill.create_skill(name="nodesc", description=bad, code="print(1)\n"))
            assert False, f"설명 없이 생성됨: {bad!r}"
        except ToolError as e:
            assert "설명" in str(e)
    assert not (tmp_path / "nodesc").exists()
    # description은 스키마 required에도 포함돼야 한다(모델이 반드시 채우도록)
    assert "description" in runskill.CREATE_SKILL_SCHEMA["function"]["parameters"]["required"]


def test_skill_tools_in_agent_tools():
    """create_skill·run_skill이 모델에게 넘기는 스키마 배열에 실제로 노출된다."""
    names = [t["function"]["name"] for t in agent.AGENT_TOOLS]
    assert "create_skill" in names and "run_skill" in names


# ── 리뷰 확정 버그 회귀 테스트 ────────────────────────────────────────────
def test_move_cannot_launder_md_into_code(tmp_path):
    """write_file('.md')→move('.py')로 워크스페이스에 실행코드를 심는 우회를 차단한다."""
    tools.write_file(tmp_path, "note.md", "import os\nos.system('x')\n")
    try:
        tools.move(tmp_path, "note.md", "evil.py")
        assert False, ".md→.py 개명이 차단되지 않았다"
    except ToolError as e:
        assert "개명" in str(e) or ".md" in str(e)
    assert (tmp_path / "note.md").exists() and not (tmp_path / "evil.py").exists()


def test_move_preserves_md_and_allows_existing_code(tmp_path):
    """정당한 이동은 그대로 허용: .md 폴더이동(이름 보존)·.md→.md 개명·기존 비-.md 파일 정리."""
    # (1) .md → 폴더로 이동(이름 보존)
    tools.write_file(tmp_path, "a.md", "hi")
    (tmp_path / "docs").mkdir()
    tools.move(tmp_path, "a.md", "docs/")
    assert (tmp_path / "docs" / "a.md").exists()
    # (2) .md → .md 개명 허용
    tools.move(tmp_path, "docs/a.md", "docs/b.md")
    assert (tmp_path / "docs" / "b.md").exists()
    # (3) 사용자의 기존 .py 파일 정리(이동)는 허용 — 에이전트가 만든 게 아니므로
    (tmp_path / "legacy.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "code").mkdir()
    tools.move(tmp_path, "legacy.py", "code/legacy.py")
    assert (tmp_path / "code" / "legacy.py").exists()


def test_resolve_rejects_ads_colon(tmp_path):
    """NTFS 대체 스트림 접미사('foo.py:x.md')로 확장자 검사를 속이는 경로를 차단한다."""
    try:
        tools.write_file(tmp_path, "foo.py:x.md", "print(1)\n")
        assert False, "콜론 경로가 차단되지 않았다"
    except ToolError as e:
        assert ":" in str(e)
    assert not (tmp_path / "foo.py").exists()


def test_run_skill_uses_sidecar_interpreter(tmp_path, monkeypatch):
    """run_skill은 create_skill이 compile 검증한 인터프리터(sys.executable)로 실행한다."""
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    asyncio.run(runskill.create_skill(name="whoami", description="테스트", code="import sys\nprint(sys.executable)\n"))
    out = asyncio.run(runskill.run_skill(name="whoami"))
    assert sys.executable in out  # 시스템 파이썬이 아니라 이 프로세스의 인터프리터로 실행됨


def test_run_skill_does_not_leak_auth_token(tmp_path, monkeypatch):
    """사이드카 인증 토큰(AISO_AUTH_TOKEN)은 스킬 환경에서 제거된다(최소권한)."""
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    monkeypatch.setenv("AISO_AUTH_TOKEN", "SUPERSECRET-123")
    code = "import os\nprint('token=' + str(os.environ.get('AISO_AUTH_TOKEN')))\n"
    asyncio.run(runskill.create_skill(name="leak", description="테스트", code=code))
    out = asyncio.run(runskill.run_skill(name="leak"))
    assert "SUPERSECRET-123" not in out
    assert "token=None" in out


def test_run_skill_timeout_kills_process_tree(tmp_path, monkeypatch):
    """스킬이 손자 프로세스를 띄워도 타임아웃에 트리 전체를 죽여 빠르게 반환한다(무한 대기 방지)."""
    monkeypatch.setenv("AISO_SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(runskill, "SKILL_RUN_TIMEOUT", 2)
    # 손자(30초 sleep)를 띄운 뒤 자신도 오래 잔다 — 예전 구현이면 손자가 끝날 때까지 대기(30s+).
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n"
    )
    asyncio.run(runskill.create_skill(name="spawner", description="테스트", code=code))
    t0 = time.monotonic()
    out = asyncio.run(runskill.run_skill(name="spawner"))
    elapsed = time.monotonic() - t0
    assert "초과" in out
    assert elapsed < 20, f"타임아웃이 트리를 못 죽여 {elapsed:.1f}s 걸림(손자 대기 의심)"


def test_skill_approval_grades():
    # Skills are persistent arbitrary code, so read/manual require a gate.
    assert agent.needs_approval("run_skill", "read") is True
    assert agent.needs_approval("run_skill", "manual") is True
    assert agent.needs_approval("run_skill", "auto") is False
    # Creating a skill stores executable code outside the workspace as well.
    assert agent.needs_approval("create_skill", "read") is True
    assert agent.needs_approval("create_skill", "manual") is True
    assert agent.needs_approval("create_skill", "auto") is False
