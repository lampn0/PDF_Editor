"""Text extraction and in-place text editing.

All coordinates are PDF points in unrotated page space (the space used by
``page.get_text`` and ``page.insert_text`` in PyMuPDF 1.28), so editing works
the same on rotated pages. Text running in any of the four axis directions
(e.g. text drawn upright on a /Rotate 90 page) can be edited.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import pymupdf

from pdf_editor.core.document import PdfDocument
from pdf_editor.core.errors import PdfError
from pdf_editor.core.fonts import (
    fallback_font,
    is_bold,
    register_font,
    resolve_font,
    span_render_mode,
)

# Extraction flags: "dict" defaults without image blocks, and with ligatures
# expanded to their component characters.
_TEXT_FLAGS = (
    pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES & ~pymupdf.TEXT_PRESERVE_LIGATURES
)
# Extra width allowed before a replacement counts as overflowing (points).
WIDTH_TOLERANCE = 0.5
# shrink_to_fit never goes below this fraction of the original size.
MIN_SHRINK = 0.5
# Max distance (points) between a span and its fresh extraction to count as the same.
_MATCH_TOL = 1.0
# Text direction (unrotated page space, y down) -> insert_text ``rotate``.
_DIR_ROTATE = {(1, 0): 0, (0, -1): 90, (-1, 0): 180, (0, 1): 270}
_RENDER_INVISIBLE = 3
_RENDER_STROKE = 1


@dataclass(frozen=True)
class TextSpan:
    page_index: int
    block: int
    line: int
    span: int
    bbox: tuple[float, float, float, float]
    origin: tuple[float, float]
    text: str
    font: str
    size: float
    color: int
    flags: int
    dir: tuple[float, float]
    alpha: int = 255  # 0 = invisible text (e.g. OCR layer)

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def is_vertical(self) -> bool:
        return abs(self.dir[0]) < abs(self.dir[1])

    @property
    def length(self) -> float:
        """Extent of the span along its text direction."""
        return self.bbox[3] - self.bbox[1] if self.is_vertical else self.width


@dataclass(frozen=True)
class EditResult:
    font_used: str
    substituted: bool
    overflow: bool
    fontsize: float


def _page(doc: PdfDocument, page_index: int) -> pymupdf.Page:
    if not 0 <= page_index < doc.page_count:
        raise PdfError(f"Page index {page_index} out of range (0..{doc.page_count - 1})")
    return doc.raw[page_index]


def get_spans(doc: PdfDocument, page_index: int) -> list[TextSpan]:
    """All non-blank text spans on a page, in reading order of the content stream."""
    page = _page(doc, page_index)
    spans: list[TextSpan] = []
    for b, block in enumerate(page.get_text("dict", flags=_TEXT_FLAGS)["blocks"]):
        if block.get("type", 0) != 0:
            continue
        for ln, line in enumerate(block["lines"]):
            for s, sp in enumerate(line["spans"]):
                if not sp["text"].strip():
                    continue
                spans.append(
                    TextSpan(
                        page_index=page_index,
                        block=b,
                        line=ln,
                        span=s,
                        bbox=tuple(float(v) for v in sp["bbox"]),
                        origin=tuple(float(v) for v in sp["origin"]),
                        text=sp["text"],
                        font=sp["font"],
                        size=float(sp["size"]),
                        color=int(sp["color"]),
                        flags=int(sp["flags"]),
                        dir=tuple(float(v) for v in line["dir"]),
                        alpha=int(sp.get("alpha", 255)),
                    )
                )
    return spans


def find_span_at(doc: PdfDocument, page_index: int, x: float, y: float) -> TextSpan | None:
    """The span whose bbox contains (x, y); the smallest one if several overlap."""
    hits = [
        s
        for s in get_spans(doc, page_index)
        if s.bbox[0] <= x <= s.bbox[2] and s.bbox[1] <= y <= s.bbox[3]
    ]
    if not hits:
        return None
    return min(hits, key=lambda s: (s.bbox[2] - s.bbox[0]) * (s.bbox[3] - s.bbox[1]))


def _same_span(a: TextSpan, b: TextSpan) -> bool:
    return a.text == b.text and all(
        abs(p - q) <= _MATCH_TOL for p, q in zip(a.bbox, b.bbox, strict=True)
    )


def _rotation_for(span: TextSpan) -> int:
    for (dx, dy), rotate in _DIR_ROTATE.items():
        if abs(span.dir[0] - dx) <= 1e-3 and abs(span.dir[1] - dy) <= 1e-3:
            return rotate
    raise PdfError("Editing text that is not at a multiple of 90 degrees is not supported")


def _check_text(text: str) -> None:
    bad = sorted({f"U+{ord(ch):04X}" for ch in text if unicodedata.category(ch) == "Cc"})
    if bad:
        raise PdfError(
            "Replacement text must be a single line without control characters "
            f"(found {', '.join(bad)})"
        )


def _redact_rect(span: TextSpan) -> pymupdf.Rect:
    """Span bbox shrunk so only this span's characters are hit.

    Across the baseline only the middle half of the line is kept, so glyphs of
    neighbouring lines (whose boxes may touch or overlap) are not removed.
    Along the baseline a small inset avoids neighbouring spans on the same line.
    """
    x0, y0, x1, y1 = span.bbox
    w, h = x1 - x0, y1 - y0
    if span.is_vertical:
        dy = min(0.05 * span.size, 0.25 * h)
        return pymupdf.Rect(x0 + 0.25 * w, y0 + dy, x1 - 0.25 * w, y1 - dy)
    dx = min(0.05 * span.size, 0.25 * w)
    return pymupdf.Rect(x0 + dx, y0 + 0.25 * h, x1 - dx, y1 - 0.25 * h)


def _color(color: int) -> tuple[float, float, float]:
    """sRGB int -> RGB floats; near-black is snapped to pure black.

    Extraction only exposes colours converted to RGB, so gray/CMYK source
    colours are re-written as their RGB equivalent.
    """
    rgb = ((color >> 16) & 255, (color >> 8) & 255, color & 255)
    if max(rgb) <= 2:
        return 0.0, 0.0, 0.0
    return rgb[0] / 255, rgb[1] / 255, rgb[2] / 255


def _link_key(link: dict) -> tuple:
    r = pymupdf.Rect(link["from"])
    return (
        link.get("kind"),
        tuple(round(v, 1) for v in r),
        link.get("uri"),
        link.get("page"),
        link.get("file"),
        link.get("nameddest"),
    )


def _restore_links(page: pymupdf.Page, before: list[dict]) -> None:
    """Re-insert links that redaction removed (MuPDF drops overlapping links)."""
    present = {_link_key(lk) for lk in page.get_links()}
    for link in before:
        if _link_key(link) not in present:
            page.insert_link({k: v for k, v in link.items() if k not in ("xref", "id")})


def _guarded(what: str, fn, *args):
    """Call ``fn``; turn any non-PdfError exception into PdfError."""
    try:
        return fn(*args)
    except PdfError:
        raise
    except Exception as exc:
        raise PdfError(f"{what} failed: {exc}") from exc


def _precheck_now(doc: PdfDocument, span: TextSpan, new_text: str) -> tuple[pymupdf.Page, int]:
    page = _page(doc, span.page_index)
    _check_text(new_text)
    rotate = _rotation_for(span)
    if not any(_same_span(span, s) for s in get_spans(doc, span.page_index)):
        raise PdfError(
            "Span no longer exists on the page (document changed); "
            "re-read spans, e.g. after undo/redo"
        )
    if any(a.type[0] == pymupdf.PDF_ANNOT_REDACT for a in page.annots()):
        raise PdfError(
            "Page contains pending redaction annotations; apply or remove them before editing"
        )
    return page, rotate


def _precheck(doc: PdfDocument, span: TextSpan, new_text: str) -> tuple[pymupdf.Page, int]:
    """Read-only validation shared by ``replace_span`` and ``measure_replacement``.

    Returns ``(page, insert_text rotate)``; raises PdfError only.
    """
    return _guarded("Checking the span", _precheck_now, doc, span, new_text)


def _fit(font: pymupdf.Font, span: TextSpan, text: str, shrink_to_fit: bool) -> tuple[float, bool]:
    """Font size to write ``text`` with, and whether it still overflows the span."""
    fontsize = span.size
    width = font.text_length(text, fontsize=fontsize)
    if width > span.length + WIDTH_TOLERANCE and shrink_to_fit:
        fontsize = max(fontsize * span.length / width, MIN_SHRINK * span.size)
        width = font.text_length(text, fontsize=fontsize)
    return fontsize, width > span.length + WIDTH_TOLERANCE


def _prepare_font(
    doc: PdfDocument, page: pymupdf.Page, span: TextSpan, text: str
) -> tuple[pymupdf.Font, str, bool]:
    """Resolve and register the font; fall back to Noto if the embedded one fails."""
    font, fontname, substituted = _guarded("Choosing a font", resolve_font, doc, page, span, text)
    try:
        return font, register_font(page, fontname, font), substituted
    except Exception as exc:
        if substituted:
            raise PdfError(f"Cannot embed fallback font: {exc}") from exc
    font, fontname = fallback_font(is_bold(span.flags))
    try:
        return font, register_font(page, fontname, font), True
    except Exception as exc:
        raise PdfError(f"Cannot embed fallback font: {exc}") from exc


def measure_replacement(
    doc: PdfDocument, span: TextSpan, new_text: str, shrink_to_fit: bool = False
) -> EditResult:
    """Predict ``replace_span``'s result without changing the document.

    Runs the same validation, font choice and fitting as ``replace_span`` but
    registers no font, redacts nothing and does not mark the document dirty.
    (``replace_span`` can still differ if embedding the chosen embedded font
    fails at write time and it has to fall back to Noto.) Raises PdfError only.
    """
    page, _rotate = _precheck(doc, span, new_text)
    if not new_text:
        return EditResult(font_used="", substituted=False, overflow=False, fontsize=span.size)
    font, _name, substituted = _guarded("Choosing a font", resolve_font, doc, page, span, new_text)
    fontsize, overflow = _guarded("Measuring text", _fit, font, span, new_text, shrink_to_fit)
    return EditResult(
        font_used=font.name, substituted=substituted, overflow=overflow, fontsize=fontsize
    )


def replace_span(
    doc: PdfDocument, span: TextSpan, new_text: str, shrink_to_fit: bool = False
) -> EditResult:
    """Replace ``span``'s text with ``new_text`` at the same origin, size and colour.

    The old glyphs are removed with a redaction that leaves images, vector
    graphics and links untouched; the new text is written at the span origin
    (the origin of its first character, including leading whitespace), in the
    same direction, size, colour, opacity and visibility (invisible OCR text
    stays invisible). ``overflow`` is True when the new text is longer than
    the original span (plus ``WIDTH_TOLERANCE``); with ``shrink_to_fit`` the
    font size is reduced to fit, but not below ``MIN_SHRINK`` x the original
    size (``overflow`` stays True if it still does not fit). An empty
    ``new_text`` deletes the span. ``measure_replacement`` predicts the result.

    Raises PdfError (only) for invalid input or a stale span without touching
    the document. If writing fails part-way, the document is marked dirty and
    PdfError is raised so the caller can roll back.
    """
    page, rotate = _precheck(doc, span, new_text)
    mode = _guarded("Reading the text trace", span_render_mode, page, span)
    fontsize = span.size
    overflow = False
    font = None
    fontname = ""
    substituted = False
    if new_text:
        font, fontname, substituted = _prepare_font(doc, page, span, new_text)
        fontsize, overflow = _guarded("Measuring text", _fit, font, span, new_text, shrink_to_fit)

    invisible = span.alpha == 0 or mode == _RENDER_INVISIBLE
    if invisible:
        render_mode = _RENDER_INVISIBLE
    elif mode == _RENDER_STROKE:
        render_mode = _RENDER_STROKE
    else:
        render_mode = 0
    opacity = span.alpha / 255 if 0 < span.alpha < 255 else 1

    try:
        links = page.get_links()
        page.add_redact_annot(_redact_rect(span), fill=False, cross_out=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
        _restore_links(page, links)
        if font is not None:
            # apply_redactions drops unused font resources: re-bind the font
            # object registered above (no new embedding).
            fontname = register_font(page, fontname, font)
            page.insert_text(
                span.origin,
                new_text,
                fontsize=fontsize,
                fontname=fontname,
                color=_color(span.color),
                rotate=rotate,
                render_mode=render_mode,
                fill_opacity=opacity,
                stroke_opacity=opacity,
            )
    except Exception as exc:
        doc.mark_dirty()
        raise PdfError(f"Text edit failed part-way; roll back the document: {exc}") from exc
    doc.mark_dirty()
    return EditResult(
        font_used=font.name if font is not None else "",
        substituted=substituted,
        overflow=overflow,
        fontsize=fontsize,
    )
