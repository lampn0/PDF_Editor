"""Page rendering to raster images."""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf

from pdf_editor.core.document import PdfDocument
from pdf_editor.core.errors import PdfError


@dataclass(frozen=True)
class RenderedPage:
    """A rendered page bitmap. ``rgb`` True means 3 bytes/pixel, no alpha."""

    width: int
    height: int
    stride: int
    samples: bytes
    rgb: bool = True


@dataclass(frozen=True)
class PageGeometry:
    """Display geometry of a page, in PDF points.

    ``width``/``height`` are the displayed (rotated, CropBox) size, i.e. the
    size of a bitmap from :func:`render_page` at zoom 1. ``to_display`` is the
    affine matrix ``(a, b, c, d, e, f)`` that maps *unrotated page coordinates*
    (the CropBox-relative space used by text extraction and editing in
    ``core.text``) to display coordinates: ``x' = a*x + c*y + e``,
    ``y' = b*x + d*y + f``.
    """

    width: float
    height: float
    rotation: int
    to_display: tuple[float, float, float, float, float, float]


def _page(doc: PdfDocument, index: int) -> pymupdf.Page:
    count = doc.page_count
    if not 0 <= index < count:
        raise PdfError(f"Page index {index} out of range (0..{count - 1})")
    return doc.raw[index]


def _render(page: pymupdf.Page, scale: float) -> RenderedPage:
    if not scale > 0:
        raise PdfError(f"Invalid zoom {scale}")
    try:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    except Exception as exc:
        raise PdfError(f"Render failed: {exc}") from exc
    return RenderedPage(pix.width, pix.height, pix.stride, bytes(pix.samples), True)


def page_geometry(doc: PdfDocument, index: int) -> PageGeometry:
    """Displayed size, rotation and unrotated-to-display matrix of page ``index``."""
    page = _page(doc, index)
    rect = page.rect
    m = page.rotation_matrix
    return PageGeometry(
        float(rect.width),
        float(rect.height),
        int(page.rotation),
        (float(m.a), float(m.b), float(m.c), float(m.d), float(m.e), float(m.f)),
    )


def render_page(doc: PdfDocument, index: int, zoom: float = 1.0) -> RenderedPage:
    """Render page ``index`` at ``zoom`` (1.0 = 72 dpi)."""
    return _render(_page(doc, index), zoom)


def render_thumbnail(doc: PdfDocument, index: int, max_size: int) -> RenderedPage:
    """Render page ``index`` so its longer side is at most ``max_size`` pixels."""
    if max_size <= 0:
        raise PdfError(f"Invalid max_size {max_size}")
    page = _page(doc, index)
    rect = page.rect
    longest = max(rect.width, rect.height)
    if longest <= 0:
        raise PdfError("Page has empty size")
    return _render(page, max_size / longest)
