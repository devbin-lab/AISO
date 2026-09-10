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


# git 이 실제로 내보내는 모양: `<ref><TAB><sha>` (for-each-ref --format)
REFS = "".join(
    f"{ref}{chr(9)}{sha}" + chr(10)
    for ref, sha in [
        ("refs/heads/main", "aaaa11112222"),
        ("refs/heads/feature/login", "bbbb11112222"),
        ("refs/remotes/origin/HEAD", "aaaa11112222"),
        ("refs/remotes/origin/main", "aaaa11112222"),
        ("refs/remotes/origin/feature/login", "bbbb11112222"),
    ]
)


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


# ── 모든 브랜치 한 번에 ──────────────────────────────────────────────────
# 각자 자기 브랜치에서 일하는 팀은 브랜치 하나만 봐서는 따라갈 수 없다. origin/main 만
# 보면 머지 전까지 보고서가 계속 비어 있고, 브랜치마다 예약을 걸면 새 브랜치가 생길
# 때마다 사람이 등록해 줘야 한다.

def _recording_git(monkeypatch, refs: str, log: str = "", *, missing=(), fetch_error=""):
    """git 호출을 기록하는 대역. 어떤 인자로 log 를 불렀는지가 이 모드의 핵심이다."""
    calls: list[list[str]] = []

    async def fake(_repo, args, _timeout):
        calls.append(list(args))
        if args[0] == "fetch":
            if fetch_error:
                raise gitreport.GitReportError(fetch_error)
            return ""
        if args[0] == "for-each-ref":
            return refs
        if args[0] == "rev-parse":
            return "aaaa11112222" + chr(10)
        if args[0] == "cat-file":
            target = args[2].split("^")[0]
            if target in missing:
                raise gitreport.GitReportError("존재하지 않는 커밋")
            return ""
        if args[0] == "log":
            return log
        raise AssertionError(f"예상하지 못한 git 호출: {args}")

    monkeypatch.setattr(gitreport, "_git", fake)
    return calls


def _log_call(calls):
    return next(args for args in calls if args[0] == "log")


ONE_COMMIT = commit_chunk(
    "abc123", "Alice", "alice@example.com", "2026-09-10 12:00", "Feat : network", "",
    ["20" + chr(9) + "1" + chr(9) + "Assets/Net.cs"],
)


def test_every_branch_is_read_against_every_known_cursor(tmp_path, monkeypatch):
    """`git log <지금 ref 전부> --not <지난 커서 전부>` 가 이 모드의 전부다.

    이 형태라야 새로 생긴 브랜치에서 그 브랜치만의 커밋이 나오고(갈라져 나온 지점까지는
    다른 커서로 닿는다), 머지된 커밋이 두 브랜치에 걸쳐도 한 번만 나온다.
    """
    calls = _recording_git(monkeypatch, REFS, ONE_COMMIT)
    report = asyncio.run(gitreport.collect_all_branches(
        _repo(tmp_path), {"origin/main": "aaaa00000000"}, fetch=False,
    ))
    args = _log_call(calls)
    assert "aaaa11112222" in args and "bbbb11112222" in args, "원격 ref 를 모두 넣는다"
    assert args[args.index("--not") + 1] == "aaaa00000000"
    assert len(report.commits) == 1


def test_the_default_branch_alias_is_not_counted_twice(tmp_path, monkeypatch):
    """origin/HEAD 는 origin/main 과 같은 커밋을 가리키는 별칭이다."""
    calls = _recording_git(monkeypatch, REFS, ONE_COMMIT)
    report = asyncio.run(gitreport.collect_all_branches(
        _repo(tmp_path), {"origin/main": "aaaa00000000"}, fetch=False,
    ))
    assert "origin/HEAD" not in report.ref_heads
    assert sorted(report.ref_heads) == ["origin/feature/login", "origin/main"]


def test_without_any_cursor_it_takes_a_baseline_instead_of_dumping_history(tmp_path, monkeypatch):
    """등록 직후 첫 회차. 저장소 전체 역사를 쏟아내면 보고서가 아니라 재난이다."""
    calls = _recording_git(monkeypatch, REFS, ONE_COMMIT)
    report = asyncio.run(gitreport.collect_all_branches(_repo(tmp_path), {}, fetch=False))
    assert report.commits == []
    assert report.ref_heads == {
        "origin/main": "aaaa11112222", "origin/feature/login": "bbbb11112222",
    }
    assert not any(args[0] == "log" for args in calls), "볼 것이 없으면 로그도 읽지 않는다"


def test_a_cursor_that_vanished_is_dropped_and_said_out_loud(tmp_path, monkeypatch):
    """강제 푸시·리베이스로 기준이 사라져도 나머지 커서가 공통 역사를 걸러 준다."""
    calls = _recording_git(
        monkeypatch, REFS, ONE_COMMIT, missing=("dddd00000000",),
    )
    report = asyncio.run(gitreport.collect_all_branches(
        _repo(tmp_path),
        {"origin/main": "aaaa00000000", "origin/feature/login": "dddd00000000"},
        fetch=False,
    ))
    args = _log_call(calls)
    assert "dddd00000000" not in args
    assert "aaaa00000000" in args
    assert "사라진 기준 커밋" in report.fetch_warning


def test_only_the_branches_that_moved_are_named(tmp_path, monkeypatch):
    """제목에 적히는 값이다. 움직이지 않은 브랜치까지 적으면 어디서 벌어진 일인지 흐려진다."""
    _recording_git(monkeypatch, REFS, ONE_COMMIT)
    report = asyncio.run(gitreport.collect_all_branches(
        _repo(tmp_path),
        {"origin/main": "aaaa11112222", "origin/feature/login": "cccc00000000"},
        fetch=False,
    ))
    assert report.branch == "origin/feature/login"


def test_a_repository_without_a_remote_falls_back_to_local_branches(tmp_path, monkeypatch):
    """원격이 아예 없는 저장소는 로컬 브랜치가 전부다 — 아니면 볼 것이 없다."""
    refs = "".join(
        f"{ref}{chr(9)}{sha}" + chr(10)
        for ref, sha in [("refs/heads/main", "aaaa11112222"), ("refs/heads/wip", "eeee11112222")]
    )
    _recording_git(monkeypatch, refs, ONE_COMMIT)
    report = asyncio.run(gitreport.collect_all_branches(_repo(tmp_path), {}, fetch=False))
    assert sorted(report.ref_heads) == ["main", "wip"]


def test_local_branches_are_ignored_when_a_remote_exists(tmp_path, monkeypatch):
    """로컬 브랜치는 아직 아무에게도 공유되지 않은 작업이다."""
    _recording_git(monkeypatch, REFS, ONE_COMMIT)
    report = asyncio.run(gitreport.collect_all_branches(_repo(tmp_path), {}, fetch=False))
    assert all(name.startswith("origin/") for name in report.ref_heads)


def test_a_repository_with_no_branches_at_all_is_an_error(tmp_path, monkeypatch):
    _recording_git(monkeypatch, "")
    with pytest.raises(gitreport.GitReportError):
        asyncio.run(gitreport.collect_all_branches(_repo(tmp_path), {}, fetch=False))


def test_a_failed_fetch_still_reports_what_is_already_local(tmp_path, monkeypatch):
    _recording_git(monkeypatch, REFS, ONE_COMMIT, fetch_error="원격에 접근할 수 없습니다")
    report = asyncio.run(gitreport.collect_all_branches(
        _repo(tmp_path), {"origin/main": "aaaa00000000"},
    ))
    assert len(report.commits) == 1
    assert "원격에 접근할 수 없습니다" in report.fetch_warning


# ── 미리보기 수집 ────────────────────────────────────────────────────────

def test_a_preview_reads_the_newest_commits_and_ignores_cursors(tmp_path, monkeypatch):
    calls = _recording_git(monkeypatch, REFS, ONE_COMMIT)
    report = asyncio.run(gitreport.collect_recent(_repo(tmp_path), "origin/main", limit=5, fetch=False))
    args = _log_call(calls)
    assert "--not" not in args, "미리보기는 커서를 읽지 않는다"
    assert "--max-count=5" in args
    assert "origin/main" in args
    assert report.branch == "origin/main"


def test_a_preview_of_all_branches_covers_every_ref(tmp_path, monkeypatch):
    calls = _recording_git(monkeypatch, REFS, ONE_COMMIT)
    report = asyncio.run(
        gitreport.collect_recent(_repo(tmp_path), gitreport.ALL_BRANCHES, fetch=False)
    )
    args = _log_call(calls)
    assert "aaaa11112222" in args and "bbbb11112222" in args
    assert report.branch == gitreport.ALL_BRANCHES


def test_a_preview_of_a_branch_that_does_not_exist_is_refused(tmp_path, monkeypatch):
    """log 에 그대로 넘기면 알아보기 어려운 오류가 나온다 — ref 부터 확인한다."""
    async def fake(_repo, args, _timeout):
        if args[0] == "rev-parse":
            raise gitreport.GitReportError("unknown revision")
        return REFS if args[0] == "for-each-ref" else ""

    monkeypatch.setattr(gitreport, "_git", fake)
    with pytest.raises(gitreport.GitReportError):
        asyncio.run(gitreport.collect_recent(_repo(tmp_path), "oigin/main", fetch=False))


def test_a_preview_never_asks_for_more_than_the_report_cap(tmp_path, monkeypatch):
    calls = _recording_git(monkeypatch, REFS, ONE_COMMIT)
    asyncio.run(gitreport.collect_recent(_repo(tmp_path), "origin/main", limit=10_000, fetch=False))
    assert f"--max-count={gitreport.MAX_COMMITS}" in _log_call(calls)
