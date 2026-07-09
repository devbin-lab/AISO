"""문서 파일 텍스트 추출 — 에이전트 read_file이 PDF/엑셀/워드/한글 등을 읽게 한다.

각 추출기는 텍스트를 돌려주거나 ExtractError를 던진다. (tools.py가 ToolError로 변환)
코드·md·csv·txt 등 순수 텍스트는 tools.read_file의 기본 경로가 처리한다.
"""

from __future__ import annotations

import re
import zlib
from pathlib import Path


class ExtractError(Exception):
    """문서 추출 실패 — 사유는 모델에게 그대로 전달된다."""


# ---------- PDF ----------

def extract_pdf(target: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ExtractError("PDF 읽기 라이브러리(pypdf)가 설치되어 있지 않습니다.")
    try:
        reader = PdfReader(str(target))
    except Exception as e:  # noqa: BLE001
        raise ExtractError(f"PDF를 열 수 없습니다: {e}")
    total = len(reader.pages)
    parts: list[str] = []
    for i, page in enumerate(reader.pages):
        if i >= 60:
            parts.append(f"\n…(총 {total}쪽 중 60쪽까지만 표시)")
            break
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            parts.append("")
    return "\n\n".join(parts)


# ---------- 엑셀 (XLSX/XLSM) ----------

def extract_xlsx(target: Path) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ExtractError("엑셀 읽기 라이브러리(openpyxl)가 설치되어 있지 않습니다.")
    try:
        wb = load_workbook(str(target), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise ExtractError(f"엑셀을 열 수 없습니다: {e}")
    out: list[str] = []
    try:
        for ws in wb.worksheets:
            out.append(f"# 시트: {ws.title}")
            for r, row in enumerate(ws.iter_rows(values_only=True)):
                if r >= 500:
                    out.append("…(행 500개 초과 생략)")
                    break
                cells = [("" if c is None else str(c)) for c in row]
                if any(c.strip() for c in cells):
                    out.append("\t".join(cells))
    finally:
        wb.close()
    return "\n".join(out)


def extract_xls(target: Path) -> str:
    raise ExtractError("구형 .xls 형식은 지원하지 않습니다. 엑셀에서 .xlsx로 저장 후 다시 시도하세요.")


# ---------- 워드 (DOCX) ----------

def extract_docx(target: Path) -> str:
    try:
        import docx
    except ImportError:
        raise ExtractError("워드 읽기 라이브러리(python-docx)가 설치되어 있지 않습니다.")
    try:
        d = docx.Document(str(target))
    except Exception as e:  # noqa: BLE001
        raise ExtractError(f"워드 문서를 열 수 없습니다: {e}")
    parts: list[str] = [p.text for p in d.paragraphs]
    for tbl in d.tables:
        for row in tbl.rows:
            parts.append("\t".join(c.text for c in row.cells))
    return "\n".join(parts)


# ---------- 한글 HWPX (ZIP + XML) ----------

def extract_hwpx(target: Path) -> str:
    import zipfile
    from xml.etree import ElementTree as ET

    parts: list[str] = []
    try:
        with zipfile.ZipFile(str(target)) as z:
            names = sorted(
                n for n in z.namelist() if n.startswith("Contents/") and n.endswith(".xml") and "section" in n.lower()
            )
            if not names:
                names = sorted(n for n in z.namelist() if n.endswith(".xml") and "section" in n.lower())
            for name in names:
                xml = z.read(name).decode("utf-8", errors="replace")
                try:
                    root = ET.fromstring(xml)
                    for el in root.iter():
                        tag = el.tag.rsplit("}", 1)[-1]
                        if tag == "p":
                            parts.append("\n")
                        elif tag == "t" and el.text:
                            parts.append(el.text)
                except ET.ParseError:
                    parts.append(re.sub(r"<[^>]+>", "", xml))
    except zipfile.BadZipFile:
        raise ExtractError("HWPX(ZIP) 파일을 열 수 없습니다.")
    return "".join(parts)


# ---------- 한글 HWP 5.0 (OLE 복합 파일) ----------

_EXT_CTRL = frozenset({1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23})
_INLINE_CTRL = frozenset({4, 5, 6, 7, 8, 9, 19, 20})
_HWPTAG_PARA_TEXT = 67


def _decode_hwp_text(data: bytes) -> str:
    out: list[str] = []
    i = 0
    n = len(data) - (len(data) % 2)
    while i < n:
        code = data[i] | (data[i + 1] << 8)
        if code < 32:
            if code in (10, 13):
                out.append("\n")
                i += 2
            elif code in _EXT_CTRL or code in _INLINE_CTRL:
                i += 16  # 확장/인라인 컨트롤은 8 wchar 차지
            else:
                i += 2
        else:
            out.append(chr(code))
            i += 2
    return "".join(out)


def _hwp_section_text(buf: bytes) -> str:
    out: list[str] = []
    p = 0
    n = len(buf)
    while p + 4 <= n:
        header = int.from_bytes(buf[p:p + 4], "little")
        p += 4
        tag = header & 0x3FF
        size = (header >> 20) & 0xFFF
        if size == 0xFFF:
            size = int.from_bytes(buf[p:p + 4], "little")
            p += 4
        data = buf[p:p + size]
        p += size
        if tag == _HWPTAG_PARA_TEXT:
            out.append(_decode_hwp_text(data))
    return "\n".join(out)


def extract_hwp(target: Path) -> str:
    try:
        import olefile
    except ImportError:
        raise ExtractError("한글(HWP) 읽기 라이브러리(olefile)가 설치되어 있지 않습니다.")
    if not olefile.isOleFile(str(target)):
        raise ExtractError("HWP 5.0 형식이 아닙니다 (구형 HWP 3.0 등은 미지원).")
    ole = olefile.OleFileIO(str(target))
    try:
        compressed = True
        if ole.exists("FileHeader"):
            fh = ole.openstream("FileHeader").read()
            if len(fh) > 36:
                compressed = bool(fh[36] & 0x01)
        sections = [
            e for e in ole.listdir()
            if len(e) == 2 and e[0] == "BodyText" and e[1].lower().startswith("section")
        ]
        sections.sort(key=lambda e: int((re.search(r"(\d+)", e[1]) or re.match("0", "0")).group()))
        out: list[str] = []
        for entry in sections:
            raw = ole.openstream(entry).read()
            if compressed:
                try:
                    raw = zlib.decompress(raw, -15)  # raw deflate
                except Exception:  # noqa: BLE001
                    continue
            out.append(_hwp_section_text(raw))
        text = "\n".join(out)
        if not text.strip():
            raise ExtractError("HWP에서 텍스트를 추출하지 못했습니다.")
        return text
    finally:
        ole.close()


# 확장자 → (추출기, 라벨)
EXTRACTORS = {
    ".pdf": (extract_pdf, "PDF"),
    ".xlsx": (extract_xlsx, "엑셀"),
    ".xlsm": (extract_xlsx, "엑셀"),
    ".xls": (extract_xls, "엑셀"),
    ".docx": (extract_docx, "워드"),
    ".hwp": (extract_hwp, "한글 HWP"),
    ".hwpx": (extract_hwpx, "한글 HWPX"),
}
