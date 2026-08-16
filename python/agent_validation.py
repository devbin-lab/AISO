"""Filesystem-safe primitives for the agent's HTML validation contract.

This module deliberately owns only deterministic path, mutation-contract, and
result-normalisation logic.  Natural-language intent classification remains in
``agent.py`` for now, where it can continue to use the conversational context.
Keeping this boundary dependency-free makes the safety rules reusable and easy
to test without starting an agent run.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import re
from pathlib import Path
from typing import Any

from tools import MAX_CODE_FILE_BYTES


_ENGLISH_MUTATION_VERB = (
    r"(?:create|build|implement|write|rewrite|edit|add|fix|change|modify|improve|"
    r"optimi[sz]e|update|refactor|repair|revise|polish|apply|perform|execute|"
    r"carry\s+out|patch|replace|delete|remove|rename|move|"
    r"copy|overwrite|generate|make|develop|code|save|deploy|publish|format|convert|"
    r"export|archive|upload|install|minify|bundle|compile|package|commit|push|release|ship)"
)


def html_entry_path(args: dict[str, Any]) -> tuple[str, str] | None:
    """Return a stable comparison key and display path for an HTML tool target."""
    raw = str(args.get("path") or "").strip()
    if not raw:
        return None
    display = raw.replace("\\", "/")
    if display.startswith("/") or ":" in display or re.match(r"^[a-z]:", display, re.IGNORECASE):
        return None
    display = posixpath.normpath(display)
    if display in {"", ".", ".."} or display.startswith("../"):
        return None
    if Path(display).suffix.lower() not in {".html", ".htm"}:
        return None
    # Keep the comparison key exact. Windows normally resolves casing for us,
    # while case-sensitive NTFS/UNC workspaces may legitimately contain both
    # A.html and a.html and must not collapse their permissions.
    return display, display


def web_validation_policy_key(root: Path | None, display_path: str) -> str:
    """Return a file-identity key for per-run validation limits."""
    display = posixpath.normpath(str(display_path or "").replace("\\", "/"))
    if root is not None and display not in {"", ".", ".."}:
        candidate = root.joinpath(*display.split("/"))
        try:
            stat = candidate.stat()
        except (OSError, ValueError):
            pass
        else:
            return f"file:{stat.st_dev}:{stat.st_ino}"
    # Do not case-fold a path that does not exist yet. Case-sensitive NTFS and
    # UNC workspaces can legitimately contain A.html and a.html. Existing
    # aliases are already coalesced by the filesystem identity branch above.
    return f"path:{display}"


def safe_relative_effect_path(raw: Any) -> str | None:
    """Normalise one workspace-relative effect path, or reject it."""
    display = posixpath.normpath(str(raw or "").strip().replace("\\", "/"))
    if (
        display in {"", ".", ".."}
        or display.startswith("/")
        or display.startswith("../")
        or ":" in display
    ):
        return None
    return display


def relative_tool_effect_paths(
    name: str,
    args: dict[str, Any],
    root: Path | None = None,
) -> list[str]:
    """Return every safe workspace path a trackable tool can mutate."""
    if name == "move":
        # Moving/renaming removes the source and creates or replaces the target.
        src = safe_relative_effect_path(args.get("src"))
        dst = safe_relative_effect_path(args.get("dst"))
        if src is None or dst is None:
            raw_paths: list[Any] = [src, dst]
        else:
            raw_dst = str(args.get("dst") or "")
            dst_path = root.joinpath(*dst.split("/")) if root is not None else None
            destination_is_directory = bool(
                raw_dst.endswith(("/", "\\"))
                or (
                    dst_path is not None
                    and (
                        dst_path.is_dir()
                        or (not dst_path.exists() and Path(dst).suffix == "")
                    )
                )
            )
            effective_dst = posixpath.join(dst, Path(src).name) if destination_is_directory else dst
            raw_paths = [src, effective_dst]
    else:
        raw_paths = [args.get("path") or args.get("dst")]
    paths: list[str] = []
    for raw in raw_paths:
        display = safe_relative_effect_path(raw)
        if display is not None and display not in paths:
            paths.append(display)
    return paths


def relative_tool_effect_path(args: dict[str, Any]) -> str | None:
    """Backward-compatible single-effect helper used by focused policy tests."""
    return safe_relative_effect_path(args.get("path") or args.get("dst"))


def display_path_key(display_path: str) -> str:
    return posixpath.normpath(str(display_path or "").replace("\\", "/"))


def workspace_paths_match(root: Path | None, left: str, right: str) -> bool:
    """Compare exact planned paths, falling back to filesystem identity."""
    left_key = display_path_key(left)
    right_key = display_path_key(right)
    if left_key == right_key:
        return True
    if root is None:
        return bool(os.name == "nt" and left_key.casefold() == right_key.casefold())
    try:
        left_path = root.joinpath(*left_key.split("/"))
        right_path = root.joinpath(*right_key.split("/"))
        if left_path.exists() and right_path.exists():
            return left_path.samefile(right_path)
    except (OSError, ValueError):
        pass
    # The common Windows workspace is case-insensitive. If either planned path
    # does not exist yet, fail closed so casing cannot bypass a no-edit contract.
    return bool(os.name == "nt" and left_key.casefold() == right_key.casefold())


def workspace_effect_covers_path(
    root: Path | None,
    effect_path: str,
    protected_path: str,
) -> bool:
    """Return true when a direct or directory effect can mutate a protected path."""
    if workspace_paths_match(root, effect_path, protected_path):
        return True
    effect_key = display_path_key(effect_path).rstrip("/")
    protected_key = display_path_key(protected_path)
    if protected_key.startswith(f"{effect_key}/"):
        return True
    if os.name == "nt" and protected_key.casefold().startswith(f"{effect_key.casefold()}/"):
        return True
    if root is not None:
        try:
            resolved_effect = root.joinpath(*effect_key.split("/")).resolve(strict=False)
            resolved_protected = root.joinpath(*protected_key.split("/")).resolve(strict=False)
            if resolved_effect == resolved_protected or resolved_effect in resolved_protected.parents:
                return True
        except (OSError, ValueError, RuntimeError):
            # Resolution failures stay fail-closed for casing aliases above; an
            # unrelated exact lexical path is not treated as the same file.
            pass
    return False


def workspace_file_fingerprint(root: Path | None, display_path: str) -> str:
    """Hash current file bytes for an exact task-start mutation baseline."""
    if root is None:
        return "unavailable"
    display = posixpath.normpath(str(display_path or "").replace("\\", "/"))
    if display in {"", ".", ".."} or display.startswith("../") or ":" in display:
        return "invalid"
    try:
        root_resolved = root.resolve()
        target = root.joinpath(*display.split("/")).resolve()
        if not target.is_relative_to(root_resolved):
            return "invalid"
        if not target.exists():
            return "missing"
        if not target.is_file():
            return "not-file"
        if target.stat().st_size > MAX_CODE_FILE_BYTES:
            return "oversize"
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    except (OSError, ValueError):
        return "unavailable"


def non_html_file_tokens(text: str) -> list[str]:
    """Extract explicitly named local dependency files such as app.js or style.css."""
    tokens: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        display = safe_relative_effect_path(raw)
        if display is None:
            return
        suffix = Path(display).suffix.lower()
        if (
            not re.fullmatch(r"\.[a-z][a-z0-9]{0,15}", suffix, re.IGNORECASE)
            or suffix in {".html", ".htm"}
            or display in seen
        ):
            return
        seen.add(display)
        tokens.append(display)

    source = str(text or "")
    # A path containing spaces or Unicode prose characters must be quoted so it
    # can be distinguished from the instruction around it.
    for opener, closer in (("`", "`"), ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        cursor = 0
        while cursor < len(source):
            start = source.find(opener, cursor)
            if start < 0:
                break
            end = source.find(closer, start + len(opener))
            if end < 0:
                break
            body = source[start + len(opener):end]
            if "\n" not in body and "\r" not in body:
                add(body)
            cursor = end + len(closer)
    for match in re.finditer(
        r"(?<![a-z0-9_:/\\.-])([a-z0-9_.-]+(?:[/\\][a-z0-9_.-]+)*\."
        r"[a-z][a-z0-9]{0,15})(?![a-z0-9_])",
        source,
        flags=re.IGNORECASE,
    ):
        add(match.group(1))
    return tokens


def request_explicitly_preserves_path(text: str, path: str) -> bool:
    """Return true only when this exact path is an explicit no-mutation target."""
    normalized = " ".join(str(text or "").casefold().split())
    escaped = re.escape(path.casefold())
    wrapped = rf"(?:[`\"'“‘]\s*)?{escaped}(?:\s*[`\"'”’])?"
    english_verb = (
        r"(?:edit|update|fix|change|modify|rewrite|create|build|implement|write|"
        r"add|generate|make|save|replace|patch|refactor|delete|remove|rename|move|touch)"
    )
    english_file_item = (
        r"(?:[`\"'“‘]\s*)?[a-z0-9_.-]+(?:[/\\][a-z0-9_.-]+)*\."
        r"[a-z][a-z0-9]{0,15}(?:\s*[`\"'”’])?"
    )
    english_connector = r"(?:,|and|or|&|as\s+well\s+as|plus)"
    english_target_list = (
        rf"(?:{english_file_item}\s*{english_connector}\s*)*{wrapped}"
        rf"(?:\s*{english_connector}\s*{english_file_item})*"
    )
    if re.search(
        rf"\b(?:leave|keep)\s+(?:only\s+)?(?:the\s+)?(?:file\s+)?"
        rf"{wrapped}\s+unchanged\b|"
        rf"\b(?:do\s+not|don't|dont)\s+(?:please\s+)?{english_verb}\s+"
        rf"(?:only\s+)?(?:the\s+)?(?:file\s+)?{wrapped}(?=$|[\s,.;:!?])|"
        rf"\bwithout\s+(?:editing|updating|fixing|changing|modifying|rewriting|"
        rf"creating|building|implementing|writing|adding|generating|making|saving|"
        rf"replacing|patching|refactoring|deleting|removing|renaming|moving|touching)\s+"
        rf"(?:only\s+)?(?:the\s+)?(?:file\s+)?{wrapped}(?=$|[\s,.;:!?])",
        normalized,
    ):
        return True
    if re.search(
        rf"\b(?:do\s+not|don't|dont)\s+(?:please\s+)?{english_verb}\s+"
        rf"(?:only\s+)?(?:the\s+)?(?:files?\s+)?{english_target_list}"
        rf"(?=$|[\s,.;:!?])|"
        rf"\bwithout\s+(?:editing|updating|fixing|changing|modifying|rewriting|"
        rf"creating|building|implementing|writing|adding|generating|making|saving|"
        rf"replacing|patching|refactoring|deleting|removing|renaming|moving|touching)\s+"
        rf"(?:only\s+)?(?:the\s+)?(?:files?\s+)?{english_target_list}"
        rf"(?=$|[\s,.;:!?])",
        normalized,
    ):
        return True
    korean_file_item = (
        r"(?:[`\"'“‘]\s*)?[a-z0-9_.-]+(?:[/\\][a-z0-9_.-]+)*\."
        r"[a-z][a-z0-9]{0,15}(?:\s*[`\"'”’])?(?:은|는|을|를|도|만)?"
    )
    korean_connector = r"(?:,|와|과|랑|이랑|및|하고|&|그리고)"
    korean_target_list = (
        rf"(?:{korean_file_item}\s*{korean_connector}\s*)*"
        rf"{wrapped}(?:은|는|을|를|도|만)?"
        rf"(?:\s*{korean_connector}\s*{korean_file_item})*"
    )
    if re.search(
        rf"{korean_target_list}\s*(?:"
        r"(?:수정|편집|업데이트|변경|작성|생성|구현|추가|삭제|제거|저장|교체|이동|복사)"
        r"(?:하)?지\s*(?:말|않)|"
        r"(?:고치|바꾸|만들|건드리)지\s*(?:말|않)|"
        r"(?:수정|편집|업데이트|변경|작성|생성|구현|추가)\s*없이|"
        r"그대로\s*(?:두|유지))",
        normalized,
    ):
        return True
    return bool(re.search(
        rf"{wrapped}(?:은|는|을|를|도|만)?\s*(?:"
        r"(?:수정|편집|업데이트|변경|작성|생성|구현|추가|삭제|제거|저장|교체|이동|복사)"
        r"(?:하)?지\s*(?:말|않)|"
        r"(?:고치|바꾸|만들|건드리)지\s*(?:말|않)|"
        r"(?:수정|편집|업데이트|변경|작성|생성|구현|추가)\s*없이|"
        r"그대로\s*(?:두|유지))",
        normalized,
    ))


def request_directly_mutates_dependency_path(text: str, path: str) -> bool:
    """Recognise a non-HTML file explicitly named as a mutation target."""
    normalized = " ".join(str(text or "").casefold().split())
    escaped = re.escape(path.casefold())
    wrapped = rf"(?:[`\"'“‘]\s*)?{escaped}(?:\s*[`\"'”’])?"
    if request_explicitly_preserves_path(normalized, path):
        return False
    file_item = r"(?:[`\"'“‘]\s*)?[a-z0-9_.-]+(?:[/\\][a-z0-9_.-]+)*\.[a-z][a-z0-9]{0,15}(?:\s*[`\"'”’])?"
    if re.search(
        rf"\b{_ENGLISH_MUTATION_VERB}\s+(?:only\s+)?(?:the\s+)?(?:file\s+)?"
        rf"(?:{file_item}\s*(?:,|and(?:\s+then)?|as\s+well\s+as|plus|&|then|next)\s*)*"
        rf"{wrapped}(?=$|[\s,.;:!?])",
        normalized,
    ):
        return True
    if re.search(
        rf"\b(?:rename|move)\s+(?:only\s+)?(?:the\s+)?(?:file\s+)?"
        rf"(?:{file_item})\s+to\s+(?:the\s+)?{wrapped}(?=$|[\s,.;:!?])",
        normalized,
    ):
        return True
    if re.search(
        rf"\b(?:after|once)\s+(?:editing|updating|fixing|changing|modifying|rewriting|"
        rf"creating|building|implementing|writing|adding|generating|making|saving|"
        rf"replacing|patching|refactoring|deleting|removing|renaming|moving)"
        rf"\s+(?:only\s+)?(?:the\s+)?{wrapped}(?=$|[\s,.;:!?])",
        normalized,
    ):
        return True
    korean_item = (
        r"(?:[`\"'“‘]\s*)?[a-z0-9_.-]+(?:[/\\][a-z0-9_.-]+)*\."
        r"[a-z][a-z0-9]{0,15}(?:\s*[`\"'”’])?(?:을|를|은|는|도|만)?"
    )
    connector = r"(?:,|와|과|랑|이랑|및|하고|&|그리고|그다음|다음|then|next)"
    korean_action = (
        r"(?:(?:수정|편집|업데이트|변경|작성|생성|구현|추가|삭제|제거|저장|교체|이동|복사)"
        r"(?:하고|해서|해|할|하자|해줘|해주세요|한\s*(?:뒤|후|다음)|\s*(?:후|뒤))|"
        r"(?:고치|바꾸|만들)(?:고|어서|어|자|어줘|어주세요|고\s*나서|고\s*난\s*(?:뒤|후)))"
    )
    return bool(re.search(
        rf"(?:{korean_item}\s*{connector}\s*)*"
        rf"{wrapped}(?:을|를|은|는|도|만)?"
        rf"(?:\s*{connector}\s*{korean_item})*\s*"
        rf"(?:모두|둘\s*다|전부)?\s*{korean_action}",
        normalized,
    ))


def validation_target_map(paths: list[str] | None) -> dict[str, str]:
    targets: dict[str, str] = {}
    for path in paths or []:
        target = html_entry_path({"path": path})
        if target is not None:
            targets[target[0]] = target[1]
    return targets


def authoritative_html_inventory_result(candidates: list[str] | None) -> str:
    """Render the bounded harness inventory without re-scanning via an LLM tool."""
    if candidates is None:
        return (
            "[AISO_HTML_INVENTORY v1]\nstatus=INDETERMINATE\n"
            "작업 폴더의 HTML 목록을 안전 한도 안에서 확정하지 못했습니다. "
            "상대 경로를 사용자에게 직접 지정받아야 합니다."
        )
    if not candidates:
        return "[AISO_HTML_INVENTORY v1]\nstatus=COMPLETE\nHTML 후보가 없습니다."
    return (
        "[AISO_HTML_INVENTORY v1]\nstatus=COMPLETE\n"
        "다음 경로는 Aiso가 확정한 읽기 전용 HTML 후보입니다:\n"
        + "\n".join(f"- {path}" for path in candidates)
    )


def provider_safe_web_validation_result(result: Any) -> str:
    """Keep local diagnostics out of a subsequent external-provider request."""
    text = str(result or "")
    match = re.search(r"(?m)^status=(PASS|FAIL|INCONCLUSIVE)\s+level=([a-z]+)\b", text)
    status = match.group(1) if match else "INCONCLUSIVE"
    level = match.group(2) if match else "runtime"
    lines = [
        "[WEB_VALIDATION v1 REDACTED]",
        f"status={status} level={level}",
        "summary=상세 콘솔·예외·URL·페이지 내용은 로컬 결과 카드에만 보관됩니다.",
    ]
    runtime = re.search(r"(?m)^runtime=(PASS|FAIL)(?:\s+errors=(\d+)|\s+canvas_blank=true)?", text)
    if runtime:
        suffix = f" errors={runtime.group(2)}" if runtime.group(2) else ""
        lines.append(f"runtime={runtime.group(1)}{suffix}")
    for label in ("네트워크", "팝업", "다운로드", "대화상자", "로컬 파일"):
        count = re.search(rf"(?m)^security={label}\s+차단\s+(\d+)건$", text)
        if count:
            lines.append(f"security={label} 차단 {count.group(1)}건")
    return "\n".join(lines)


def web_validation_status(result: Any) -> str | None:
    match = re.search(
        r"^\[WEB_VALIDATION v1\]\r?\nstatus=(PASS|FAIL|INCONCLUSIVE)\b",
        str(result or ""),
    )
    return match.group(1) if match is not None else None


def unverified_html_notice(pending: dict[str, str]) -> dict[str, str]:
    return {
        "type": "notice",
        "text": (
            "⚠ 웹 검증 미완료: 변경한 HTML이 run_web PASS로 확인되지 않았습니다. "
            f"대상: {', '.join(pending.values())}"
        ),
    }


def existing_web_validation_notice(
    run_web_available: bool,
    *,
    run_requested: bool = False,
    run_started: bool = False,
    candidates: list[str] | None = None,
    missing: list[str] | None = None,
) -> dict[str, str]:
    if run_web_available:
        if missing:
            text = (
                "⚠ 기존 웹 산출물 검증 미완료: 지정된 대상 일부에 run_web 실행 결과가 없습니다. "
                f"미검증 대상: {', '.join(missing)} 원본 파일은 변경하지 않았습니다."
            )
        elif run_started:
            text = (
                "⚠ 기존 웹 산출물 검증 실패: run_web 실행은 시작됐지만 유효한 "
                "PASS·FAIL·INCONCLUSIVE 보고서를 반환하지 못했습니다. 원본 파일은 변경하지 않았습니다."
            )
        elif run_requested:
            text = (
                "⚠ 기존 웹 산출물 검증 미실행: 실행 승인이 거부·만료되었거나 "
                "run_web이 실제로 시작되기 전에 중단되었습니다. 원본 파일은 변경하지 않았습니다."
            )
        elif candidates == []:
            text = (
                "기존 웹 산출물을 찾지 못해 실행 검증을 시작하지 않았습니다. "
                "대체 파일을 새로 만들지 않았습니다."
            )
        elif candidates is not None and len(candidates) > 1:
            text = (
                "검증할 HTML 후보가 여러 개여서 임의로 실행하지 않았습니다. "
                f"대상 후보: {', '.join(candidates)}. 검증할 상대 경로를 정확히 입력해 주세요."
            )
        elif candidates is None:
            text = (
                "⚠ 기존 웹 산출물 검증 미실행: 작업 폴더의 HTML 목록을 안전 한도 안에서 "
                "확정하지 못했습니다. 검증할 HTML 상대 경로를 직접 지정해 다시 요청해 주세요. "
                "원본 파일은 변경하지 않았습니다."
            )
        else:
            text = (
                "⚠ 기존 웹 산출물 검증 미실행: run_web 결과가 없습니다. "
                "원본 파일은 새로 만들거나 수정하지 않았습니다."
            )
    else:
        text = (
            "⚠ 웹 실행 검증 도구가 꺼져 있어 기존 웹 산출물을 검증하지 못했습니다. "
            "설정의 도구 항목에서 웹 실행 검증을 켠 뒤 다시 요청해 주세요."
        )
    return {"type": "notice", "text": text}


__all__ = [
    "authoritative_html_inventory_result",
    "display_path_key",
    "existing_web_validation_notice",
    "html_entry_path",
    "non_html_file_tokens",
    "provider_safe_web_validation_result",
    "relative_tool_effect_path",
    "relative_tool_effect_paths",
    "request_directly_mutates_dependency_path",
    "request_explicitly_preserves_path",
    "safe_relative_effect_path",
    "unverified_html_notice",
    "validation_target_map",
    "web_validation_policy_key",
    "web_validation_status",
    "workspace_effect_covers_path",
    "workspace_file_fingerprint",
    "workspace_paths_match",
]
