from __future__ import annotations

import zipfile
from html import escape
from pathlib import Path

import pytest

from extract import ExtractError, extract_pptx, pptx_to_html


def _write_pptx(path: Path, slides: list[list[str]]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for index, lines in enumerate(slides, 1):
            paragraphs = "".join(
                f"<a:p><a:r><a:t>{escape(line)}</a:t></a:r></a:p>" for line in lines
            )
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                "<p:sld xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\" "
                "xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\">"
                f"<p:cSld><p:spTree>{paragraphs}</p:spTree></p:cSld></p:sld>",
            )


def test_extract_pptx_keeps_slide_boundaries(tmp_path: Path) -> None:
    source = tmp_path / "deck.pptx"
    _write_pptx(source, [["첫 슬라이드", "핵심 기능"], ["둘째 슬라이드"]])

    assert extract_pptx(source) == "# 슬라이드 1\n첫 슬라이드\n핵심 기능\n\n# 슬라이드 2\n둘째 슬라이드"


def test_pptx_to_html_is_text_safe_and_keeps_slide_headings(tmp_path: Path) -> None:
    source = tmp_path / "deck.pptx"
    destination = tmp_path / "rendered" / "deck.html"
    _write_pptx(source, [["<제목>", "내용 & 근거"]])

    pptx_to_html(source, destination)

    rendered = destination.read_text(encoding="utf-8")
    assert "<h2>슬라이드 1</h2>" in rendered
    assert "&lt;제목&gt;" in rendered
    assert "내용 &amp; 근거" in rendered


def test_extract_pptx_rejects_invalid_zip(tmp_path: Path) -> None:
    source = tmp_path / "broken.pptx"
    source.write_bytes(b"not a pptx")

    with pytest.raises(ExtractError, match="Office 문서"):
        extract_pptx(source)
