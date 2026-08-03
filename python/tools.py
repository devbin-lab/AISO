"""에이전트 파일 툴 — 작업 폴더(workspace) 안으로 강제 confine.

모든 경로는 workspace 루트 기준으로 해석되며, realpath 검사로 폴더 밖 접근을
차단한다. 파괴적 작업(write/edit/delete)의 승인은 상위(agent 루프)에서 처리한다.
"""

from __future__ import annotations

import os
import re
import shutil
from fnmatch import fnmatch
from pathlib import Path

from extract import EXTRACTORS, ExtractError

MAX_READ_BYTES = 256 * 1024  # 읽기 상한 (256KB)
MAX_READ_LINES = 2000        # offset/limit 미지정 시 라인모드 기본 상한
MAX_LIST_ENTRIES = 500

# grep/glob 검색 관련 상한
MAX_GREP_MATCHES = 200            # content 모드 총 매치 상한
MAX_GREP_FILES = 5000             # 스캔 파일 수 상한
MAX_GREP_FILE_BYTES = 5 * 1024 * 1024  # 이보다 큰 파일은 스캔 제외
MAX_MATCH_LINE_LEN = 300          # 매치 라인 표시 길이
MAX_GLOB_RESULTS = 300            # glob 결과 파일 수 상한
MAX_CODE_FILE_BYTES = 2 * 1024 * 1024

_CODE_FILE_SUFFIXES = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".html", ".htm", ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".java", ".kt", ".kts",
    ".go", ".rs", ".swift", ".dart", ".lua", ".rb", ".php", ".sh", ".ps1", ".bat", ".cmd",
    ".gd", ".shader", ".hlsl", ".glsl", ".compute", ".usf", ".ush",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg", ".conf",
    ".sql", ".graphql", ".proto", ".txt", ".csv",
    ".sln", ".csproj", ".fsproj", ".vbproj", ".vcxproj", ".props", ".targets",
    ".uproject", ".uplugin", ".unity", ".asmdef",
})
_CODE_FILE_NAMES = frozenset({
    "dockerfile", "makefile", "cmakelists.txt", "meson.build", "justfile",
    "requirements.txt", "pyproject.toml", "package.json", "tsconfig.json",
    ".gitignore", ".gitattributes", ".editorconfig",
})
_CODE_BLOCKED_PARTS = frozenset({".git", ".aiso", "node_modules", ".venv", "venv", "__pycache__"})
_CODE_SECRET_SUFFIXES = frozenset({".pem", ".key", ".pfx", ".p12", ".cer", ".crt"})


class ToolError(Exception):
    """툴 실행 실패 — 사유는 모델에게 그대로 전달된다."""


def validate_workspace(workspace: str) -> Path:
    """작업 폴더로 안전한 경로인지 검증하고 resolved Path를 돌려준다."""
    if not workspace:
        raise ToolError("작업 폴더가 지정되지 않았습니다.")
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ToolError(f"작업 폴더가 존재하지 않습니다: {root}")
    # 드라이브 루트(C:\) / 사용자 홈은 통째로 열지 않는다
    if root == Path(root.anchor):
        raise ToolError("드라이브 루트는 작업 폴더로 사용할 수 없습니다.")
    if root == Path.home().resolve():
        raise ToolError("사용자 홈 폴더는 작업 폴더로 사용할 수 없습니다.")
    return root


def _resolve(root: Path, rel: str) -> Path:
    """workspace 기준 상대경로를 안전하게 절대경로로. 폴더 밖이면 거부."""
    if rel is None:
        raise ToolError("경로가 필요합니다.")
    # 모델이 경로를 따옴표로 감싸거나(예: 약한 모델이 '.' 대신 '."""'처럼) 공백을 덧붙이는
    # 인용 아티팩트 방어. 따옴표·백틱은 Windows 파일명에 쓸 수 없으므로 양끝에서 벗겨도 안전.
    rel = str(rel).strip().strip("\"'`").strip() or "."
    # NTFS 대체 데이터 스트림(':')·드라이브 지정자 차단 — 작업 폴더 기준 상대경로엔 콜론이
    # 올 일이 없다. 'foo.py:x.md'처럼 스트림 접미사로 확장자 검사를 속이는 경로를 원천 차단한다.
    if ":" in rel:
        raise ToolError(f"경로에 ':'를 쓸 수 없습니다: {rel}")
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"작업 폴더 밖 경로 접근이 거부되었습니다: {rel}")
    return target


def _require_doc(target: Path, path: str) -> None:
    """Keep Markdown authoring separate from the optional project-code tools."""
    if target.suffix.lower() != ".md":
        raise ToolError(
            f"문서는 마크다운(.md)만 작성·편집할 수 있습니다. '{path}'는 허용되지 않습니다. "
            "프로젝트 코드는 설정 > 도구에서 별도로 허용한 코드 작성·편집 도구를 사용하세요."
        )


def _require_code_file(root: Path, target: Path, path: str) -> None:
    """프로젝트 코드 도구가 다룰 수 있는 UTF-8 텍스트 소스·설정 경로인지 확인한다."""
    try:
        relative_parts = tuple(part.lower() for part in target.relative_to(root).parts)
    except ValueError as error:
        raise ToolError(f"작업 폴더 밖 경로 접근이 거부되었습니다: {path}") from error
    if any(part in _CODE_BLOCKED_PARTS for part in relative_parts[:-1]):
        raise ToolError("저장소 제어·의존성·가상환경 폴더의 파일은 코드 도구로 편집할 수 없습니다.")
    lower_name = target.name.lower()
    if lower_name == ".env" or lower_name.startswith(".env.") or target.suffix.lower() in _CODE_SECRET_SUFFIXES:
        raise ToolError("비밀값이나 인증서 파일은 코드 도구로 작성·편집할 수 없습니다.")
    if lower_name not in _CODE_FILE_NAMES and target.suffix.lower() not in _CODE_FILE_SUFFIXES:
        raise ToolError(
            f"지원하지 않는 프로젝트 텍스트 파일 형식입니다: {path}. "
            "소스 코드·스크립트·일반 프로젝트 설정 파일만 허용됩니다."
        )


def _read_utf8_code_file(target: Path, path: str) -> str:
    if not target.is_file():
        raise ToolError(f"파일이 없습니다: {path}")
    data = target.read_bytes()
    if len(data) > MAX_CODE_FILE_BYTES:
        raise ToolError(f"코드 파일이 {MAX_CODE_FILE_BYTES // (1024 * 1024)}MB 상한을 넘습니다: {path}")
    if b"\x00" in data:
        raise ToolError(f"바이너리 파일은 코드 도구로 편집할 수 없습니다: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolError(f"UTF-8이 아닌 파일은 손상 방지를 위해 편집하지 않습니다: {path}") from error


def _validate_code_content(content: str) -> None:
    if not isinstance(content, str):
        raise ToolError("파일 내용은 문자열이어야 합니다.")
    if "\x00" in content:
        raise ToolError("NUL 문자가 포함된 바이너리 내용은 코드 파일로 저장할 수 없습니다.")
    if len(content.encode("utf-8")) > MAX_CODE_FILE_BYTES:
        raise ToolError(f"작성할 코드가 {MAX_CODE_FILE_BYTES // (1024 * 1024)}MB 상한을 넘습니다.")


def _dominant_newline(text: str) -> str | None:
    """Return the existing file's dominant newline without rewriting untouched text."""
    crlf_count = text.count("\r\n")
    lf_count = text.count("\n") - crlf_count
    cr_count = text.count("\r") - crlf_count
    if crlf_count == lf_count == cr_count == 0:
        return None
    if crlf_count >= lf_count and crlf_count >= cr_count:
        return "\r\n"
    if lf_count >= cr_count:
        return "\n"
    return "\r"


def _normalize_edit_newlines(value: str, newline: str | None) -> str:
    if newline is None or not any(marker in value for marker in ("\r", "\n")):
        return value
    canonical = value.replace("\r\n", "\n").replace("\r", "\n")
    return canonical if newline == "\n" else canonical.replace("\n", newline)


def _prepare_code_edit(text: str, old_string: str, new_string: str) -> tuple[str, str]:
    """Match model-supplied LF snippets to the file while preserving its newline style."""
    newline = _dominant_newline(text)
    old = old_string if old_string in text else _normalize_edit_newlines(old_string, newline)
    new = _normalize_edit_newlines(new_string, newline)
    return old, new


def _rel(root: Path, target: Path) -> str:
    try:
        return target.relative_to(root).as_posix() or "."
    except ValueError:
        return str(target)


_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


def _is_link_or_reparse_point(path: Path) -> bool:
    """Return whether a child can redirect traversal outside the workspace.

    ``Path.is_symlink`` covers POSIX links, while Windows directory junctions are
    reparse points and do not reliably report as symlinks. Never recurse through
    either kind of entry from a workspace tree listing.
    """
    try:
        if path.is_symlink():
            return True
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        # An inaccessible or racing entry should not be followed optimistically.
        return True


# ---- 안전 툴 (자동 실행) ----

def list_dir(root: Path, path: str = ".") -> str:
    target = _resolve(root, path or ".")
    if not target.is_dir():
        raise ToolError(f"폴더가 아닙니다: {path}")
    entries = []
    for i, p in enumerate(sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))):
        if i >= MAX_LIST_ENTRIES:
            entries.append(f"... (외 {MAX_LIST_ENTRIES}개 초과 생략)")
            break
        kind = "dir " if p.is_dir() else "file"
        size = f"  {p.stat().st_size}B" if p.is_file() else ""
        entries.append(f"[{kind}] {p.name}{size}")
    listing = "\n".join(entries) if entries else "(빈 폴더)"
    return f"{_rel(root, target)}/\n{listing}"


# 트리에서 통째로 생략할 잡폴더 (이름만 표시하고 펼치지 않는다)
_TREE_SKIP = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "out",
    "build", ".next", ".aiso", "tools", "target", ".idea", ".vscode", "coverage",
}
_TREE_LIMIT = 600  # 트리 전체 항목 상한


def list_tree(root: Path, path: str = ".", max_depth: int = 4) -> str:
    """폴더 구조를 하위까지 재귀 트리로 본다. 잡폴더는 이름만 표시하고 펼치지 않는다."""
    target = _resolve(root, path or ".")
    if not target.is_dir():
        raise ToolError(f"폴더가 아닙니다: {path}")
    try:
        max_depth = max(1, min(int(max_depth), 8))
    except (TypeError, ValueError):
        max_depth = 4

    lines: list[str] = []
    state = {"count": 0, "truncated": False}

    def walk(d: Path, depth: int, prefix: str) -> None:
        if state["truncated"]:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return
        for p in entries:
            if state["count"] >= _TREE_LIMIT:
                state["truncated"] = True
                return
            state["count"] += 1
            if _is_link_or_reparse_point(p):
                lines.append(f"{prefix}{p.name}  [링크/정션 — 탐색 생략]")
                continue
            if p.is_dir():
                lines.append(f"{prefix}{p.name}/")
                if p.name in _TREE_SKIP:
                    lines.append(f"{prefix}  … (생략)")
                elif depth < max_depth:
                    walk(p, depth + 1, prefix + "  ")
                else:
                    try:
                        if any(p.iterdir()):
                            lines.append(f"{prefix}  … (더 깊음 — max_depth 초과)")
                    except OSError:
                        pass
            else:
                try:
                    lines.append(f"{prefix}{p.name}  ({p.stat().st_size}B)")
                except OSError:
                    lines.append(f"{prefix}{p.name}")

    walk(target, 1, "")
    body = "\n".join(lines) if lines else "(빈 폴더)"
    note = f"\n… (항목 {_TREE_LIMIT}개 초과 — 이후 생략)" if state["truncated"] else ""
    return f"{_rel(root, target)}/\n{body}{note}"


# grep/glob도 트리와 같은 잡폴더를 건너뛴다 (드리프트 방지: 같은 목록 재사용)
_SEARCH_SKIP = _TREE_SKIP


def grep(
    root: Path,
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    ignore_case: bool = False,
    output_mode: str = "content",
) -> str:
    """작업 폴더 텍스트 파일에서 정규식/문자열을 검색한다 (읽기 전용). 순수 파이썬 re."""
    if not pattern:
        raise ToolError("검색어(pattern)가 비어 있습니다.")
    if output_mode not in ("content", "files", "count"):
        raise ToolError("output_mode는 'content'|'files'|'count' 중 하나여야 합니다.")
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"정규식이 올바르지 않습니다: {e}")

    start = _resolve(root, path or ".")
    if not start.exists():
        raise ToolError(f"경로가 없습니다: {path}")

    files: list[Path] = []
    if start.is_file():
        files = [start]
    else:
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP]
            for fn in filenames:
                if glob and not fnmatch(fn, glob):
                    continue
                files.append(Path(dirpath) / fn)
                if len(files) >= MAX_GREP_FILES:
                    break
            if len(files) >= MAX_GREP_FILES:
                break

    content: list[str] = []
    file_hits: list[str] = []
    counts: dict[str, int] = {}
    total = 0
    truncated = False
    for fp in files:
        try:
            if fp.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            with fp.open("rb") as f:
                if b"\x00" in f.read(4096):  # 바이너리 스킵
                    continue
            rel = _rel(root, fp)
            n = 0
            with fp.open("r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if rx.search(line):
                        n += 1
                        if output_mode == "content":
                            snip = line.rstrip("\n")[:MAX_MATCH_LINE_LEN]
                            content.append(f"{rel}:{lineno}:{snip}")
                            total += 1
                            if total >= MAX_GREP_MATCHES:
                                truncated = True
                                break
            if n:
                file_hits.append(rel)
                counts[rel] = n
        except OSError:
            continue
        if truncated:
            break

    if output_mode == "files":
        return "\n".join(sorted(file_hits)) or "일치하는 파일이 없습니다."
    if output_mode == "count":
        if not counts:
            return "일치하는 내용이 없습니다."
        return "\n".join(f"{c:>6}  {rel}" for rel, c in sorted(counts.items()))
    body = "\n".join(content) or "일치하는 내용이 없습니다."
    return body + (f"\n…(매치 {MAX_GREP_MATCHES}건 초과 — 이후 생략)" if truncated else "")


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """'**'(폴더 경계 무시)·'*'(폴더 내)·'?'를 지원하는 글롭→정규식."""
    i, out = 0, ["(?s)^"]
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def glob(root: Path, pattern: str, path: str = ".") -> str:
    """글롭 패턴으로 파일을 찾아 최근 수정순으로 돌려준다 (읽기 전용). 순수 파이썬."""
    if not pattern:
        raise ToolError("glob 패턴이 비어 있습니다.")
    start = _resolve(root, path or ".")
    if not start.is_dir():
        raise ToolError(f"폴더가 아닙니다: {path}")
    rx = _glob_to_regex(pattern)
    hits: list[tuple[float, str]] = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP]
        for fn in filenames:
            fp = Path(dirpath) / fn
            if rx.match(fp.relative_to(start).as_posix()):
                try:
                    hits.append((fp.stat().st_mtime, _rel(root, fp)))
                except OSError:
                    continue
    hits.sort(key=lambda t: t[0], reverse=True)  # 최신 수정 우선
    truncated = len(hits) > MAX_GLOB_RESULTS
    rows = [rel for _, rel in hits[:MAX_GLOB_RESULTS]]
    body = "\n".join(rows) or "조건에 맞는 파일이 없습니다."
    return body + (f"\n…(파일 {MAX_GLOB_RESULTS}개 초과 — 이후 생략)" if truncated else "")


def read_file(root: Path, path: str, offset: int | None = None, limit: int | None = None) -> str:
    target = _resolve(root, path)
    if not target.is_file():
        raise ToolError(f"파일이 없습니다: {path}")
    # 문서 형식(PDF/엑셀/워드/한글 등)은 텍스트 추출 — offset/limit 미적용
    ext = target.suffix.lower()
    if ext in EXTRACTORS:
        fn, label = EXTRACTORS[ext]
        try:
            text = (fn(target) or "").strip()
        except ExtractError as e:
            raise ToolError(str(e))
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"{label} 파일을 읽는 중 오류: {e}")
        if not text:
            return f"[{path}] {label}에서 추출된 텍스트가 없습니다 (빈 문서이거나 이미지 기반일 수 있음)."
        if len(text) > MAX_READ_BYTES:
            text = text[:MAX_READ_BYTES] + "\n\n…(내용이 길어 앞부분만 표시)"
        note = ""
        if offset is not None or limit is not None:
            note = "\n\n(참고: 문서 형식에는 줄 범위 지정이 적용되지 않아 전체에서 추출했습니다.)"
        return f"[{path} · {label} 텍스트 추출]\n{text}{note}"
    # 바이너리 감지 (널 바이트) → 깨진 텍스트 대신 명확히 알림
    with target.open("rb") as f:
        probe = f.read(4096)
    if b"\x00" in probe:
        raise ToolError(
            f"바이너리 파일이라 텍스트로 읽을 수 없습니다: {path} "
            "(PDF는 자동으로 텍스트가 추출됩니다. 이미지 등 기타 형식은 미지원)"
        )

    # offset/limit 둘 다 없으면 → 기존 동작(앞 256KB 바이트 읽기) 그대로
    if offset is None and limit is None:
        size = target.stat().st_size
        with target.open("rb") as f:
            data = f.read(MAX_READ_BYTES)
        text = data.decode("utf-8", errors="replace")
        if size > MAX_READ_BYTES:
            text += f"\n\n... (파일이 커서 앞 {MAX_READ_BYTES}바이트만 표시)"
        return text

    # 라인 범위 모드 — cat -n 스타일(줄번호)로 후속 편집 좌표 제공
    start = 1 if offset is None else int(offset)
    if start < 1:
        raise ToolError("offset은 1 이상이어야 합니다.")
    n = MAX_READ_LINES if limit is None else int(limit)
    if n < 1:
        raise ToolError("limit은 1 이상이어야 합니다.")
    n = min(n, MAX_READ_LINES)

    picked: list[str] = []
    total = 0
    budget = MAX_READ_BYTES
    with target.open("r", encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            total = lineno
            if lineno < start:
                continue
            if lineno >= start + n:
                for lineno, _ in enumerate(f, lineno + 1):  # 남은 줄 수만 마저 센다
                    total = lineno
                break
            body = line.rstrip("\n")
            budget -= len(body) + 1
            if budget <= 0:
                picked.append(f"{lineno}\t…(범위 내용이 256KB를 넘어 잘림)")
                break
            picked.append(f"{lineno}\t{body}")

    if start > total:
        return f"[{path}] 파일은 총 {total}줄입니다. offset {start}은 파일 끝을 벗어났습니다 (표시할 내용 없음)."
    header = f"[{path} · {start}~{min(start + n - 1, total)}줄 / 총 {total}줄]"
    return header + "\n" + "\n".join(picked)


def create_dir(root: Path, path: str) -> str:
    target = _resolve(root, path)
    if target.is_file():
        raise ToolError(f"같은 이름의 파일이 이미 있습니다: {path}")
    target.mkdir(parents=True, exist_ok=True)
    return f"{_rel(root, target)}/ 폴더를 생성함."


# ---- 파괴적 툴 (승인 후 실행) ----

def write_file(root: Path, path: str, content: str = "") -> str:
    target = _resolve(root, path)
    _require_doc(target, path)
    if target.is_dir():
        raise ToolError(f"폴더에는 쓸 수 없습니다: {path}")
    existed = target.is_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    verb = "덮어씀" if existed else "생성함"
    return f"{_rel(root, target)} 파일을 {verb} ({len(content)}자)."


def edit_file(root: Path, path: str, old_string: str, new_string: str) -> str:
    target = _resolve(root, path)
    _require_doc(target, path)
    if not target.is_file():
        raise ToolError(f"파일이 없습니다: {path}")
    text = target.read_text(encoding="utf-8", errors="replace")
    if old_string == "":
        raise ToolError("old_string이 비어 있습니다.")
    count = text.count(old_string)
    if count == 0:
        raise ToolError("old_string과 일치하는 내용을 찾지 못했습니다.")
    if count > 1:
        raise ToolError(f"old_string이 {count}곳에서 발견되었습니다. 더 긴 맥락으로 유일하게 지정하세요.")
    target.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
    return f"{_rel(root, target)} 파일을 편집함 (1곳 치환)."


def multi_edit(root: Path, path: str, edits: list) -> str:
    """한 파일에 여러 찾기·바꾸기를 순서대로 원자적으로 적용한다(전부 성공 또는 전부 취소)."""
    target = _resolve(root, path)
    _require_doc(target, path)
    if not target.is_file():
        raise ToolError(f"파일이 없습니다: {path}")
    if not isinstance(edits, list) or not edits:
        raise ToolError("적용할 편집(edits)이 없습니다.")
    with target.open("rb") as f:
        if b"\x00" in f.read(4096):  # 바이너리는 replace로 깨질 수 있어 거부
            raise ToolError(f"바이너리 파일은 편집할 수 없습니다: {path}")

    text = target.read_text(encoding="utf-8", errors="replace")
    applied = 0
    for i, e in enumerate(edits, 1):
        if not isinstance(e, dict):
            raise ToolError(f"{i}번째 편집 형식이 올바르지 않습니다.")
        old = e.get("old_string", "")
        new = e.get("new_string", "")
        all_ = bool(e.get("replace_all", False))
        if old == "":
            raise ToolError(f"{i}번째 편집의 old_string이 비어 있습니다.")
        if old == new:
            raise ToolError(f"{i}번째 편집: old_string과 new_string이 같습니다.")
        count = text.count(old)
        if count == 0:
            raise ToolError(f"{i}번째 편집: old_string과 일치하는 내용을 찾지 못했습니다.")
        if count > 1 and not all_:
            raise ToolError(
                f"{i}번째 편집: old_string이 {count}곳에서 발견되었습니다. "
                "replace_all=true로 지정하거나 더 긴 맥락으로 유일하게 지정하세요."
            )
        text = text.replace(old, new) if all_ else text.replace(old, new, 1)
        applied += count if all_ else 1
    # 모든 편집이 검증을 통과한 뒤에야 한 번 쓴다 → all-or-nothing
    target.write_text(text, encoding="utf-8")
    return f"{_rel(root, target)} 파일을 편집함 ({len(edits)}개 편집, {applied}곳 치환)."


def write_code_file(root: Path, path: str, content: str = "") -> str:
    target = _resolve(root, path)
    _require_code_file(root, target, path)
    _validate_code_content(content)
    if target.is_dir():
        raise ToolError(f"폴더에는 쓸 수 없습니다: {path}")
    existed = target.is_file()
    if existed:
        _read_utf8_code_file(target, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write exact UTF-8 bytes so an existing/model-supplied CRLF is not translated
    # into CRCRLF by Windows text-mode newline conversion.
    target.write_bytes(content.encode("utf-8"))
    verb = "덮어씀" if existed else "생성함"
    return f"{_rel(root, target)} 코드 파일을 {verb} ({len(content)}자)."


def edit_code_file(root: Path, path: str, old_string: str, new_string: str) -> str:
    target = _resolve(root, path)
    _require_code_file(root, target, path)
    text = _read_utf8_code_file(target, path)
    _validate_code_content(new_string)
    if not isinstance(old_string, str):
        raise ToolError("old_string은 문자열이어야 합니다.")
    if old_string == "":
        raise ToolError("old_string이 비어 있습니다.")
    old_string, new_string = _prepare_code_edit(text, old_string, new_string)
    if old_string == new_string:
        raise ToolError("old_string과 new_string이 같습니다.")
    count = text.count(old_string)
    if count == 0:
        raise ToolError("old_string과 일치하는 내용을 찾지 못했습니다.")
    if count > 1:
        raise ToolError(f"old_string이 {count}곳에서 발견되었습니다. 더 긴 맥락으로 유일하게 지정하세요.")
    updated = text.replace(old_string, new_string, 1)
    _validate_code_content(updated)
    target.write_bytes(updated.encode("utf-8"))
    return f"{_rel(root, target)} 코드 파일을 편집함 (1곳 치환)."


def multi_edit_code_file(root: Path, path: str, edits: list) -> str:
    """UTF-8 프로젝트 파일에 여러 편집을 검증한 뒤 한 번에 적용한다."""
    target = _resolve(root, path)
    _require_code_file(root, target, path)
    text = _read_utf8_code_file(target, path)
    if not isinstance(edits, list) or not edits:
        raise ToolError("적용할 편집(edits)이 없습니다.")
    applied = 0
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict):
            raise ToolError(f"{index}번째 편집 형식이 올바르지 않습니다.")
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        replace_all = edit.get("replace_all", False)
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError(f"{index}번째 편집 내용은 문자열이어야 합니다.")
        if not isinstance(replace_all, bool):
            raise ToolError(f"{index}번째 편집의 replace_all은 boolean이어야 합니다.")
        if old == "":
            raise ToolError(f"{index}번째 편집의 old_string이 비어 있습니다.")
        old, new = _prepare_code_edit(text, old, new)
        if old == new:
            raise ToolError(f"{index}번째 편집: old_string과 new_string이 같습니다.")
        count = text.count(old)
        if count == 0:
            raise ToolError(f"{index}번째 편집: old_string과 일치하는 내용을 찾지 못했습니다.")
        if count > 1 and not replace_all:
            raise ToolError(
                f"{index}번째 편집: old_string이 {count}곳에서 발견되었습니다. "
                "replace_all=true로 지정하거나 더 긴 맥락으로 유일하게 지정하세요."
            )
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        applied += count if replace_all else 1
    _validate_code_content(text)
    target.write_bytes(text.encode("utf-8"))
    return f"{_rel(root, target)} 코드 파일을 편집함 ({len(edits)}개 편집, {applied}곳 치환)."


def _trash_or_remove(target: Path, is_dir: bool) -> str:
    """Move to the recycle bin, or fail without deleting anything.

    An approved reversible delete must not silently turn into permanent removal
    because the recycle-bin provider is unavailable (for example on a network
    share). Permanent deletion needs a separate, explicit product flow.
    """
    try:
        from send2trash import send2trash  # 잘못 지워도 사용자가 휴지통에서 복구 가능
        send2trash(str(target))
        return "휴지통으로 보냄"
    except Exception as e:  # noqa: BLE001
        kind = "폴더" if is_dir else "파일"
        raise ToolError(
            f"{kind}을(를) 휴지통으로 옮기지 못했습니다({type(e).__name__}). "
            "영구 삭제로 대체하지 않았습니다. 휴지통을 사용할 수 있는 위치인지 확인한 뒤 다시 시도하세요."
        ) from e


def delete_file(root: Path, path: str) -> str:
    target = _resolve(root, path)
    if target.is_dir():
        raise ToolError("폴더는 삭제할 수 없습니다 (파일만 가능).")
    if not target.is_file():
        raise ToolError(f"파일이 없습니다: {path}")
    how = _trash_or_remove(target, is_dir=False)
    return f"{_rel(root, target)} 파일을 {how}."


def delete_dir(root: Path, path: str) -> str:
    target = _resolve(root, path)
    if target == root:
        raise ToolError("작업 폴더 자체는 삭제할 수 없습니다.")
    if target.is_file():
        raise ToolError(f"파일입니다. delete_file을 사용하세요: {path}")
    if not target.is_dir():
        raise ToolError(f"폴더가 없습니다: {path}")
    n_files = sum(1 for p in target.rglob("*") if p.is_file())
    how = _trash_or_remove(target, is_dir=True)
    return f"{_rel(root, target)}/ 폴더를 {how} (하위 파일 {n_files}개 포함)."


def move(root: Path, src: str, dst: str) -> str:
    """파일/폴더를 이동하거나 이름을 바꾼다. 내용을 재작성하지 않고 원본 그대로 옮긴다(바이너리 안전)."""
    s = _resolve(root, src)
    if not s.exists():
        raise ToolError(f"원본이 없습니다: {src}")
    if s == root:
        raise ToolError("작업 폴더 자체는 이동할 수 없습니다.")
    d = _resolve(root, dst.rstrip("/\\") or ".")
    # dst가 (1)기존 폴더거나 (2)'/'로 끝나거나 (3)확장자 없는 새 이름이면 → 그 폴더 안으로 이동(원래 이름 유지)
    looks_like_dir = dst.endswith(("/", "\\")) or (not d.exists() and d.suffix == "")
    if d.is_dir() or looks_like_dir:
        d = d / s.name
    if d == s:
        raise ToolError("원본과 대상이 같습니다.")
    if d.exists():
        raise ToolError(f"대상이 이미 있습니다(덮어쓰지 않음): {_rel(root, d)}")
    # .md 잠금 우회 차단 — 에이전트가 쓸 수 있는 문서(.md)를 코드 확장자로 개명해 워크스페이스에
    # 실행 코드를 심는 경로를 막는다(write_file('x.md')→move('x.py')). 사용자의 기존 비-.md
    # 파일 정리나 폴더 이동(.md는 이름 보존)은 그대로 허용된다.
    if s.is_file() and s.suffix.lower() == ".md" and d.suffix.lower() != ".md":
        raise ToolError(
            "문서(.md)를 다른 확장자로 개명할 수 없습니다 — 이 에이전트는 프로그램 코드를 "
            "워크스페이스에 만들지 않습니다. 자동화가 필요하면 create_skill을 쓰세요."
        )
    src_label = _rel(root, s)
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return f"{src_label} → {_rel(root, d)} 로 이동함."


_DISPATCH = {
    "list_dir": list_dir,
    "list_tree": list_tree,
    "grep": grep,
    "glob": glob,
    "read_file": read_file,
    "create_dir": create_dir,
    "write_file": write_file,
    "edit_file": edit_file,
    "multi_edit": multi_edit,
    "write_code_file": write_code_file,
    "edit_code_file": edit_code_file,
    "multi_edit_code_file": multi_edit_code_file,
    "delete_file": delete_file,
    "delete_dir": delete_dir,
    "move": move,
}


def run_tool(root: Path, name: str, args: dict) -> str:
    fn = _DISPATCH.get(name)
    if fn is None:
        raise ToolError(f"알 수 없는 툴: {name}")
    try:
        return fn(root, **args)
    except ToolError:
        raise
    except (TypeError, ValueError) as e:
        raise ToolError(f"인자 오류: {e}")
    except OSError as e:
        raise ToolError(f"파일 시스템 오류: {e}")


# LLM runtime의 tools 파라미터로 넘길 함수 스키마
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "작업 폴더 내 디렉터리의 파일·하위폴더 목록을 본다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "작업 폴더 기준 상대경로. 기본값은 최상위."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tree",
            "description": "작업 폴더의 파일·폴더 구조를 하위까지 재귀적으로 트리로 본다. 사용자가 '전체/상세 구조'나 '무슨 파일이 있는지'를 물으면 한 단계만 보는 list_dir 대신 이걸 써서 하위 폴더 안의 파일까지 한 번에 빠짐없이 파악하라. node_modules·.git 등 잡폴더는 자동 생략된다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "작업 폴더 기준 상대경로. 기본값은 최상위."},
                    "max_depth": {"type": "integer", "description": "재귀 깊이 (기본 4, 최대 8)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "작업 폴더 내 파일의 내용을 읽는다. 코드·md·csv 등 텍스트는 물론, PDF·엑셀(xlsx)·워드(docx)·한글(hwp/hwpx) 문서도 자동으로 텍스트를 추출한다. 큰 텍스트 파일은 offset·limit으로 특정 줄 범위만 읽을 수 있고(줄번호가 함께 표시됨), 문서에는 줄 범위가 적용되지 않는다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "작업 폴더 기준 상대경로."},
                    "offset": {"type": "integer", "description": "읽기 시작할 줄 번호(1부터). 생략하면 처음부터."},
                    "limit": {"type": "integer", "description": "읽을 줄 수. 생략하면 상한까지."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "작업 폴더 안의 텍스트 파일들에서 정규식(또는 문자열)과 일치하는 내용을 빠르게 검색한다. 특정 함수·변수·문구가 '어느 파일 몇 번째 줄'에 있는지 찾을 때 read_file로 일일이 열지 말고 이걸 써라. node_modules·.git·tools 등 잡폴더와 바이너리·대용량 파일은 자동으로 건너뛴다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "찾을 정규식 또는 문자열."},
                    "path": {"type": "string", "description": "검색 시작 경로(폴더 또는 단일 파일). 기본값은 최상위('.')."},
                    "glob": {"type": "string", "description": "파일명 필터(예: '*.py', '*.ts')."},
                    "ignore_case": {"type": "boolean", "description": "대소문자 무시 여부(기본 false)."},
                    "output_mode": {
                        "type": "string",
                        "enum": ["content", "files", "count"],
                        "description": "content=일치 줄(file:line:내용), files=일치 파일 목록, count=파일별 횟수. 기본 content.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "글롭 패턴으로 작업 폴더 안의 파일을 찾는다(예: '**/*.ts', 'src/**/*.py', '*.md'). 최근 수정된 파일이 먼저 오도록 정렬한다. 파일 이름/확장자로 위치를 찾을 때 list_tree보다 정확하다. node_modules·.git 등 잡폴더는 자동 제외된다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "글롭 패턴. '**'는 하위 폴더 전체를 뜻한다."},
                    "path": {"type": "string", "description": "검색 시작 폴더(기본 '.')."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_dir",
            "description": "작업 폴더 내에 새 폴더(디렉터리)를 만든다. 중간 경로도 함께 생성된다.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "작업 폴더 기준 상대경로."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move",
            "description": "파일이나 폴더를 다른 경로로 이동하거나 이름을 바꾼다. 내용을 재작성하지 않고 원본 그대로 옮기므로 PDF·엑셀·한글 등 바이너리도 안전하다. **파일 정리(폴더별 이동)에는 read_file+write_file 대신 반드시 이 move를 써라.** dst가 기존 폴더면 그 안으로 옮긴다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "옮길 원본 경로 (작업 폴더 기준)."},
                    "dst": {"type": "string", "description": "대상 경로. 기존 폴더면 그 안으로, 아니면 그 이름으로 이동/개명."},
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "마크다운(.md) 문서를 생성하거나 전체 내용을 덮어쓴다. 프로젝트 코드는 write_code_file을 사용한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "작업 폴더 기준 상대경로."},
                    "content": {"type": "string", "description": "파일에 쓸 전체 내용."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "마크다운(.md) 문서에서 old_string을 찾아 new_string으로 정확히 1곳 치환한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "작업 폴더 기준 상대경로."},
                    "old_string": {"type": "string", "description": "바꿀 기존 내용 (파일 내에서 유일해야 함)."},
                    "new_string": {"type": "string", "description": "새 내용."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit",
            "description": "마크다운(.md) 문서 하나에 여러 찾기·바꾸기를 순서대로 검증한 뒤 한 번에 적용한다. 프로젝트 코드는 multi_edit_code_file을 사용한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "작업 폴더 기준 상대경로."},
                    "edits": {
                        "type": "array",
                        "description": "순서대로 적용할 편집 목록.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string", "description": "바꿀 기존 내용."},
                                "new_string": {"type": "string", "description": "새 내용."},
                                "replace_all": {"type": "boolean", "description": "일치하는 모든 곳을 바꿀지(기본 false)."},
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_code_file",
            "description": "프로젝트의 UTF-8 소스 코드·스크립트·설정 파일을 새로 만들거나 전체 내용을 덮어쓴다. .git·의존성·가상환경·비밀값 경로와 바이너리 파일은 거부한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "작업 폴더 기준 프로젝트 파일 상대경로."},
                    "content": {"type": "string", "description": "파일에 쓸 UTF-8 전체 내용."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_code_file",
            "description": "기존 UTF-8 프로젝트 파일에서 유일한 old_string 한 곳을 new_string으로 치환한다. 코드의 국소 수정에 사용한다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "작업 폴더 기준 프로젝트 파일 상대경로."},
                    "old_string": {"type": "string", "description": "파일 안에서 유일해야 하는 기존 내용."},
                    "new_string": {"type": "string", "description": "대체할 새 내용."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "multi_edit_code_file",
            "description": "UTF-8 프로젝트 파일 하나에 여러 찾기·바꾸기를 순서대로 검증한 뒤 한 번에 적용한다. 하나라도 실패하면 파일을 쓰지 않는다.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "작업 폴더 기준 프로젝트 파일 상대경로."},
                    "edits": {
                        "type": "array",
                        "description": "순서대로 적용할 편집 목록.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {"type": "string", "description": "바꿀 기존 내용."},
                                "new_string": {"type": "string", "description": "새 내용."},
                                "replace_all": {"type": "boolean", "description": "모든 일치 항목을 바꿀지 여부."},
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "작업 폴더 내 파일 하나를 삭제한다.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "작업 폴더 기준 상대경로."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_dir",
            "description": "작업 폴더 내 폴더를 하위 내용까지 통째로 삭제한다(재귀). 작업 폴더 자체는 삭제 불가.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "작업 폴더 기준 상대경로."}},
                "required": ["path"],
            },
        },
    },
]
