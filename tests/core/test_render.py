"""Tests for pdf_editor.core.render."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pdf_editor.core.document import PdfDocument
from pdf_editor.core.errors import PdfError
from pdf_editor.core.render import page_geometry, render_page, render_thumbnail

from .helpers import copy_sample, make_doc


@pytest.fixture
def sample_doc(data_dir: Path, tmp_path: Path):
    doc = PdfDocument.open(copy_sample(data_dir, tmp_path))
    yield doc
    doc.close()


@pytest.mark.parametrize("zoom", [0.5, 1.0, 1.5, 2.0])
def test_render_dimensions_match_zoom(sample_doc: PdfDocument, zoom: float) -> None:
    rect = sample_doc.raw[0].rect
    img = render_page(sample_doc, 0, zoom)
    assert abs(img.width - rect.width * zoom) <= 1
    assert abs(img.height - rect.height * zoom) <= 1
    assert img.rgb
    assert img.stride >= img.width * 3
    assert len(img.samples) == img.stride * img.height


def test_render_rotated_page_swaps_dimensions() -> None:
    with make_doc(1) as doc:
        doc.raw[0].set_rotation(90)
        rect = doc.raw[0].rect
        img = render_page(doc, 0, 1.0)
        assert rect.width > rect.height
        assert abs(img.width - rect.width) <= 1
        assert abs(img.height - rect.height) <= 1


@pytest.mark.parametrize("max_size", [32, 100, 200])
def test_thumbnail_fits(sample_doc: PdfDocument, max_size: int) -> None:
    img = render_thumbnail(sample_doc, 0, max_size)
    assert max(img.width, img.height) <= max_size
    assert max(img.width, img.height) >= max_size - 1


@pytest.mark.parametrize("index", [-1, 10_000])
def test_render_bad_index(sample_doc: PdfDocument, index: int) -> None:
    with pytest.raises(PdfError):
        render_page(sample_doc, index)
    with pytest.raises(PdfError):
        render_thumbnail(sample_doc, index, 100)


def test_render_bad_zoom_and_size(sample_doc: PdfDocument) -> None:
    with pytest.raises(PdfError):
        render_page(sample_doc, 0, 0)
    with pytest.raises(PdfError):
        render_thumbnail(sample_doc, 0, 0)


def test_render_last_page(sample_doc: PdfDocument) -> None:
    img = render_page(sample_doc, sample_doc.page_count - 1, 0.25)
    assert img.width > 0 and img.height > 0


def _apply(m: tuple[float, ...], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("crop", [None, (30, 40, 400, 700)])
def test_page_geometry_maps_text_to_rendered_ink(rotation: int, crop) -> None:
    raw = pymupdf.open()
    page = raw.new_page(width=595, height=842)
    page.insert_text((100, 200), "XXXXXXXX", fontsize=20)
    if crop:
        page.set_cropbox(pymupdf.Rect(*crop))
    page.set_rotation(rotation)
    data = raw.tobytes()
    raw.close()
    with PdfDocument.from_bytes(data) as doc:
        geo = page_geometry(doc, 0)
        assert geo.rotation == rotation
        span = doc.raw[0].get_text("dict")["blocks"][0]["lines"][0]["spans"][0]
        x0, y0, x1, y1 = span["bbox"]
        corners = [_apply(geo.to_display, x, y) for x in (x0, x1) for y in (y0, y1)]
        dx0, dx1 = min(c[0] for c in corners), max(c[0] for c in corners)
        dy0, dy1 = min(c[1] for c in corners), max(c[1] for c in corners)
        img = render_page(doc, 0, 1.0)
        assert abs(img.width - geo.width) <= 1 and abs(img.height - geo.height) <= 1
        ink = [
            (x, y)
            for y in range(img.height)
            for x in range(img.width)
            if img.samples[y * img.stride + 3 * x] < 100
        ]
        assert ink
        assert dx0 - 1 <= min(p[0] for p in ink) and max(p[0] for p in ink) <= dx1 + 1
        assert dy0 - 1 <= min(p[1] for p in ink) and max(p[1] for p in ink) <= dy1 + 1


def test_page_geometry_bad_index(sample_doc: PdfDocument) -> None:
    with pytest.raises(PdfError):
        page_geometry(sample_doc, sample_doc.page_count)
