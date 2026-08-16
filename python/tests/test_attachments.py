from __future__ import annotations

import json
from pathlib import Path

import pytest

from attachments import AttachmentError, append_attachment_context, build_attachment_context


def _stage(root: Path, identifier: str, name: str, content: str) -> None:
    item = root / identifier
    item.mkdir(parents=True)
    (item / name).write_text(content, encoding="utf-8")
    (item / "manifest.json").write_text(json.dumps({"name": name}), encoding="utf-8")


def test_attachment_context_uses_only_staged_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "attachments"
    identifier = "123e4567-e89b-42d3-a456-426614174000"
    _stage(root, identifier, "brief.txt", "source requirement")
    monkeypatch.setenv("AISO_ATTACHMENTS_DIR", str(root))

    context = build_attachment_context([identifier])

    assert "brief.txt" in context.text
    assert "source requirement" in context.text
    assert context.names == ("brief.txt",)


def test_attachment_context_rejects_manifest_path_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "attachments"
    identifier = "123e4567-e89b-42d3-a456-426614174000"
    item = root / identifier
    item.mkdir(parents=True)
    (item / "manifest.json").write_text(json.dumps({"name": "../outside.txt"}), encoding="utf-8")
    monkeypatch.setenv("AISO_ATTACHMENTS_DIR", str(root))

    with pytest.raises(AttachmentError):
        build_attachment_context([identifier])


def test_attachment_context_is_added_only_to_latest_user_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "attachments"
    identifier = "123e4567-e89b-42d3-a456-426614174000"
    _stage(root, identifier, "notes.md", "important note")
    monkeypatch.setenv("AISO_ATTACHMENTS_DIR", str(root))

    messages = append_attachment_context(
        [{"role": "user", "content": "old"}, {"role": "assistant", "content": "answer"}, {"role": "user", "content": "summarize"}],
        [identifier],
        allow_images=False,
    )

    assert messages[0]["content"] == "old"
    assert "important note" in messages[-1]["content"]


def test_non_vision_provider_is_told_not_to_guess_attached_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "attachments"
    identifier = "123e4567-e89b-42d3-a456-426614174000"
    item = root / identifier
    item.mkdir(parents=True)
    (item / "scene.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (item / "manifest.json").write_text(json.dumps({"name": "scene.png"}), encoding="utf-8")
    monkeypatch.setenv("AISO_ATTACHMENTS_DIR", str(root))

    messages = append_attachment_context(
        [{"role": "user", "content": "what is this?"}], [identifier], allow_images=False
    )

    assert "does not receive the image binary" in messages[0]["content"]
    assert "images" not in messages[0]


def test_attachment_wrapper_is_english_but_preserves_source_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "attachments"
    identifier = "123e4567-e89b-42d3-a456-426614174000"
    _stage(root, identifier, "notes.md", "원문은 그대로 보존된다")
    monkeypatch.setenv("AISO_ATTACHMENTS_DIR", str(root))

    messages = append_attachment_context(
        [{"role": "user", "content": "요약해줘"}], [identifier], allow_images=False
    )

    assert "## User-attached material" in messages[0]["content"]
    assert "Base the answer on the file list" in messages[0]["content"]
    assert "[Attachment: notes.md]" in messages[0]["content"]
    assert "원문은 그대로 보존된다" in messages[0]["content"]
