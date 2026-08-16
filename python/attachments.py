"""User-selected attachment store for Chat and Agent.

Electron copies explicit file/folder selections into ``AISO_ATTACHMENTS_DIR``.
The renderer sends only opaque UUID handles; this module validates those handles
and never accepts an arbitrary local path from an LLM or renderer request.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from extract import EXTRACTORS, ExtractError

MAX_ATTACHMENT_IDS = 16
MAX_CONTEXT_CHARS = 120_000
MAX_SINGLE_TEXT_CHARS = 30_000
MAX_IMAGE_COUNT = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 10 * 1024 * 1024
_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm"})
_SKIP_PARTS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})


class AttachmentError(ValueError):
    pass


@dataclass(frozen=True)
class AttachmentContext:
    text: str
    images: tuple[str, ...]
    names: tuple[str, ...]


def _root() -> Path:
    raw = os.environ.get("AISO_ATTACHMENTS_DIR", "").strip()
    if not raw:
        raise AttachmentError("첨부 저장소가 준비되지 않았습니다.")
    return Path(raw).resolve()


def normalize_attachment_ids(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > MAX_ATTACHMENT_IDS:
        raise AttachmentError(f"첨부는 최대 {MAX_ATTACHMENT_IDS}개까지 전달할 수 있습니다.")
    ids: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise AttachmentError("첨부 식별자가 올바르지 않습니다.")
        if value in ids:
            raise AttachmentError("동일한 첨부 파일이 중복되었습니다.")
        ids.append(value)
    return tuple(ids)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _safe_attachment_root(identifier: str) -> tuple[Path, dict[str, Any]]:
    root = _root()
    item_root = (root / identifier).resolve()
    if not _within(item_root, root) or not item_root.is_dir():
        raise AttachmentError("첨부 파일을 찾을 수 없습니다. 다시 추가해 주세요.")
    manifest = item_root / "manifest.json"
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AttachmentError("첨부 파일 정보가 손상되었습니다. 다시 추가해 주세요.") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
        raise AttachmentError("첨부 파일 정보가 올바르지 않습니다.")
    if Path(raw["name"]).name != raw["name"]:
        raise AttachmentError("첨부 파일 경로가 올바르지 않습니다.")
    return item_root, raw


def _iter_files(root: Path, name: str) -> Iterable[Path]:
    source = (root / name).resolve()
    if not _within(source, root) or not source.exists():
        raise AttachmentError("첨부 파일 경로가 올바르지 않습니다.")
    if source.is_file():
        yield source
        return
    if not source.is_dir():
        raise AttachmentError("첨부 항목 형식이 올바르지 않습니다.")
    for path in sorted(source.rglob("*"), key=lambda item: str(item).casefold()):
        if any(part.casefold() in _SKIP_PARTS for part in path.relative_to(source).parts):
            continue
        if path.is_symlink():
            continue
        if path.is_file() and _within(path, source):
            yield path


def _read_text(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in EXTRACTORS:
        fn, _label = EXTRACTORS[extension]
        try:
            return (fn(path) or "").strip()
        except ExtractError:
            return ""
    if extension in _TEXT_EXTENSIONS:
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
    return ""


def build_attachment_context(raw_ids: Any) -> AttachmentContext:
    identifiers = normalize_attachment_ids(raw_ids)
    if not identifiers:
        return AttachmentContext("", (), ())

    sections: list[str] = []
    image_payloads: list[str] = []
    image_bytes = 0
    names: list[str] = []
    remaining = MAX_CONTEXT_CHARS
    for identifier in identifiers:
        item_root, manifest = _safe_attachment_root(identifier)
        name = str(manifest["name"])
        names.append(name)
        files = list(_iter_files(item_root, name))
        listing = ", ".join(path.relative_to(item_root).as_posix() for path in files[:80])
        if len(files) > 80:
            listing += f" … and {len(files) - 80} more"
        body: list[str] = [f"[Attachment: {name}]", f"File list: {listing or '(empty)'}"]
        for path in files:
            if len(image_payloads) < MAX_IMAGE_COUNT and path.suffix.lower() in _IMAGE_EXTENSIONS:
                try:
                    size = path.stat().st_size
                    if size <= MAX_IMAGE_BYTES and image_bytes + size <= MAX_TOTAL_IMAGE_BYTES:
                        image_payloads.append(base64.b64encode(path.read_bytes()).decode("ascii"))
                        image_bytes += size
                except OSError:
                    pass
            extracted = _read_text(path)
            if not extracted:
                continue
            extracted = extracted[:MAX_SINGLE_TEXT_CHARS]
            relative = path.relative_to(item_root).as_posix()
            body.append(f"\n--- {relative} ---\n{extracted}")
            if sum(len(part) for part in body) >= remaining:
                break
        section = "\n".join(body)
        if len(section) > remaining:
            sections.append(section[:remaining] + "\n[Attachment context truncated due to length limit]")
            remaining = 0
            break
        sections.append(section)
        remaining -= len(section)
        if remaining <= 0:
            break
    return AttachmentContext("\n\n".join(sections), tuple(image_payloads), tuple(names))


def append_attachment_context(messages: list[dict[str, Any]], raw_ids: Any, *, allow_images: bool) -> list[dict[str, Any]]:
    context = build_attachment_context(raw_ids)
    if not context.text and not context.images:
        return messages
    copied = [dict(message) for message in messages]
    target_index = next((index for index in range(len(copied) - 1, -1, -1) if copied[index].get("role") == "user"), None)
    if target_index is None:
        raise AttachmentError("첨부 파일에는 함께 보낼 사용자 요청이 필요합니다.")
    current = str(copied[target_index].get("content") or "")
    copied[target_index]["content"] = (
        f"{current}\n\n## User-attached material\n"
        "The following material was attached directly by the user. Base the answer on the file list and extracted source text; do not guess content that could not be read.\n"
        f"{context.text}"
    ).strip()
    if allow_images and context.images:
        copied[target_index]["images"] = list(context.images)
    elif context.images:
        copied[target_index]["content"] += (
            "\n\n[Attached image notice] The selected provider does not receive the image binary. "
            "Use only the file name and do not guess image contents. Image analysis is available with a "
            "model that supports local Ollama vision input, such as Gemma 4."
        )
    return copied
