"""Helpers shared by the core tests (no fixtures from tests/conftest.py are changed)."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from pdf_editor.core.document import PdfDocument

SAMPLE_NAME = "Thiet_ke_VLSI_Exercise.pdf"
NOTO_REGULAR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "pdf_editor"
    / "resources"
    / "fonts"
    / "NotoSans-Regular.ttf"
)


def make_pdf_bytes(count: int, prefix: str = "P", width: float = 595, height: float = 842) -> bytes:
    """A PDF with ``count`` pages, page ``i`` carrying the text marker ``f"{prefix}{i}"``."""
    doc = pymupdf.open()
    try:
        for i in range(count):
            page = doc.new_page(width=width, height=height)
            page.insert_text((72, 72), f"{prefix}{i}", fontsize=24)
        return doc.tobytes()
    finally:
        doc.close()


def make_doc(count: int, prefix: str = "P") -> PdfDocument:
    return PdfDocument.from_bytes(make_pdf_bytes(count, prefix))


def markers(doc: PdfDocument) -> list[str]:
    """Text of each page, stripped (blank pages give "")."""
    return [page.get_text().strip() for page in doc.raw]


def rotations(doc: PdfDocument) -> list[int]:
    return [page.rotation for page in doc.raw]


def copy_sample(data_dir: Path, tmp_path: Path) -> Path:
    target = tmp_path / SAMPLE_NAME
    target.write_bytes((data_dir / SAMPLE_NAME).read_bytes())
    return target
