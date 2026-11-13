"""Tests for pdf_editor.core.fonts (embedded font reuse and fallback)."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest

from pdf_editor.core import fonts
from pdf_editor.core.document import PdfDocument
from pdf_editor.core.fonts import (
    FONT_DIR,
    fallback_font,
    register_font,
    resolve_font,
    resource_name,
)
from pdf_editor.core.text import TextSpan, get_spans

SAMPLE = "Thiet_ke_VLSI_Exercise.pdf"
VIET = "Thiết kế vi mạch Đường Ữ"


@pytest.fixture
def doc(data_dir: Path, tmp_path: Path):
    dst = tmp_path / SAMPLE
    shutil.copyfile(data_dir / SAMPLE, dst)
    with PdfDocument.open(dst) as d:
        yield d


def _span(doc: PdfDocument, prefix: str) -> TextSpan:
    return next(s for s in get_spans(doc, 0) if s.text.startswith(prefix))


def test_bundled_fonts_present_with_license() -> None:
    assert (FONT_DIR / "OFL.txt").is_file()
    for name in ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf"):
        font = pymupdf.Font(fontfile=str(FONT_DIR / name))
        assert all(font.has_glyph(ord(c)) for c in VIET)


def test_fallback_variants() -> None:
    regular, rname = fallback_font(bold=False)
    bold, bname = fallback_font(bold=True)
    assert rname != bname
    assert "Bold" in bold.name and "Bold" not in regular.name
    assert "bundled" in fonts.fallback_source(False)


def test_base_name_strips_subset_tag() -> None:
    assert fonts._base_name("BCDEEE+Calibri") == "Calibri"
    assert fonts._base_name("ArialMT") == "ArialMT"


def test_name_matching_allows_truncation() -> None:
    assert fonts._name_matches("ABCDEF+VeryLongFontName-BoldItalic", "VeryLongFontName-Bo")
    assert not fonts._name_matches("Arial", "Calibri")


def test_resource_name_is_stable_digest() -> None:
    regular, rname = fallback_font(False)
    bold, bname = fallback_font(True)
    assert rname == resource_name(regular) == resource_name(fallback_font(False)[0])
    assert rname.startswith("PE") and len(rname) == 12
    assert rname != bname


def test_simple_font_not_reused(doc: PdfDocument) -> None:
    # "Dolphin Technology Inc." is drawn with the WinAnsi (simple) ArialMT only.
    span = _span(doc, "Dolphin")
    simple = [f for f in doc.raw[0].get_fonts(full=True) if f[2] != "Type0"]
    assert simple
    font, _name, substituted = resolve_font(doc, doc.raw[0], span, "Dolphin")
    # Reuse, if any, must be a Type0 program (the simple font is not embedded).
    assert substituted or "Arial" in font.name


def test_resolve_uses_embedded_font(doc: PdfDocument) -> None:
    span = _span(doc, "Yêu cầu thực hiện")
    font, name, substituted = resolve_font(doc, doc.raw[0], span, "thiết kế")
    assert not substituted
    assert "Arial" in font.name
    assert name == resource_name(font)


def test_resolve_falls_back_for_missing_glyph(doc: PdfDocument) -> None:
    span = _span(doc, "Yêu cầu thực hiện")
    font, name, substituted = resolve_font(doc, doc.raw[0], span, "Q")
    assert substituted
    assert font.name == "Noto Sans Regular"
    assert font.has_glyph(ord("Q"))
    assert name == resource_name(font)


def test_resolve_bold_fallback(doc: PdfDocument) -> None:
    span = _span(doc, "Thiết kế VLSI")
    assert fonts.is_bold(span.flags)
    font, _name, substituted = resolve_font(doc, doc.raw[0], span, "Quiz")
    assert substituted
    assert font.name == "Noto Sans Bold"


def test_resolve_unknown_font_falls_back(doc: PdfDocument) -> None:
    span = replace(_span(doc, "Yêu cầu thực hiện"), font="NoSuchFont")
    _font, _name, substituted = resolve_font(doc, doc.raw[0], span, "abc")
    assert substituted


def test_resolve_non_embedded_font_falls_back(tmp_path: Path) -> None:
    raw = pymupdf.open()
    raw.new_page().insert_text((50, 100), "Helvetica text", fontname="helv")
    path = tmp_path / "helv.pdf"
    raw.save(path)
    raw.close()
    with PdfDocument.open(path) as d:
        span = get_spans(d, 0)[0]
        _font, _name, substituted = resolve_font(d, d.raw[0], span, "abc")
        assert substituted


def test_register_font_maps_space_to_u0020(doc: PdfDocument) -> None:
    # The embedded Arial subset maps U+0020 and U+00A0 to one glyph.
    span = _span(doc, "Yêu cầu thực hiện")
    font, name, _ = resolve_font(doc, doc.raw[0], span, "a b")
    assert font.has_glyph(0x20) == font.has_glyph(0xA0)
    out = pymupdf.open()
    page = out.new_page()
    name = register_font(page, name, font)
    page.insert_text((50, 100), "a b c", fontname=name, fontsize=11)
    assert page.get_text().strip() == "a b c"


def test_register_font_reuses_same_program() -> None:
    font, name = fallback_font(False)
    raw = pymupdf.open()
    page = raw.new_page()
    assert register_font(page, name, font) == name
    assert register_font(page, name, font) == name
    assert len(page.get_fonts()) == 1


def test_register_font_suffixes_on_conflict() -> None:
    font, name = fallback_font(False)
    bold, _ = fallback_font(True)
    raw = pymupdf.open()
    page = raw.new_page()
    page.insert_font(fontname=name, fontbuffer=bold.buffer)  # same name, other program
    used = register_font(page, name, font)
    assert used == f"{name}_1"
    page.insert_text((50, 100), "Quiz", fontname=used, fontsize=11)
    assert page.get_text().strip() == "Quiz"


def test_register_font_shares_object_after_reload() -> None:
    font, name = fallback_font(False)
    raw = pymupdf.open()
    p0 = raw.new_page()
    register_font(p0, name, font)
    p0.insert_text((50, 100), "zero", fontname=name)
    raw.new_page()
    again = pymupdf.open("pdf", raw.tobytes(garbage=3))
    p1 = again[1]
    used = register_font(p1, name, font)
    p1.insert_text((50, 100), "one Quiz", fontname=used)
    assert p1.get_text().strip() == "one Quiz"
    xrefs = {f[0] for pno in range(2) for f in again.get_page_fonts(pno)}
    assert len(xrefs) == 1


def test_fallback_when_bundled_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(fonts, "FONT_DIR", tmp_path)
    fonts._fallback.cache_clear()
    try:
        try:
            font, _ = fallback_font(False)
        except fonts.PdfError:
            pytest.skip("no built-in or system fallback font on this machine")
        assert font.has_glyph(ord("A"))
        assert "bundled" not in fonts.fallback_source(False)
    finally:
        fonts._fallback.cache_clear()
