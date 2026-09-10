# -*- coding: utf-8 -*-
"""git 로그 파싱의 계약.

보고서의 모든 수치가 여기서 나온다. 여기가 한 줄이라도 틀리면 보고서는 조용히
틀린 수를 말하고, 사람은 그걸 확인할 방법이 없다. 그래서 실제로 겪은 두 결함을
회귀로 못박는다 — 본문에 파일이 섞여 들어간 것, 상한을 넘긴 파일의 줄 수가 사라진 것.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gitreport  # noqa: E402

NUL = "\x00"
REC = "\x1e"
FLD = "\x1f"
STX = "\x02"


def commit_chunk(sha, name, email, when, subject, body, numstat_entries):
    """git 이 실제로 내보내는 모양 그대로 만든다: 본문 뒤에 곧바로 첫 numstat 줄이 붙는다."""
    head = FLD.join([sha, name, email, when, subject, body])
    stream = NUL.join(numstat_entries) + NUL if numstat_entries else ""
    return f"{REC}{head}{STX}\n\n{stream}"


def test_parses_one_commit_with_files():
    raw = commit_chunk(
        "abc123def4567", "Alice", "alice@example.com", "2026-09-06 20:40",
        "Add player controller", "본문 한 줄",
        ["424\t0\tAssets/Scripts/Player/PlayerController.cs", "27\t0\tAssets/Scripts/Player/MovementState.cs"],
    )
    commits = gitreport.parse_log(raw)
    assert len(commits) == 1
    commit = commits[0]
    assert commit.sha == "abc123def456"
    assert commit.author_name == "Alice"
    assert commit.subject == "Add player controller"
    assert commit.body == "본문 한 줄"
    assert len(commit.files) == 2
    assert commit.added == 451
    assert commit.removed == 0


def test_a_body_with_blank_lines_does_not_swallow_the_first_file():
    """실제로 겪은 결함. 본문에 빈 줄이 있으면 첫 numstat 줄이 본문에 섞여 파일 하나가 사라졌다."""
    body = "- 변경 이유\n\nCo-Authored-By: Someone <a@b.c>"
    raw = commit_chunk(
        "aaa111bbb2223", "Bob", "bob@example.com", "2026-09-06 08:13",
        "Add .gitattributes and ignore .vscode", body,
        ["47\t0\t.gitattributes", "3\t0\t.gitignore"],
    )
    commit = gitreport.parse_log(raw)[0]
    assert len(commit.files) == 2, "본문 뒤 첫 파일이 삼켜지면 안 된다"
    assert commit.added == 50
    assert "Co-Authored-By" in commit.body
    assert ".gitattributes" not in commit.body


def test_b_files_beyond_the_cap_keep_their_line_counts():
    """실제로 겪은 결함. 경로는 버려도 합계는 버리면 안 된다 — 6,634줄이 6,186줄로 줄었다."""
    entries = [f"10\t1\tAssets/File{i}.meta" for i in range(gitreport.MAX_FILES_PER_COMMIT + 5)]
    raw = commit_chunk("ccc333ddd4445", "Carol", "c@e.com", "2026-09-04 23:11", "Init", "", entries)
    commit = gitreport.parse_log(raw)[0]
    assert len(commit.files) == gitreport.MAX_FILES_PER_COMMIT
    assert commit.truncated_files == 5
    assert commit.added == 10 * len(entries), "잘린 파일의 줄 수까지 합쳐야 한다"
    assert commit.removed == 1 * len(entries)


def test_binary_files_do_not_become_zero_by_accident():
    raw = commit_chunk(
        "ddd444eee5556", "Dan", "d@e.com", "2026-09-06 10:00", "Add texture", "",
        ["-\t-\tAssets/Art/tex.png", "5\t2\tREADME.md"],
    )
    commit = gitreport.parse_log(raw)[0]
    assert len(commit.files) == 2
    assert commit.added == 5
    assert commit.removed == 2


def test_renamed_files_report_the_new_path():
    raw = commit_chunk(
        "eee555fff6667", "Eve", "e@e.com", "2026-09-06 11:00", "Rename", "",
        ["3\t1\t", "old/name.cs", "new/name.cs"],
    )
    commit = gitreport.parse_log(raw)[0]
    assert len(commit.files) == 1
    assert commit.files[0].path == "new/name.cs"
    assert commit.added == 3


def test_a_commit_with_no_file_changes_is_still_a_commit():
    raw = commit_chunk("fff666aaa7778", "Fay", "f@e.com", "2026-09-06 12:00", "Empty", "", [])
    commits = gitreport.parse_log(raw)
    assert len(commits) == 1
    assert commits[0].files == []
    assert commits[0].added == 0


def test_multiple_commits_keep_their_order():
    raw = (
        commit_chunk("111aaa222bbb3", "A", "a@e.com", "t1", "first", "", ["1\t0\ta.txt"])
        + commit_chunk("222bbb333ccc4", "B", "b@e.com", "t2", "second", "", ["2\t0\tb.txt"])
    )
    commits = gitreport.parse_log(raw)
    assert [c.subject for c in commits] == ["first", "second"]


def test_empty_output_is_not_an_error():
    assert gitreport.parse_log("") == []


# ── 참여자 묶기 ────────────────────────────────────────────────────────────

def report_with(authors):
    commits = [
        gitreport.Commit(sha=f"s{i}", author_name=name, author_email=email,
                         when="t", subject="s", body="")
        for i, (name, email) in enumerate(authors)
    ]
    return gitreport.RepoReport(repo_path="/r", branch="main", head="h", commits=commits)


def test_the_same_person_with_two_email_spellings_counts_once():
    """실제 저장소에서 alice@ 와 Alice@ 로 갈려 2명이 3명으로 세어졌다."""
    report = report_with([
        ("AliceNov (JunseoLee)", "alice@alicenov.com"),
        ("AliceInNovember", "Alice@alicenov.com"),
        ("devbin0318", "devbin0318@gmail.com"),
    ])
    authors = report.authors
    assert len(authors) == 2
    assert authors[0] == ("AliceInNovember", 2), "표시 이름은 가장 최근 것을 쓴다"
    assert authors[1] == ("devbin0318", 1)


def test_author_order_follows_first_appearance():
    report = report_with([("B", "b@e.com"), ("A", "a@e.com"), ("B", "b@e.com")])
    assert [name for name, _ in report.authors] == ["B", "A"]


def test_surrounding_whitespace_in_an_email_does_not_split_a_person():
    report = report_with([("A", " a@e.com "), ("A", "a@e.com")])
    assert len(report.authors) == 1


# ── 코드와 생성물 구분 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("path,generated", [
    ("Assets/Scripts/Player/PlayerController.cs", False),
    ("README.md", False),
    (".gitattributes", False),
    ("Assets/Scripts/Player.meta", True),
    ("Assets/Scenes/Main.unity", True),
    ("Assets/Prefabs/Player.prefab", True),
    ("ProjectSettings/ProjectVersion.txt", True),
    ("Packages/manifest.json", True),
    ("Assets/Art/tex.PNG", True),
])
def test_generated_files_are_recognised(path, generated):
    assert gitreport.CommitFile(path=path, added=1, removed=0).generated is generated


def test_code_lines_exclude_generated_output():
    """유니티는 줄 수의 90%가 에셋이다. 구분하지 않으면 실제 작업이 묻힌다."""
    commit = gitreport.Commit(
        sha="s", author_name="A", author_email="a@e.com", when="t", subject="s", body="",
        files=[
            gitreport.CommitFile("Assets/Scripts/P.cs", 424, 0),
            gitreport.CommitFile("Assets/Scenes/M.unity", 3000, 0),
            gitreport.CommitFile("Assets/Scripts/P.cs.meta", 8, 0),
        ],
    )
    report = gitreport.RepoReport(repo_path="/r", branch="main", head="h", commits=[commit])
    assert report.code_lines == (424, 0)
    assert report.total_lines == (3432, 0)


# ── 모델에게 넘길 사실 묶음 ────────────────────────────────────────────────

def test_facts_are_empty_when_nothing_changed():
    report = gitreport.RepoReport(repo_path="/r", branch="main", head="h", commits=[])
    assert gitreport.build_facts(report) == ""


def test_facts_state_truncation_instead_of_hiding_it():
    commit = gitreport.Commit(sha="s", author_name="A", author_email="a@e.com",
                              when="t", subject="s", body="")
    report = gitreport.RepoReport(repo_path="/r", branch="main", head="h",
                                  commits=[commit], truncated=True)
    assert str(gitreport.MAX_COMMITS) in gitreport.build_facts(report)


def test_facts_carry_the_fetch_warning():
    commit = gitreport.Commit(sha="s", author_name="A", author_email="a@e.com",
                              when="t", subject="s", body="")
    report = gitreport.RepoReport(repo_path="/r", branch="main", head="h",
                                  commits=[commit], fetch_warning="네트워크 없음")
    assert "네트워크 없음" in gitreport.build_facts(report)


# ── 경로 검증 ──────────────────────────────────────────────────────────────

def test_a_relative_path_is_refused():
    with pytest.raises(gitreport.GitReportError):
        gitreport.assert_repository("some/relative/path")


def test_a_missing_folder_is_refused():
    with pytest.raises(gitreport.GitReportError):
        gitreport.assert_repository("C:/definitely/not/here/xyz")


def test_a_folder_without_git_is_refused(tmp_path):
    with pytest.raises(gitreport.GitReportError):
        gitreport.assert_repository(str(tmp_path))


def test_a_real_repository_is_accepted(tmp_path):
    (tmp_path / ".git").mkdir()
    assert gitreport.assert_repository(str(tmp_path)) == tmp_path


# ── 브랜치 목록 ─────────────────────────────────────────────────────────
# 등록 화면의 드롭다운을 채우는 값이다. 사람이 브랜치를 손으로 적으면 `main` 과
# `origin/main` 을 혼동하는데, 그 실수는 "fetch 는 성공하는데 보고는 영원히 비어 있음"
# 으로만 드러난다 — 새 커밋이 없을 때 침묵하는 것이 정상 동작이라 아무도 눈치채지 못한다.

def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


def _fake_git(monkeypatch, refs: str, *, current: str = "main", fetch_error: str = ""):
    async def fake(_repo, args, _timeout):
        if args[0] == "fetch":
            if fetch_error:
                raise gitreport.GitReportError(fetch_error)
            return ""
        if args[0] == "rev-parse":
            return current + "\n"
        if args[0] == "for-each-ref":
            return refs
        raise AssertionError(f"예상하지 못한 git 호출: {args}")

    monkeypatch.setattr(gitreport, "_git", fake)


REFS = """refs/heads/main
refs/heads/feature/login
refs/remotes/origin/HEAD
refs/remotes/origin/main
refs/remotes/origin/feature/login
"""


def test_local_and_remote_branches_are_told_apart(tmp_path, monkeypatch):
    """슬래시가 든 로컬 브랜치를 원격으로 오분류하면 목록이 거짓말을 한다."""
    _fake_git(monkeypatch, REFS)
    refs = asyncio.run(gitreport.list_refs(_repo(tmp_path)))
    assert refs["local"] == ["main", "feature/login"]
    assert refs["remote"] == ["origin/main", "origin/feature/login"]


def test_the_default_branch_alias_is_not_offered(tmp_path, monkeypatch):
    """origin/HEAD 는 별칭이라, 고르면 원격 기본 브랜치가 바뀔 때 보는 대상이 조용히 바뀐다."""
    _fake_git(monkeypatch, REFS)
    refs = asyncio.run(gitreport.list_refs(_repo(tmp_path)))
    assert all(not name.endswith("/HEAD") for name in refs["remote"])


def test_the_remote_counterpart_of_the_checked_out_branch_is_recommended(tmp_path, monkeypatch):
    _fake_git(monkeypatch, REFS, current="feature/login")
    refs = asyncio.run(gitreport.list_refs(_repo(tmp_path)))
    assert refs["recommended"] == "origin/feature/login"


def test_a_local_branch_is_never_recommended_when_a_remote_exists(tmp_path, monkeypatch):
    """fetch 가 움직이는 것은 origin/* 다. 로컬을 권하면 남의 커밋이 하나도 안 잡힌다."""
    _fake_git(monkeypatch, REFS, current="지역전용")
    refs = asyncio.run(gitreport.list_refs(_repo(tmp_path)))
    assert refs["recommended"] == "origin/main"


def test_without_any_remote_the_checked_out_branch_is_recommended(tmp_path, monkeypatch):
    _fake_git(monkeypatch, "refs/heads/main\n", current="main")
    refs = asyncio.run(gitreport.list_refs(_repo(tmp_path)))
    assert refs["recommended"] == "main"
    assert refs["remote"] == []


def test_a_detached_head_is_not_treated_as_a_branch_name(tmp_path, monkeypatch):
    _fake_git(monkeypatch, REFS, current="HEAD")
    refs = asyncio.run(gitreport.list_refs(_repo(tmp_path)))
    assert refs["current"] == ""
    assert refs["recommended"] == "origin/main"


def test_a_failed_refresh_still_returns_the_list_and_says_so(tmp_path, monkeypatch):
    """네트워크가 없다고 등록을 막을 이유는 없다. 다만 목록이 오래됐다는 사실은 밝힌다."""
    _fake_git(monkeypatch, REFS, fetch_error="원격에 접근할 수 없습니다")
    refs = asyncio.run(gitreport.list_refs(_repo(tmp_path)))
    assert refs["remote"] == ["origin/main", "origin/feature/login"]
    assert "오래된" in refs["warning"]


def test_skipping_the_refresh_never_touches_the_network(tmp_path, monkeypatch):
    _fake_git(monkeypatch, REFS, fetch_error="불러선 안 되는 fetch")
    refs = asyncio.run(gitreport.list_refs(_repo(tmp_path), refresh=False))
    assert refs["warning"] == ""


def test_a_folder_that_is_not_a_repository_is_refused_before_any_git_call(tmp_path):
    with pytest.raises(gitreport.GitReportError):
        asyncio.run(gitreport.list_refs(str(tmp_path)))
