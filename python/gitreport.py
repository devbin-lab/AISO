# -*- coding: utf-8 -*-
"""로컬 클론에서 보고서 재료를 읽는다.

여기서 하는 일은 **사실 수집**뿐이다. 분류와 서술은 모델이 하고, 이 모듈은 git 이
보증하는 값만 돌려준다 — 커밋 해시·작성자·시각·제목·본문·파일 목록·줄 수.
그래야 보고서에서 지어낸 문장과 확인된 수치를 구분할 수 있다.

명령은 언제나 인자 배열로 실행한다. 문자열로 조립해 셸에 넘기면 저장소 경로나
브랜치 이름에 든 문자가 명령이 된다 — 예약 작업은 사람이 보지 않는 동안 돌기 때문에
그 경로는 절대 열어 두지 않는다.
"""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# 유니티 저장소는 커밋 하나가 수백 파일을 건드린다. 보고서에 다 실을 수 없고 실을 이유도 없다.
MAX_COMMITS = 100
MAX_FILES_PER_COMMIT = 40
MAX_SUBJECT = 200
MAX_BODY = 4000
FETCH_TIMEOUT_S = 90
LOG_TIMEOUT_S = 30
# 실제로 사람이 쓴 코드와 도구가 만들어 낸 산출물을 가른다. 유니티에서는 줄 수의 90%가
# 후자라, 구분하지 않으면 "무엇을 했는가"가 에셋 줄 수에 묻힌다.
GENERATED_SUFFIXES = ('.meta', '.asset', '.unity', '.prefab', '.mat', '.anim', '.controller', '.fbx', '.png', '.psd', '.tga', '.wav', '.mp3', '.ogg')
GENERATED_DIRS = ('ProjectSettings/', 'Packages/', 'UserSettings/')

_NUL = '\x00'
_RECORD = '\x1e'
_FIELD = '\x1f'
# 본문 끝을 명시적으로 찍는다. `-z --numstat` 은 첫 numstat 줄을 본문 바로 뒤에 붙여
# 내보내는데, 본문 안에도 빈 줄이 있어서 빈 줄로는 둘을 가를 수 없다.
_BODY_END = '\x02'


class GitReportError(Exception):
    """저장소를 읽지 못했다. 사용자에게 그대로 보여도 되는 문구만 담는다."""


@dataclass
class CommitFile:
    path: str
    added: int
    removed: int

    @property
    def generated(self) -> bool:
        lower = self.path.lower()
        if any(lower.endswith(suffix) for suffix in GENERATED_SUFFIXES):
            return True
        return any(self.path.startswith(prefix) for prefix in GENERATED_DIRS)


@dataclass
class Commit:
    sha: str
    author_name: str
    author_email: str
    when: str
    subject: str
    body: str
    files: list[CommitFile] = field(default_factory=list)
    truncated_files: int = 0
    # 목록에 싣지 않은 파일의 줄 수. 경로는 버려도 **합계는 버리면 안 된다** —
    # 유니티 초기 반입처럼 파일이 50개인 커밋에서 40개까지만 세면 6,634줄이 6,186줄로
    # 줄어들어 보고서가 조용히 틀린 수를 말하게 된다.
    truncated_added: int = 0
    truncated_removed: int = 0

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files) + self.truncated_added

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files) + self.truncated_removed


@dataclass
class RepoReport:
    repo_path: str
    branch: str
    head: str
    commits: list[Commit]
    """커서 이후 커밋이 MAX_COMMITS 를 넘어 잘렸으면 그 사실을 알린다 — 조용히 빠뜨리지 않는다."""
    truncated: bool = False
    fetch_warning: str = ''

    @property
    def authors(self) -> list[tuple[str, int]]:
        """이메일을 소문자로 묶은 참여자와 커밋 수.

        같은 사람이 컴퓨터마다 다른 표기로 커밋하는 일이 흔하다(alice@ 와 Alice@).
        이메일 주소는 실무에서 대소문자를 가리지 않으므로 소문자로 맞춰 센다.
        표시 이름은 그 사람이 **가장 최근에** 쓴 것을 쓴다.
        """
        order: list[str] = []
        counts: dict[str, int] = {}
        names: dict[str, str] = {}
        for commit in self.commits:
            key = commit.author_email.strip().lower()
            if key not in counts:
                counts[key] = 0
                order.append(key)
            counts[key] += 1
            names[key] = commit.author_name
        return [(names[key], counts[key]) for key in order]

    @property
    def code_lines(self) -> tuple[int, int]:
        """사람이 쓴 코드의 추가·삭제 줄 수.

        목록에서 잘린 파일은 성격을 알 수 없어 여기에 넣지 않는다. 파일이 40개를 넘는
        커밋은 대개 에셋 반입이라, 코드로 세는 쪽이 틀릴 위험이 더 크다.
        """
        added = sum(f.added for c in self.commits for f in c.files if not f.generated)
        removed = sum(f.removed for c in self.commits for f in c.files if not f.generated)
        return added, removed

    @property
    def total_lines(self) -> tuple[int, int]:
        added = sum(c.added for c in self.commits)
        removed = sum(c.removed for c in self.commits)
        return added, removed


def _run_git_sync(repo: Path, args: list[str], timeout: int) -> str:
    try:
        completed = subprocess.run(
            ['git', '-C', str(repo), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise GitReportError('git 을 찾을 수 없습니다. git 이 설치되어 있는지 확인하세요.') from error
    except subprocess.TimeoutExpired as error:
        raise GitReportError(f'git 명령이 {timeout}초를 넘겨 중단했습니다.') from error
    if completed.returncode != 0:
        detail = completed.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise GitReportError(f"git 명령 실패: {detail[-1] if detail else f'종료 코드 {completed.returncode}'}")
    return completed.stdout.decode('utf-8', 'replace')


async def _git(repo: Path, args: list[str], timeout: int) -> str:
    return await asyncio.to_thread(_run_git_sync, repo, args, timeout)


def assert_repository(repo_path: str) -> Path:
    """git 저장소인지 확인하고 절대 경로를 돌려준다. 등록 시점에 한 번 부른다."""
    candidate = str(repo_path or '').strip()
    if not candidate:
        raise GitReportError('저장소 경로를 입력해 주세요.')
    repo = Path(candidate).expanduser()
    if not repo.is_absolute():
        raise GitReportError('저장소 경로는 전체 경로여야 합니다.')
    if not repo.is_dir():
        raise GitReportError(f'폴더를 찾을 수 없습니다: {repo}')
    if not (repo / '.git').exists():
        raise GitReportError(f'git 저장소가 아닙니다: {repo}')
    return repo


def parse_log(raw: str) -> list[Commit]:
    """`--numstat -z` 출력에서 커밋 목록을 만든다.

    출력 구조는 이렇다.
        <RS> sha <US> 이름 <US> 메일 <US> 시각 <US> 제목 <US> 본문 <STX>
        빈 줄, 그다음 numstat 줄들이 NUL 로 끝나며 이어진다.

    본문 뒤에 첫 numstat 줄이 곧바로 붙으므로 본문 끝을 STX 로 찍어 가른다. 빈 줄로
    가르려 하면 본문 안의 빈 줄에 걸려 파일 하나가 통째로 본문에 섞인다 — 실제로 그래서
    50개 파일 커밋이 49개로 세어졌다.

    파일 이름에 줄바꿈이나 따옴표가 들어갈 수 있어 NUL 구분을 쓴다. 사람이 정한 이름을
    구분자로 믿으면 파일 하나 때문에 보고서 전체가 어긋난다.
    """
    commits: list[Commit] = []
    for chunk in raw.split(_RECORD):
        if not chunk.strip():
            continue
        if _BODY_END not in chunk:
            continue
        head_text, rest = chunk.split(_BODY_END, 1)
        header = head_text.split(_FIELD)
        if len(header) < 6:
            continue
        commit = Commit(
            sha=header[0].strip()[:12],
            author_name=header[1],
            author_email=header[2],
            when=header[3],
            subject=header[4][:MAX_SUBJECT],
            body=header[5][:MAX_BODY].strip(),
        )
        tokens = rest.split(_NUL)
        index = 0
        while index < len(tokens):
            entry = tokens[index].strip('\n')
            index += 1
            if not entry:
                continue
            parts = entry.split('\t')
            if len(parts) < 3:
                continue
            added_raw, removed_raw = parts[0], parts[1]
            path = '\t'.join(parts[2:])
            # 이름이 바뀐 파일은 경로 자리가 비고 옛 경로·새 경로가 뒤이어 온다.
            if not path:
                index += 1  # 옛 경로는 버린다 — 보고서는 지금 이름만 쓴다
                path = tokens[index] if index < len(tokens) else ''
                index += 1
            if not path:
                continue
            # 바이너리는 '-' 로 나온다. 줄 수를 셀 수 없다는 뜻이지 0 이 아니다.
            added = int(added_raw) if added_raw.isdigit() else 0
            removed = int(removed_raw) if removed_raw.isdigit() else 0
            if len(commit.files) >= MAX_FILES_PER_COMMIT:
                commit.truncated_files += 1
                commit.truncated_added += added
                commit.truncated_removed += removed
                continue
            commit.files.append(CommitFile(path=path, added=added, removed=removed))
        commits.append(commit)
    return commits


async def collect(
    repo_path: str,
    branch: str,
    since_commit: str,
    *,
    fetch: bool = True,
) -> RepoReport:
    """커서 이후의 커밋을 모은다. 커서가 비어 있으면 브랜치의 최신 1건만 본다.

    처음 등록할 때 저장소 전체 역사를 쏟아내지 않기 위해서다. 등록 직후의 첫 보고는
    "여기서부터 봅니다"라는 기준점을 잡는 것이 목적이다.
    """
    repo = assert_repository(repo_path)
    target = str(branch or '').strip() or 'HEAD'
    warning = ''
    if fetch:
        try:
            await _git(repo, ['fetch', '--quiet'], FETCH_TIMEOUT_S)
        except GitReportError as error:
            # 네트워크가 없어도 로컬에 이미 받아 둔 커밋은 보고할 수 있다. 다만 조용히
            # 넘어가면 "새 커밋이 없다"와 "못 받아왔다"를 구분할 수 없으므로 남긴다.
            warning = str(error)

    head = (await _git(repo, ['rev-parse', target], LOG_TIMEOUT_S)).strip()[:12]
    cursor = str(since_commit or '').strip()
    if cursor:
        try:
            await _git(repo, ['cat-file', '-e', f'{cursor}^{{commit}}'], LOG_TIMEOUT_S)
            span = f'{cursor}..{target}'
        except GitReportError:
            # 커서가 사라졌다(강제 푸시·리베이스). 처음부터 다시 쏟아내지 않고 최신 1건만 본다.
            span = f'{target}~1..{target}'
            warning = (warning + ' · ' if warning else '') + '이전 기준 커밋을 찾지 못해 최신 커밋부터 다시 시작합니다.'
    else:
        span = f'{target}~1..{target}'

    fmt = f'%H{_FIELD}%an{_FIELD}%ae{_FIELD}%ad{_FIELD}%s{_FIELD}%b{_BODY_END}'
    raw = await _git(repo, [
        'log', span, '--reverse', f'--max-count={MAX_COMMITS + 1}',
        '--numstat', '-z', '--date=format:%Y-%m-%d %H:%M',
        f'--pretty=format:{_RECORD}{fmt}',
    ], LOG_TIMEOUT_S)
    commits = parse_log(raw)
    truncated = len(commits) > MAX_COMMITS
    if truncated:
        commits = commits[-MAX_COMMITS:]
    return RepoReport(
        repo_path=str(repo),
        branch=target,
        head=head,
        commits=commits,
        truncated=truncated,
        fetch_warning=warning,
    )


def build_facts(report: RepoReport) -> str:
    """모델에게 넘길 사실 묶음. 여기 없는 것은 보고서에 나와서는 안 된다."""
    if not report.commits:
        return ''
    lines: list[str] = []
    total_added, total_removed = report.total_lines
    code_added, code_removed = report.code_lines
    authors = ', '.join(f'{name} ({count} commits)' for name, count in report.authors)
    lines.append(f'[Repository] {Path(report.repo_path).name} · branch {report.branch}')
    lines.append(
        f'[Totals] {len(report.commits)} commits · +{total_added}/-{total_removed} lines · '
        f'hand-written code +{code_added}/-{code_removed} · authors: {authors}'
    )
    if report.truncated:
        lines.append(f'[Note] Only the most recent {MAX_COMMITS} commits are included.')
    if report.fetch_warning:
        lines.append(f'[Warning] {report.fetch_warning}')
    lines.append('')
    for commit in report.commits:
        lines.append(f'--- {commit.sha} | {commit.when} | {commit.author_name}')
        lines.append(f'subject: {commit.subject}')
        if commit.body:
            lines.append(f'body: {commit.body}')
        shown = [f for f in commit.files if not f.generated][:12]
        if shown:
            listed = ', '.join(f'{f.path} (+{f.added}/-{f.removed})' for f in shown)
            lines.append(f'code files: {listed}')
        generated = len(commit.files) - len([f for f in commit.files if not f.generated])
        if generated or commit.truncated_files:
            extra = commit.truncated_files
            lines.append(
                f'other files: {generated} generated/asset'
                + (f' (+{extra} more not listed)' if extra else '')
            )
        lines.append(f'changed: {len(commit.files) + commit.truncated_files} files '
                     f'+{commit.added}/-{commit.removed}')
        lines.append('')
    return '\n'.join(lines)
