from __future__ import annotations

import asyncio
import base64

import pytest

import discord_attachments as attachments


class FakeAttachment:
    def __init__(self, filename: str, payload: bytes, *, size: int | None = None) -> None:
        self.filename = filename
        self.payload = payload
        self.size = len(payload) if size is None else size
        self.read_calls = 0

    async def read(self) -> bytes:
        self.read_calls += 1
        return self.payload


def run(coro):
    return asyncio.run(coro)


def test_discord_attachment_reuses_document_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_pptx(path):
        assert path.name == "plan.pptx"
        return "슬라이드 원문: 핵심 기능을 구현한다"

    monkeypatch.setitem(attachments.EXTRACTORS, ".pptx", (fake_pptx, "PowerPoint"))
    context = run(attachments.build_discord_attachment_context(
        [FakeAttachment("plan.pptx", b"not-a-real-pptx")], allow_images=False
    ))

    assert context.names == ("plan.pptx",)
    assert "슬라이드 원문" in context.text
    assert not context.notices


def test_discord_attachment_rejects_path_escape_before_reading() -> None:
    item = FakeAttachment("../secret.txt", b"secret")

    with pytest.raises(attachments.DiscordAttachmentError, match="이름"):
        run(attachments.build_discord_attachment_context([item], allow_images=False))
    assert item.read_calls == 0


def test_discord_attachment_nonvision_model_gets_no_image_bytes() -> None:
    payload = b"\x89PNG\r\n\x1a\nimage"
    context = run(attachments.build_discord_attachment_context(
        [FakeAttachment("scene.png", payload)], allow_images=False
    ))

    assert not context.images
    assert "Do not guess image contents" in context.text


def test_discord_attachment_gemma_vision_context_has_bounded_image_payload() -> None:
    payload = b"\x89PNG\r\n\x1a\nimage"
    context = run(attachments.build_discord_attachment_context(
        [FakeAttachment("scene.png", payload)], allow_images=True
    ))

    assert context.images == (base64.b64encode(payload).decode("ascii"),)
    messages = attachments.append_discord_attachment_context(
        [{"role": "user", "content": "무엇이 보이나요?"}], context
    )
    assert messages[0]["images"] == list(context.images)


def test_discord_attachment_unsupported_type_is_disclosed_without_guessing() -> None:
    context = run(attachments.build_discord_attachment_context(
        [FakeAttachment("archive.exe", b"binary")], allow_images=False
    ))

    assert "The file format is unsupported" in context.text
    assert any("archive.exe" in notice for notice in context.notices)


def test_discord_attachment_context_only_changes_latest_user_message() -> None:
    context = run(attachments.build_discord_attachment_context(
        [FakeAttachment("notes.txt", "기획 원문".encode())], allow_images=False
    ))
    messages = attachments.append_discord_attachment_context(
        [
            {"role": "user", "content": "이전 요청"},
            {"role": "assistant", "content": "이전 답변"},
            {"role": "user", "content": "요약해줘"},
        ],
        context,
    )

    assert messages[0]["content"] == "이전 요청"
    assert "기획 원문" in messages[-1]["content"]
    assert "## Material attached directly by the user in Discord" in messages[-1]["content"]


def test_discord_attachment_declared_limit_is_rejected_before_download(monkeypatch: pytest.MonkeyPatch) -> None:
    item = FakeAttachment("large.pdf", b"small", size=attachments.MAX_SINGLE_ATTACHMENT_BYTES + 1)

    with pytest.raises(attachments.DiscordAttachmentError, match="20MB"):
        run(attachments.build_discord_attachment_context([item], allow_images=False))
    assert item.read_calls == 0
