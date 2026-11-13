"""Tests for pdf_editor.core.pages."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from pdf_editor.core import pages
from pdf_editor.core.document import PdfDocument
from pdf_editor.core.errors import PasswordRequiredError, PdfError

from .helpers import make_doc, make_pdf_bytes, markers, rotations


@pytest.fixture
def doc():
    d = make_doc(5)
    yield d
    d.close()


# -- insert_blank -------------------------------------------------------------


def test_insert_blank_middle(doc: PdfDocument) -> None:
    pages.insert_blank(doc, 2)
    assert markers(doc) == ["P0", "P1", "", "P2", "P3", "P4"]
    assert doc.is_dirty


def test_insert_blank_start_and_end(doc: PdfDocument) -> None:
    pages.insert_blank(doc, 0)
    pages.insert_blank(doc, doc.page_count)
    assert markers(doc) == ["", "P0", "P1", "P2", "P3", "P4", ""]


def test_insert_blank_uses_neighbour_size() -> None:
    with PdfDocument.from_bytes(make_pdf_bytes(2, width=300, height=400)) as d:
        pages.insert_blank(d, 1)
        pages.insert_blank(d, 0)
        for page in d.raw:
            assert (round(page.rect.width), round(page.rect.height)) == (300, 400)


def test_insert_blank_explicit_size(doc: PdfDocument) -> None:
    pages.insert_blank(doc, 1, width=200, height=100)
    rect = doc.raw[1].rect
    assert (round(rect.width), round(rect.height)) == (200, 100)


@pytest.mark.parametrize(
    ("index", "width", "height"),
    [
        (-1, None, None),
        (6, None, None),
        (0, 100, None),
        (0, -5, 10),
        (0, 0, 0),
        (0, True, True),
        (0, "100", 100),
        (True, None, None),
        (1.0, None, None),
    ],
)
def test_insert_blank_errors(doc: PdfDocument, index: int, width, height) -> None:
    with pytest.raises(PdfError):
        pages.insert_blank(doc, index, width, height)
    assert doc.page_count == 5
    assert not doc.is_dirty


# -- insert_from_pdf ----------------------------------------------------------


def test_insert_from_pdf_bytes_all(doc: PdfDocument) -> None:
    n = pages.insert_from_pdf(doc, 1, make_pdf_bytes(3, prefix="Q"))
    assert n == 3
    assert markers(doc) == ["P0", "Q0", "Q1", "Q2", "P1", "P2", "P3", "P4"]
    assert doc.is_dirty


def test_insert_from_pdf_range_at_end(doc: PdfDocument, tmp_path: Path) -> None:
    src = tmp_path / "src.pdf"
    src.write_bytes(make_pdf_bytes(4, prefix="Q"))
    n = pages.insert_from_pdf(doc, 5, src, from_page=1, to_page=2)
    assert n == 2
    assert markers(doc) == ["P0", "P1", "P2", "P3", "P4", "Q1", "Q2"]


def test_insert_from_pdf_at_start_single(doc: PdfDocument) -> None:
    n = pages.insert_from_pdf(doc, 0, make_pdf_bytes(4, prefix="Q"), from_page=3, to_page=3)
    assert n == 1
    assert markers(doc)[:2] == ["Q3", "P0"]


def test_insert_from_pdf_from_pdfdocument(doc: PdfDocument) -> None:
    with make_doc(2, prefix="R") as other:
        assert pages.insert_from_pdf(doc, 2, other) == 2
        assert markers(doc) == ["P0", "P1", "R0", "R1", "P2", "P3", "P4"]
        assert other.page_count == 2  # source untouched and still open


@pytest.mark.parametrize(("from_page", "to_page"), [(-1, 0), (3, 3), (2, 1), (0, 5)])
def test_insert_from_pdf_bad_range(doc: PdfDocument, from_page: int, to_page: int) -> None:
    with pytest.raises(PdfError):
        pages.insert_from_pdf(doc, 0, make_pdf_bytes(3), from_page, to_page)
    assert doc.page_count == 5
    assert not doc.is_dirty


def test_insert_from_pdf_bad_index_and_source(doc: PdfDocument, tmp_path: Path) -> None:
    with pytest.raises(PdfError):
        pages.insert_from_pdf(doc, 7, make_pdf_bytes(1))
    with pytest.raises(PdfError):
        pages.insert_from_pdf(doc, 0, tmp_path / "missing.pdf")
    with pytest.raises(PdfError):
        pages.insert_from_pdf(doc, 0, doc)
    assert doc.page_count == 5


def test_insert_from_encrypted_pdf(doc: PdfDocument) -> None:
    with pymupdf.open(stream=make_pdf_bytes(2, prefix="E"), filetype="pdf") as src:
        data = src.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="own")
    with pytest.raises(PasswordRequiredError):
        pages.insert_from_pdf(doc, 0, data)
    assert pages.insert_from_pdf(doc, 0, data, password="pw") == 2
    assert markers(doc)[:3] == ["E0", "E1", "P0"]


# -- delete_pages -------------------------------------------------------------


def test_delete_pages(doc: PdfDocument) -> None:
    pages.delete_pages(doc, [3, 0, 3])
    assert markers(doc) == ["P1", "P2", "P4"]
    assert doc.is_dirty


def test_delete_refuses_all(doc: PdfDocument) -> None:
    with pytest.raises(PdfError):
        pages.delete_pages(doc, range(5))
    assert doc.page_count == 5
    assert not doc.is_dirty


@pytest.mark.parametrize("indices", [[5], [-1], [0, 9], []])
def test_delete_bad_indices(doc: PdfDocument, indices: list[int]) -> None:
    with pytest.raises(PdfError):
        pages.delete_pages(doc, indices)
    assert doc.page_count == 5


# -- rotate_pages -------------------------------------------------------------


def test_rotate_accumulates(doc: PdfDocument) -> None:
    pages.rotate_pages(doc, [0, 2], 90)
    pages.rotate_pages(doc, [2], 180)
    pages.rotate_pages(doc, [0], -90)
    pages.rotate_pages(doc, [4], 450)
    assert rotations(doc) == [0, 0, 270, 0, 90]
    assert doc.is_dirty


@pytest.mark.parametrize("degrees", [45, 1, 91.0])
def test_rotate_bad_degrees(doc: PdfDocument, degrees) -> None:
    with pytest.raises(PdfError):
        pages.rotate_pages(doc, [0], degrees)
    assert not doc.is_dirty


def test_rotate_bad_index(doc: PdfDocument) -> None:
    with pytest.raises(PdfError):
        pages.rotate_pages(doc, [0, 5], 90)
    assert rotations(doc) == [0] * 5


def test_rotation_persists_after_save(doc: PdfDocument, tmp_path: Path) -> None:
    pages.rotate_pages(doc, [1], 90)
    pages.rotate_pages(doc, [3], 270)
    out = tmp_path / "rot.pdf"
    doc.save_as(out)
    with PdfDocument.open(out) as reopened:
        assert rotations(reopened) == [0, 90, 0, 270, 0]


# -- move_page ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "dst", "expected"),
    [
        (0, 4, ["P1", "P2", "P3", "P4", "P0"]),
        (4, 0, ["P4", "P0", "P1", "P2", "P3"]),
        (1, 3, ["P0", "P2", "P3", "P1", "P4"]),
        (3, 1, ["P0", "P3", "P1", "P2", "P4"]),
        (2, 3, ["P0", "P1", "P3", "P2", "P4"]),
    ],
)
def test_move_page(doc: PdfDocument, src: int, dst: int, expected: list[str]) -> None:
    pages.move_page(doc, src, dst)
    assert markers(doc) == expected
    assert doc.is_dirty


def test_move_page_noop(doc: PdfDocument) -> None:
    pages.move_page(doc, 2, 2)
    assert markers(doc) == ["P0", "P1", "P2", "P3", "P4"]
    assert not doc.is_dirty


@pytest.mark.parametrize(("src", "dst"), [(5, 0), (0, 5), (-1, 0)])
def test_move_page_bad(doc: PdfDocument, src: int, dst: int) -> None:
    with pytest.raises(PdfError):
        pages.move_page(doc, src, dst)


# -- replace_page -------------------------------------------------------------


@pytest.mark.parametrize("index", [0, 2, 4])
def test_replace_page(doc: PdfDocument, index: int) -> None:
    pages.replace_page(doc, index, make_pdf_bytes(3, prefix="Q"), 1)
    expected = ["P0", "P1", "P2", "P3", "P4"]
    expected[index] = "Q1"
    assert markers(doc) == expected
    assert doc.is_dirty


def test_replace_page_errors(doc: PdfDocument) -> None:
    src = make_pdf_bytes(2, prefix="Q")
    with pytest.raises(PdfError):
        pages.replace_page(doc, 5, src, 0)
    with pytest.raises(PdfError):
        pages.replace_page(doc, 0, src, 2)
    with pytest.raises(PdfError):
        pages.replace_page(doc, 0, src, -1)
    assert markers(doc) == ["P0", "P1", "P2", "P3", "P4"]
    assert not doc.is_dirty


# -- type checks and partial failure ------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda d: pages.delete_pages(d, [True]),
        lambda d: pages.delete_pages(d, [1.0]),
        lambda d: pages.rotate_pages(d, [0.0], 90),
        lambda d: pages.rotate_pages(d, [0], True),
        lambda d: pages.move_page(d, 1.0, 2),
        lambda d: pages.move_page(d, 0, True),
        lambda d: pages.insert_from_pdf(d, 0, make_pdf_bytes(2), from_page=0.0),
        lambda d: pages.insert_from_pdf(d, 0, make_pdf_bytes(2), to_page=1.0),
        lambda d: pages.replace_page(d, 0, make_pdf_bytes(2), 1.0),
        lambda d: pages.replace_page(d, False, make_pdf_bytes(2), 0),
    ],
)
def test_bool_and_float_indices_rejected(doc: PdfDocument, call) -> None:
    with pytest.raises(PdfError):
        call(doc)
    assert markers(doc) == ["P0", "P1", "P2", "P3", "P4"]
    assert not doc.is_dirty


def test_replace_page_partial_failure_marks_dirty(
    doc: PdfDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("delete failed")

    monkeypatch.setattr(doc.raw, "delete_page", boom)
    with pytest.raises(PdfError, match="Replace page failed"):
        pages.replace_page(doc, 1, make_pdf_bytes(1, prefix="Q"), 0)
    monkeypatch.undo()
    assert doc.is_dirty
    assert doc.page_count == 6  # inserted but not deleted: not atomic
