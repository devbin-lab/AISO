"""Bounded, ephemeral Discord attachment extraction.

Discord messages are an external input boundary. This module accepts only the
attachment objects that arrived with an already-authorized command message; it
never accepts paths or URLs supplied by an LLM. Files are staged in a temporary
directory, extracted with the same readers used by Aiso desktop attachments,
and removed before the request returns.
"""

from __future__ import annotations

import base64
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from extract import EXTRACTORS, ExtractError


MAX_DISCORD_ATTACHMENTS = 5
MAX_SINGLE_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 25 * 1024 * 1024
# 아래 다섯 개는 attachments.py 와 값이 같지만 **의도적으로 독립된 손잡이**다.
# 디스코드 첨부는 신뢰 경계가 다르다 — 데스크톱 첨부는 사용자가 직접 고른 파일이고,
# 이쪽은 서버의 아무나 올린 파일이다. 한쪽을 조일 이유가 생겼을 때 다른 쪽을 끌고
# 가지 않도록 따로 둔다. 공유 상수로 합치지 말 것.
MAX_CONTEXT_CHARS = 120_000
MAX_SINGLE_TEXT_CHARS = 30_000
MAX_IMAGE_COUNT = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 10 * 1024 * 1024

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm"})
_SAFE_FILE_NAME_RE = re.compile(r"^[^\\/:\x00-\x1f]{1,180}$")


class DiscordAttachmentError(ValueError):
    """The message attachment cannot be safely supplied to the model."""


class DiscordAttachmentLike(Protocol):
    filename: str
    size: int

    async def read(self) -> bytes: ...


@dataclass(frozen=True)
class DiscordAttachmentContext:
    text: str
    images: tuple[str, ...]
    names: tuple[str, ...]
    notices: tuple[str, ...]


def _safe_name(raw: Any) -> str:
    name = str(raw or "").strip()
    if not _SAFE_FILE_NAME_RE.fullmatch(name) or Path(name).name != name:
        raise DiscordAttachmentError("첨부 파일 이름 형식이 올바르지 않습니다.")
    return name


def _extension(name: str) -> str:
    return Path(name).suffix.casefold()


def _extract_text(path: Path, extension: str) -> str:
    if extension in EXTRACTORS:
        extractor, _label = EXTRACTORS[extension]
        try:
            return str(extractor(path) or "").strip()
        except (ExtractError, OSError, ValueError, TypeError):
            return ""
        except Exception:  # noqa: BLE001 - malformed user documents must not kill the bot
            return ""
    if extension in _TEXT_EXTENSIONS:
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
    return ""


async def _stage_attachment(
    attachment: DiscordAttachmentLike,
    directory: Path,
    *,
    total_bytes: int,
) -> tuple[Path, str, bytes]:
    name = _safe_name(getattr(attachment, "filename", ""))
    declared_size = getattr(attachment, "size", None)
    if not isinstance(declared_size, int) or isinstance(declared_size, bool) or declared_size < 0:
        raise DiscordAttachmentError(f"{name}: 첨부 파일 크기 정보를 확인할 수 없습니다.")
    if declared_size > MAX_SINGLE_ATTACHMENT_BYTES:
        raise DiscordAttachmentError(
            f"{name}: 파일 하나는 최대 {MAX_SINGLE_ATTACHMENT_BYTES // (1024 * 1024)}MB까지 분석할 수 있습니다."
        )
    if total_bytes + declared_size > MAX_TOTAL_ATTACHMENT_BYTES:
        raise DiscordAttachmentError(
            f"첨부 파일 합계는 최대 {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)}MB까지 분석할 수 있습니다."
        )
    try:
        payload = await attachment.read()
    except Exception as error:  # noqa: BLE001 - Discord CDN/permission failures are input failures
        raise DiscordAttachmentError(f"{name}: Discord에서 파일을 가져오지 못했습니다.") from error
    if not isinstance(payload, bytes) or len(payload) > MAX_SINGLE_ATTACHMENT_BYTES:
        raise DiscordAttachmentError(f"{name}: 허용된 파일 크기를 초과했습니다.")
    if total_bytes + len(payload) > MAX_TOTAL_ATTACHMENT_BYTES:
        raise DiscordAttachmentError(
            f"첨부 파일 합계는 최대 {MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)}MB까지 분석할 수 있습니다."
        )
    path = directory / name
    path.write_bytes(payload)
    return path, name, payload


async def build_discord_attachment_context(
    attachments: Iterable[DiscordAttachmentLike], *, allow_images: bool
) -> DiscordAttachmentContext:
    """Download only current-message attachments and build bounded model input."""
    items = list(attachments)
    if not items:
        return DiscordAttachmentContext("", (), (), ())
    if len(items) > MAX_DISCORD_ATTACHMENTS:
        raise DiscordAttachmentError(f"첨부는 한 번에 최대 {MAX_DISCORD_ATTACHMENTS}개까지 분석할 수 있습니다.")

    sections: list[str] = []
    image_payloads: list[str] = []
    names: list[str] = []
    notices: list[str] = []
    remaining = MAX_CONTEXT_CHARS
    total_bytes = 0
    image_bytes = 0
    with tempfile.TemporaryDirectory(prefix="aiso-discord-attachment-") as raw_directory:
        directory = Path(raw_directory)
        for attachment in items:
            path, name, payload = await _stage_attachment(
                attachment, directory, total_bytes=total_bytes
            )
            total_bytes += len(payload)
            names.append(name)
            extension = _extension(name)
            body = [f"[Discord attachment: {name}]"]
            if extension in _IMAGE_EXTENSIONS:
                if allow_images and len(image_payloads) < MAX_IMAGE_COUNT:
                    if len(payload) <= MAX_IMAGE_BYTES and image_bytes + len(payload) <= MAX_TOTAL_IMAGE_BYTES:
                        image_payloads.append(base64.b64encode(payload).decode("ascii"))
                        image_bytes += len(payload)
                        body.append("The original image is included. Determine image contents from the original image.")
                    else:
                        notices.append(f"{name}: 이미지 분석 크기 제한으로 원본을 전달하지 않았습니다.")
                elif allow_images:
                    notices.append(f"{name}: 이미지 첨부 개수 제한으로 원본을 전달하지 않았습니다.")
                else:
                    body.append("The selected model cannot receive the original image. Do not guess image contents.")
            elif extension not in EXTRACTORS and extension not in _TEXT_EXTENSIONS:
                notices.append(f"{name}: 지원하지 않는 파일 형식입니다.")
                body.append("The file format is unsupported, so no body text could be extracted.")
            else:
                extracted = _extract_text(path, extension)
                if extracted:
                    text = extracted[:MAX_SINGLE_TEXT_CHARS]
                    body.append(f"\n--- Extracted source text ---\n{text}")
                    if len(extracted) > len(text):
                        body.append("\n[Only the beginning was included because the source is long]")
                else:
                    notices.append(f"{name}: 읽을 수 있는 텍스트를 추출하지 못했습니다.")
                    body.append("No readable text could be extracted. The source may contain only images or be damaged.")
            section = "\n".join(body)
            if len(section) > remaining:
                sections.append(section[:remaining] + "\n[Attachment context truncated due to total length limit]")
                break
            sections.append(section)
            remaining -= len(section)
            if remaining <= 0:
                break
    return DiscordAttachmentContext(
        "\n\n".join(sections), tuple(image_payloads), tuple(names), tuple(notices)
    )


def append_discord_attachment_context(
    messages: list[dict[str, Any]], context: DiscordAttachmentContext
) -> list[dict[str, Any]]:
    """Attach current-message evidence only to the last user turn."""
    if not context.text and not context.images:
        return [dict(message) for message in messages]
    copied = [dict(message) for message in messages]
    target = next(
        (index for index in range(len(copied) - 1, -1, -1) if copied[index].get("role") == "user"),
        None,
    )
    if target is None:
        raise DiscordAttachmentError("첨부 파일에는 함께 보낼 사용자 요청이 필요합니다.")
    current = str(copied[target].get("content") or "")
    copied[target]["content"] = (
        f"{current}\n\n## Material attached directly by the user in Discord\n"
        "Base the answer on the following file names and extracted source text; do not guess content that could not be read.\n"
        f"{context.text}"
    ).strip()
    if context.images:
        copied[target]["images"] = list(context.images)
    return copied


__all__ = [
    "DiscordAttachmentContext",
    "DiscordAttachmentError",
    "append_discord_attachment_context",
    "build_discord_attachment_context",
]
