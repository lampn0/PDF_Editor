"""Tests for pdf_editor.core.text (span extraction and in-place editing)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pymupdf
import pytest

from pdf_editor.core import text as text_mod
from pdf_editor.core.document import PdfDocument
from pdf_editor.core.errors import PdfError
from pdf_editor.core.fonts import fallback_font
from pdf_editor.core.text import (
    MIN_SHRINK,
    WIDTH_TOLERANCE,
    TextSpan,
    find_span_at,
    get_spans,
    measure_replacement,
    replace_span,
)

SAMPLE = "Thiet_ke_VLSI_Exercise.pdf"
VIET = "Thiết kế vi mạch"


@pytest.fixture
def sample_path(data_dir: Path, tmp_path: Path) -> Path:
    dst = tmp_path / SAMPLE
    shutil.copyfile(data_dir / SAMPLE, dst)
    return dst


@pytest.fixture
def doc(sample_path: Path):
    with PdfDocument.open(sample_path) as d:
        yield d


def _span_starting(doc: PdfDocument, prefix: str, page: int = 0) -> TextSpan:
    return next(s for s in get_spans(doc, page) if s.text.startswith(prefix))


def _close(a, b, tol: float) -> bool:
    return all(abs(p - q) <= tol for p, q in zip(a, b, strict=True))


def _unchanged(before: list[TextSpan], after: list[TextSpan], tol: float = 0.5) -> list[str]:
    """Texts of spans in ``before`` with no matching (text, bbox) in ``after``."""
    return [
        s.text
        for s in before
        if not any(o.text == s.text and _close(o.bbox, s.bbox, tol) for o in after)
    ]


_TRAILER_ID = re.compile(rb"/ID\s*\[\s*<[0-9A-Fa-f]*>\s*<[0-9A-Fa-f]*>\s*\]")


def _content_bytes(doc: PdfDocument) -> bytes:
    """Serialised document without the trailer /ID (regenerated on every write)."""
    return _TRAILER_ID.sub(b"", doc.to_bytes())


def _count(doc: PdfDocument, needle: str, page: int = 0) -> int:
    return doc.raw[page].get_text().count(needle)


def _save_new(path: Path, build) -> Path:
    raw = pymupdf.open()
    page = raw.new_page(width=300, height=400)
    build(page)
    raw.save(path)
    raw.close()
    return path


def _make_pdf(path: Path, rotation: int = 0, with_image: bool = False) -> Path:
    def build(page: pymupdf.Page) -> None:
        if with_image:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
            pix.set_rect(pix.irect, (200, 50, 50))
            page.insert_image(pymupdf.Rect(40, 80, 200, 120), pixmap=pix)
        page.insert_text((50, 100), "Hello world", fontsize=12)
        page.insert_text((50, 116), "Second line", fontsize=12)
        page.set_rotation(rotation)

    return _save_new(path, build)


def _page_font_xrefs(raw: pymupdf.Document, pno: int) -> set[int]:
    return {f[0] for f in raw.get_page_fonts(pno)}


# -- extraction -------------------------------------------------------------


def test_get_spans_non_empty(doc: PdfDocument) -> None:
    spans = get_spans(doc, 0)
    assert spans
    assert all(s.text.strip() for s in spans)
    s = spans[0]
    assert len(s.bbox) == 4 and len(s.origin) == 2 and len(s.dir) == 2
    assert s.page_index == 0 and s.size > 0 and s.alpha == 255


def test_get_spans_bad_page(doc: PdfDocument) -> None:
    with pytest.raises(PdfError):
        get_spans(doc, doc.page_count)


def test_find_span_at(doc: PdfDocument) -> None:
    span = _span_starting(doc, "Yêu cầu thực hiện")
    cx = (span.bbox[0] + span.bbox[2]) / 2
    cy = (span.bbox[1] + span.bbox[3]) / 2
    assert find_span_at(doc, 0, cx, cy) == span
    assert find_span_at(doc, 0, -10, -10) is None


# -- replace ----------------------------------------------------------------


def test_replace_span_basic(doc: PdfDocument) -> None:
    before = get_spans(doc, 0)
    span = _span_starting(doc, "Mạch reset")
    assert _count(doc, "Mạch reset") == 1
    result = replace_span(doc, span, "New text here")
    assert doc.is_dirty
    assert not result.overflow
    assert result.fontsize == pytest.approx(span.size)

    assert _count(doc, "New text here") == 1
    assert _count(doc, "Mạch reset") == 0

    new = _span_starting(doc, "New text here")
    assert _close(new.origin, span.origin, 1.0)
    assert new.size == pytest.approx(span.size, abs=0.01)
    assert new.color == span.color

    others = [s for s in before if s != span]
    assert _unchanged(others, get_spans(doc, 0)) == []


def test_neighbour_lines_untouched(doc: PdfDocument) -> None:
    # Tightly spaced wrapped paragraph: edit one line, check the lines around it.
    span = _span_starting(doc, "1 đầu ra kết quả")
    before = get_spans(doc, 0)
    neighbours = [
        s for s in before if s != span and abs(s.origin[1] - span.origin[1]) < 3 * span.size
    ]
    assert len(neighbours) >= 2
    replace_span(doc, span, "X")
    assert _unchanged(neighbours, get_spans(doc, 0)) == []


def test_vietnamese_round_trip(doc: PdfDocument) -> None:
    span = _span_starting(doc, "Yêu cầu thực hiện")
    before = _count(doc, VIET)
    replace_span(doc, span, VIET)
    assert _count(doc, VIET) == before + 1
    assert sum(s.text == VIET for s in get_spans(doc, 0)) == 1


def test_substituted_when_glyph_missing(doc: PdfDocument) -> None:
    # The embedded ArialMT subset has no 'Q' glyph (no Q on the page).
    span = _span_starting(doc, "Yêu cầu thực hiện")
    result = replace_span(doc, span, "QQQ")
    assert result.substituted
    assert "Noto Sans" in result.font_used
    assert _count(doc, "QQQ") == 1


def test_embedded_font_reused_when_covered(doc: PdfDocument) -> None:
    span = _span_starting(doc, "Yêu cầu thực hiện")
    result = replace_span(doc, span, "Yêu cầu")
    assert not result.substituted
    assert "Arial" in result.font_used


def test_overflow_flag(doc: PdfDocument) -> None:
    span = _span_starting(doc, "Yêu cầu thực hiện")
    result = replace_span(doc, span, span.text.strip() * 3)
    assert result.overflow
    assert result.fontsize == pytest.approx(span.size)


def test_shrink_to_fit(doc: PdfDocument) -> None:
    span = _span_starting(doc, "Yêu cầu thực hiện")
    long_text = span.text.strip() + " dự án"
    result = replace_span(doc, span, long_text, shrink_to_fit=True)
    assert not result.overflow
    assert MIN_SHRINK * span.size <= result.fontsize < span.size
    new = _span_starting(doc, long_text[:20])
    assert new.bbox[0] >= span.bbox[0] - WIDTH_TOLERANCE
    assert new.bbox[2] <= span.bbox[2] + WIDTH_TOLERANCE


def test_shrink_to_fit_clamped(doc: PdfDocument) -> None:
    span = _span_starting(doc, "Yêu cầu thực hiện")
    result = replace_span(doc, span, span.text.strip() * 4, shrink_to_fit=True)
    assert result.overflow
    assert result.fontsize == pytest.approx(MIN_SHRINK * span.size)


def test_empty_text_deletes_span(doc: PdfDocument) -> None:
    span = _span_starting(doc, "Mạch reset")
    replace_span(doc, span, "")
    assert _count(doc, "Mạch reset") == 0
    assert find_span_at(doc, 0, span.bbox[0] + 5, (span.bbox[1] + span.bbox[3]) / 2) is None


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\tb", "a\x00b", "a\x1bb"])
def test_control_characters_rejected(doc: PdfDocument, bad: str) -> None:
    span = _span_starting(doc, "Mạch reset")
    with pytest.raises(PdfError, match="control characters"):
        replace_span(doc, span, bad)
    assert _count(doc, "Mạch reset") == 1
    assert not doc.is_dirty


def test_repeated_edits_keep_font_count(doc: PdfDocument) -> None:
    span = _span_starting(doc, "Mạch reset")
    fonts_after: list[int] = []
    for i in range(5):
        replace_span(doc, span, f"Lần {i} QZ")
        span = _span_starting(doc, f"Lần {i}")
        fonts_after.append(len(_page_font_xrefs(doc.raw, 0)))
    assert len(set(fonts_after)) == 1
    assert _count(doc, "Lần 4 QZ") == 1


# -- persistence --------------------------------------------------------------


def test_edit_persists_after_save(doc: PdfDocument, tmp_path: Path) -> None:
    span = _span_starting(doc, "Mạch reset")
    replace_span(doc, span, VIET)
    out = tmp_path / "out.pdf"
    doc.save_as(out)
    with PdfDocument.open(out) as again:
        assert _count(again, VIET) == 1
        assert _count(again, "Mạch reset") == 0


def test_second_edit_after_save_reopen(doc: PdfDocument, tmp_path: Path) -> None:
    """Fonts subsetted on save must not break a later edit with new characters."""
    replace_span(doc, _span_starting(doc, "Mạch reset"), "Qa first")
    out = tmp_path / "out.pdf"
    doc.save_as(out)
    second = "QXYZ Đường đi mới"
    with PdfDocument.open(out) as again:
        replace_span(again, _span_starting(again, "Yêu cầu thực hiện"), second)
        out2 = tmp_path / "out2.pdf"
        again.save_as(out2)
    with PdfDocument.open(out2) as final:
        page = final.raw[0]
        text = page.get_text()
        assert "Qa first" in text
        assert second in text
        for tspan in page.get_texttrace():
            chars = "".join(chr(c[0]) for c in tspan["chars"])
            if "QXYZ" in chars or "Qa first" in chars:
                assert all(c[1] > 0 for c in tspan["chars"] if chr(c[0]) != " ")


def test_fallback_not_reembedded_after_reload(doc: PdfDocument) -> None:
    replace_span(doc, _span_starting(doc, "Mạch reset"), "QZ one")
    doc.load_bytes(doc.to_bytes())
    p1 = get_spans(doc, 1)[0]
    result = replace_span(doc, p1, "QZ two")
    assert result.substituted
    _, name = fallback_font(False)
    noto = {
        f[0]
        for pno in range(doc.page_count)
        for f in doc.raw.get_page_fonts(pno, full=True)
        if f[4].startswith(name)
    }
    assert len(noto) == 1
    assert "QZ two" in doc.raw[1].get_text()


# -- failure handling ---------------------------------------------------------


def test_register_failure_leaves_text_intact(
    doc: PdfDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("cannot embed")

    monkeypatch.setattr(text_mod, "register_font", boom)
    span = _span_starting(doc, "Mạch reset")
    with pytest.raises(PdfError):
        replace_span(doc, span, "New")
    assert _count(doc, "Mạch reset") == 1


def test_embedded_register_failure_falls_back(
    doc: PdfDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = text_mod.register_font
    _, fallback_name = fallback_font(False)

    def only_fallback(page, fontname, font):
        if not fontname.startswith(fallback_name):
            raise RuntimeError("embedded font unusable")
        return real(page, fontname, font)

    monkeypatch.setattr(text_mod, "register_font", only_fallback)
    span = _span_starting(doc, "Yêu cầu thực hiện")
    result = replace_span(doc, span, "Yêu cầu")  # covered by the embedded Arial
    assert result.substituted
    assert "Noto Sans" in result.font_used
    assert _count(doc, "Yêu cầu") >= 1


def test_insert_failure_marks_dirty(doc: PdfDocument, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("write failed")

    monkeypatch.setattr(pymupdf.Page, "insert_text", boom)
    span = _span_starting(doc, "Mạch reset")
    with pytest.raises(PdfError, match="roll back"):
        replace_span(doc, span, "New")
    assert doc.is_dirty


def test_stale_span_rejected(doc: PdfDocument) -> None:
    span = _span_starting(doc, "Mạch reset")
    replace_span(doc, span, "Other")
    with pytest.raises(PdfError, match="undo/redo"):
        replace_span(doc, span, "Again")


# -- appearance ---------------------------------------------------------------


def test_sample_images_kept(doc: PdfDocument) -> None:
    n = len(doc.raw[0].get_images())
    replace_span(doc, _span_starting(doc, "Thiết kế VLSI"), "Thiết kế ASIC")
    assert len(doc.raw[0].get_images()) == n
    assert _count(doc, "Thiết kế ASIC") == 1


def test_image_under_text_not_removed(tmp_path: Path) -> None:
    path = _make_pdf(tmp_path / "img.pdf", with_image=True)
    with PdfDocument.open(path) as d:
        assert len(d.raw[0].get_images()) == 1
        replace_span(d, _span_starting(d, "Hello"), "Bye")
        assert len(d.raw[0].get_images()) == 1
        assert len(d.raw[0].get_image_info()) == 1
        assert _count(d, "Bye") == 1


def test_adjacent_coloured_spans(tmp_path: Path) -> None:
    def build(page: pymupdf.Page) -> None:
        page.insert_text((50, 100), "Red", fontsize=12, color=(1, 0, 0))
        page.insert_text((72.008, 100), "Blue", fontsize=12, color=(0, 0, 1))

    with PdfDocument.open(_save_new(tmp_path / "c.pdf", build)) as d:
        red = _span_starting(d, "Red")
        blue = _span_starting(d, "Blue")
        assert red.color == 0xFF0000 and blue.color == 0x0000FF
        replace_span(d, red, "Ruby")
        spans = get_spans(d, 0)
        assert _unchanged([blue], spans) == []
        ruby = _span_starting(d, "Ruby")
        assert ruby.color == 0xFF0000


def test_near_black_snaps_to_black(tmp_path: Path) -> None:
    def build(page: pymupdf.Page) -> None:
        page.insert_text((50, 100), "Dark", fontsize=12, color=(1 / 255, 1 / 255, 1 / 255))

    with PdfDocument.open(_save_new(tmp_path / "k.pdf", build)) as d:
        replace_span(d, _span_starting(d, "Dark"), "Darker")
        assert _span_starting(d, "Darker").color == 0


def test_link_preserved(tmp_path: Path) -> None:
    def build(page: pymupdf.Page) -> None:
        page.insert_text((50, 100), "Linked text", fontsize=12)
        page.insert_link(
            {
                "kind": pymupdf.LINK_URI,
                "from": pymupdf.Rect(48, 88, 120, 102),
                "uri": "https://example.com/",
            }
        )

    with PdfDocument.open(_save_new(tmp_path / "l.pdf", build)) as d:
        assert len(d.raw[0].get_links()) == 1
        replace_span(d, _span_starting(d, "Linked"), "Other text")
        links = d.raw[0].get_links()
        assert len(links) == 1
        assert links[0]["uri"] == "https://example.com/"


def test_invisible_text_stays_invisible(tmp_path: Path) -> None:
    def build(page: pymupdf.Page) -> None:
        page.insert_text((50, 100), "Hidden OCR", fontsize=12, render_mode=3)

    with PdfDocument.open(_save_new(tmp_path / "i.pdf", build)) as d:
        span = _span_starting(d, "Hidden")
        assert span.alpha == 0
        replace_span(d, span, "Still hidden")
        new = _span_starting(d, "Still hidden")
        assert new.alpha == 0
        assert all(t["type"] == 3 for t in d.raw[0].get_texttrace())


def test_opacity_preserved(tmp_path: Path) -> None:
    def build(page: pymupdf.Page) -> None:
        page.insert_text((50, 100), "Faded", fontsize=12, fill_opacity=0.5)

    with PdfDocument.open(_save_new(tmp_path / "o.pdf", build)) as d:
        span = _span_starting(d, "Faded")
        replace_span(d, span, "Faded too")
        assert _span_starting(d, "Faded too").alpha == pytest.approx(span.alpha, abs=2)


# -- rotation / direction -----------------------------------------------------


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_page(tmp_path: Path, rotation: int) -> None:
    path = _make_pdf(tmp_path / "rot.pdf", rotation=rotation)
    with PdfDocument.open(path) as d:
        span = _span_starting(d, "Hello")
        second = _span_starting(d, "Second")
        replace_span(d, span, "Howdy")
        new = _span_starting(d, "Howdy")
        assert _close(new.origin, span.origin, 1.0)
        assert _count(d, "Hello") == 0
        assert _unchanged([second], get_spans(d, 0)) == []


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_upright_text_on_rotated_page(tmp_path: Path, rotate: int) -> None:
    """Text drawn rotated in page space (reads upright when /Rotate is applied)."""

    def build(page: pymupdf.Page) -> None:
        page.insert_text((150, 200), "Upright text", fontsize=12, rotate=rotate)
        # Neighbour line offset across the baseline (perpendicular to the text).
        other = (150, 230) if rotate == 180 else (180, 200)
        page.insert_text(other, "Neighbour", fontsize=12, rotate=rotate)
        page.set_rotation(rotate)

    with PdfDocument.open(_save_new(tmp_path / "up.pdf", build)) as d:
        span = _span_starting(d, "Upright")
        neighbour = _span_starting(d, "Neighbour")
        assert span.dir != (1.0, 0.0)
        result = replace_span(d, span, "Changed")
        assert not result.overflow
        new = _span_starting(d, "Changed")
        assert new.dir == pytest.approx(span.dir)
        assert _close(new.origin, span.origin, 1.0)
        assert _count(d, "Upright") == 0
        assert _unchanged([neighbour], get_spans(d, 0)) == []


def test_angled_text_rejected(tmp_path: Path) -> None:
    def build(page: pymupdf.Page) -> None:
        pt = pymupdf.Point(100, 300)
        page.insert_text(pt, "Angled", fontsize=12, morph=(pt, pymupdf.Matrix(30)))

    with PdfDocument.open(_save_new(tmp_path / "a.pdf", build)) as d:
        span = _span_starting(d, "Angled")
        with pytest.raises(PdfError, match="90 degrees"):
            replace_span(d, span, "x")
        assert not d.is_dirty


# -- measure_replacement / exception wrapping ------------------------------------

_MEASURE_CASES = [
    ("Mạch reset", "Thiết kế", False),  # embedded font, fits
    ("Yêu cầu thực hiện", "QQQ", False),  # substituted
    ("Yêu cầu thực hiện", "Yêu cầu thực hiện dự án " * 3, False),  # overflow
    ("Yêu cầu thực hiện", "Yêu cầu thực hiện dự án nhé", True),  # shrink
    ("Yêu cầu thực hiện", "Yêu cầu thực hiện dự án " * 4, True),  # shrink clamped
    ("Mạch reset", "", False),  # delete
]


@pytest.mark.parametrize(("prefix", "new", "shrink"), _MEASURE_CASES)
def test_measure_matches_replace(doc: PdfDocument, prefix: str, new: str, shrink: bool) -> None:
    span = _span_starting(doc, prefix)
    before = _content_bytes(doc)
    measured = measure_replacement(doc, span, new, shrink_to_fit=shrink)
    assert _content_bytes(doc) == before
    assert not doc.is_dirty
    assert measured == replace_span(doc, span, new, shrink_to_fit=shrink)


def test_measure_validates_like_replace(doc: PdfDocument) -> None:
    span = _span_starting(doc, "Mạch reset")
    with pytest.raises(PdfError, match="control characters"):
        measure_replacement(doc, span, "a\tb")
    replace_span(doc, span, "Other")
    with pytest.raises(PdfError, match="undo/redo"):
        measure_replacement(doc, span, "Again")


@pytest.mark.parametrize("func", [replace_span, measure_replacement])
def test_unexpected_errors_become_pdferror(
    doc: PdfDocument, monkeypatch: pytest.MonkeyPatch, func
) -> None:
    span = _span_starting(doc, "Mạch reset")
    before = _content_bytes(doc)

    def boom(*_a, **_k):
        raise RuntimeError("trace broken")

    monkeypatch.setattr(pymupdf.Page, "get_texttrace", boom)
    monkeypatch.setattr(pymupdf.Page, "get_text", boom)
    with pytest.raises(PdfError) as info:
        func(doc, span, "New")
    assert isinstance(info.value.__cause__, RuntimeError)
    monkeypatch.undo()
    assert _content_bytes(doc) == before
    assert not doc.is_dirty
    assert _count(doc, "Mạch reset") == 1


def test_texttrace_error_in_replace_is_pdferror(
    doc: PdfDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    span = _span_starting(doc, "Mạch reset")
    before = _content_bytes(doc)

    def boom(*_a, **_k):
        raise RuntimeError("trace broken")

    monkeypatch.setattr(pymupdf.Page, "get_texttrace", boom)
    with pytest.raises(PdfError, match="text trace"):
        replace_span(doc, span, "New")
    monkeypatch.undo()
    assert _content_bytes(doc) == before
    assert not doc.is_dirty
