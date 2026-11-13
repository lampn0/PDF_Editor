"""Page operations: add, delete, reorder and rotate pages.

Every function takes a :class:`PdfDocument`, validates its arguments before
touching the document, raises :class:`PdfError` on bad input, and marks the
document dirty on success.

The functions are **not atomic**: if PyMuPDF fails after the document was
changed, the document is marked dirty and left partially modified before
PdfError is raised. Run them through ``History.execute``, which restores the
pre-command snapshot on any exception.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from numbers import Real

import pymupdf

from pdf_editor.core.document import PdfDocument
from pdf_editor.core.errors import PdfError

PdfSource = str | os.PathLike[str] | bytes | bytearray | PdfDocument

_A4 = pymupdf.paper_size("a4")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_index(doc: PdfDocument, index: int, *, allow_end: bool = False) -> int:
    count = doc.page_count
    upper = count if allow_end else count - 1
    if not _is_int(index) or not 0 <= index <= upper:
        raise PdfError(f"Page index {index!r} out of range (0..{upper})")
    return index


def _check_indices(doc: PdfDocument, indices: Iterable[int]) -> list[int]:
    result = sorted({_check_index(doc, i) for i in indices})
    if not result:
        raise PdfError("No page indices given")
    return result


@contextmanager
def _source(src: PdfSource, password: str | None) -> Iterator[pymupdf.Document]:
    """Yield a PyMuPDF document for ``src``; close it afterwards if we opened it."""
    if isinstance(src, PdfDocument):
        yield src.raw
        return
    if isinstance(src, bytes | bytearray):
        opened = PdfDocument.from_bytes(bytes(src), password)
    else:
        opened = PdfDocument.open(src, password)
    try:
        yield opened.raw
    finally:
        opened.close()


def _wrap(action: str, exc: Exception) -> PdfError:
    return PdfError(f"{action} failed: {exc}")


def insert_blank(
    doc: PdfDocument, index: int, width: float | None = None, height: float | None = None
) -> None:
    """Insert an empty page so that it ends up at ``index`` (0..page_count).

    Without an explicit size the page copies the visible size of the previous
    page (or the following one when inserting at 0), falling back to A4.
    """
    _check_index(doc, index, allow_end=True)
    if (width is None) != (height is None):
        raise PdfError("Give both width and height, or neither")
    for value in (width, height):
        if value is not None and (isinstance(value, bool) or not isinstance(value, Real)):
            raise PdfError(f"Invalid page size value {value!r}")
    if width is None or height is None:
        count = doc.page_count
        if count:
            rect = doc.raw[index - 1 if index > 0 else 0].rect
            width, height = rect.width, rect.height
        else:
            width, height = _A4
    if not (width > 0 and height > 0):
        raise PdfError(f"Invalid page size {width}x{height}")
    try:
        doc.raw.new_page(pno=index, width=width, height=height)
    except Exception as exc:
        doc.mark_dirty()  # partially applied; History.execute rolls back
        raise _wrap("Insert page", exc) from exc
    doc.mark_dirty()


def insert_from_pdf(
    doc: PdfDocument,
    index: int,
    src_path_or_bytes: PdfSource,
    from_page: int = 0,
    to_page: int = -1,
    password: str | None = None,
) -> int:
    """Insert pages ``from_page..to_page`` (inclusive, -1 = last) of another PDF at ``index``.

    Returns the number of pages inserted. Open errors propagate as PdfOpenError /
    PasswordRequiredError (both PdfError subclasses).
    """
    _check_index(doc, index, allow_end=True)
    with _source(src_path_or_bytes, password) as src:
        if src is doc.raw:
            raise PdfError("Cannot insert a document into itself")
        n = src.page_count
        last = n - 1 if to_page == -1 else to_page
        if not (_is_int(from_page) and _is_int(last)) or not (
            0 <= from_page < n and from_page <= last < n
        ):
            raise PdfError(f"Invalid source page range {from_page}..{to_page} (document has {n})")
        start = -1 if index == doc.page_count else index
        try:
            doc.raw.insert_pdf(src, from_page=from_page, to_page=last, start_at=start)
        except Exception as exc:
            doc.mark_dirty()  # partially applied; History.execute rolls back
            raise _wrap("Insert pages", exc) from exc
    doc.mark_dirty()
    return last - from_page + 1


def delete_pages(doc: PdfDocument, indices: Iterable[int]) -> None:
    """Delete the given pages. Refuses to delete every page of the document."""
    targets = _check_indices(doc, indices)
    if len(targets) >= doc.page_count:
        raise PdfError("Cannot delete all pages of a document")
    try:
        doc.raw.delete_pages(targets)
    except Exception as exc:
        doc.mark_dirty()  # partially applied; History.execute rolls back
        raise _wrap("Delete pages", exc) from exc
    doc.mark_dirty()


def rotate_pages(doc: PdfDocument, indices: Iterable[int], degrees: int) -> None:
    """Add ``degrees`` (a multiple of 90, may be negative) to each page's rotation."""
    if not _is_int(degrees) or degrees % 90:
        raise PdfError(f"Rotation must be a multiple of 90, got {degrees!r}")
    targets = _check_indices(doc, indices)
    try:
        for i in targets:
            page = doc.raw[i]
            page.set_rotation((page.rotation + degrees) % 360)
    except Exception as exc:
        doc.mark_dirty()  # partially applied; History.execute rolls back
        raise _wrap("Rotate pages", exc) from exc
    doc.mark_dirty()


def move_page(doc: PdfDocument, src: int, dst: int) -> None:
    """Move page ``src`` so that it ends up at final index ``dst``."""
    _check_index(doc, src)
    _check_index(doc, dst)
    if src == dst:
        return
    # PyMuPDF's move_page(pno, to) inserts *before* page ``to`` (-1 = append).
    if dst > src:
        to = -1 if dst + 1 == doc.page_count else dst + 1
    else:
        to = dst
    try:
        doc.raw.move_page(src, to)
    except Exception as exc:
        doc.mark_dirty()  # partially applied; History.execute rolls back
        raise _wrap("Move page", exc) from exc
    doc.mark_dirty()


def replace_page(
    doc: PdfDocument,
    index: int,
    src: PdfSource,
    src_page: int,
    password: str | None = None,
) -> None:
    """Replace page ``index`` with page ``src_page`` of another PDF."""
    _check_index(doc, index)
    with _source(src, password) as source:
        if source is doc.raw:
            raise PdfError("Cannot replace a page from the same document object")
        if not _is_int(src_page) or not 0 <= src_page < source.page_count:
            raise PdfError(f"Source page {src_page} out of range (0..{source.page_count - 1})")
        try:
            doc.raw.insert_pdf(source, from_page=src_page, to_page=src_page, start_at=index)
            doc.raw.delete_page(index + 1)
        except Exception as exc:
            doc.mark_dirty()  # partially applied; History.execute rolls back
            raise _wrap("Replace page", exc) from exc
    doc.mark_dirty()
