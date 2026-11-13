"""Tests for pdf_editor.core.history."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pymupdf
import pytest

from pdf_editor.core import pages
from pdf_editor.core.document import EncryptionPolicy, PdfDocument
from pdf_editor.core.errors import PasswordRequiredError, PdfError
from pdf_editor.core.history import Command, History

from .helpers import make_doc, make_pdf_bytes, markers, rotations

ORIGINAL = ["P0", "P1", "P2", "P3", "P4"]


@pytest.fixture
def doc():
    d = make_doc(5)
    yield d
    d.close()


def _state(doc: PdfDocument) -> tuple[list[str], list[int]]:
    return markers(doc), rotations(doc)


OPS: dict[str, Callable[[PdfDocument], object]] = {
    "insert_blank": lambda d: pages.insert_blank(d, 2),
    "insert_from_pdf": lambda d: pages.insert_from_pdf(d, 1, make_pdf_bytes(2, prefix="Q")),
    "delete_pages": lambda d: pages.delete_pages(d, [0, 3]),
    "rotate_pages": lambda d: pages.rotate_pages(d, [1, 2], 90),
    "move_page": lambda d: pages.move_page(d, 0, 4),
    "replace_page": lambda d: pages.replace_page(d, 2, make_pdf_bytes(1, prefix="Q"), 0),
}


@pytest.mark.parametrize("name", list(OPS))
def test_undo_redo_each_page_op(doc: PdfDocument, name: str) -> None:
    history = History(doc)
    before = _state(doc)
    history.execute(Command(name, OPS[name]))
    after = _state(doc)
    assert after != before
    assert doc.is_dirty

    history.undo()
    assert _state(doc) == before
    assert doc.page_count == len(before[0])
    assert not doc.is_dirty
    assert history.redo_label == name

    history.redo()
    assert _state(doc) == after
    assert doc.is_dirty
    assert history.undo_label == name


def test_same_object_after_undo(doc: PdfDocument) -> None:
    history = History(doc)
    ref = doc
    history.execute(Command("rotate", lambda d: pages.rotate_pages(d, [0], 90)))
    history.undo()
    assert ref is history.doc
    assert ref.raw[0].rotation == 0


def test_execute_returns_result(doc: PdfDocument) -> None:
    history = History(doc)
    n = history.execute(Command("insert", lambda d: pages.insert_from_pdf(d, 0, make_pdf_bytes(3))))
    assert n == 3


def test_labels_and_flags(doc: PdfDocument) -> None:
    history = History(doc)
    assert not history.can_undo and not history.can_redo
    assert history.undo_label is None and history.redo_label is None
    history.execute(Command("A", lambda d: pages.rotate_pages(d, [0], 90)))
    history.execute(Command("B", lambda d: pages.rotate_pages(d, [1], 90)))
    assert history.can_undo and history.undo_label == "B"
    history.undo()
    assert history.undo_label == "A" and history.redo_label == "B"
    assert history.can_redo
    history.undo()
    assert not history.can_undo and history.redo_label == "A"
    with pytest.raises(PdfError):
        history.undo()
    history.redo()
    history.redo()
    assert rotations(doc)[:2] == [90, 90]
    with pytest.raises(PdfError):
        history.redo()


def test_multi_step_undo_order(doc: PdfDocument) -> None:
    history = History(doc)
    history.execute(Command("del", lambda d: pages.delete_pages(d, [0])))
    history.execute(Command("move", lambda d: pages.move_page(d, 0, 3)))
    history.execute(Command("blank", lambda d: pages.insert_blank(d, 0)))
    assert markers(doc) == ["", "P2", "P3", "P4", "P1"]
    history.undo()
    assert markers(doc) == ["P2", "P3", "P4", "P1"]
    history.undo()
    assert markers(doc) == ["P1", "P2", "P3", "P4"]
    history.undo()
    assert markers(doc) == ORIGINAL


def test_new_execute_clears_redo(doc: PdfDocument) -> None:
    history = History(doc)
    history.execute(Command("A", lambda d: pages.rotate_pages(d, [0], 90)))
    history.undo()
    assert history.can_redo
    history.execute(Command("B", lambda d: pages.rotate_pages(d, [1], 90)))
    assert not history.can_redo
    assert history.redo_label is None
    assert rotations(doc) == [0, 90, 0, 0, 0]


def test_failed_command_restores_and_reraises(doc: PdfDocument) -> None:
    history = History(doc)

    def half_then_fail(d: PdfDocument) -> None:
        pages.delete_pages(d, [0])
        pages.rotate_pages(d, [0], 90)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        history.execute(Command("bad", half_then_fail))
    assert markers(doc) == ORIGINAL
    assert rotations(doc) == [0] * 5
    assert not history.can_undo
    assert not doc.is_dirty

    with pytest.raises(PdfError):
        history.execute(Command("bad2", lambda d: pages.delete_pages(d, range(5))))
    assert not history.can_undo


def test_eviction_by_count(doc: PdfDocument) -> None:
    history = History(doc, max_steps=3)
    for i in range(5):
        history.execute(Command(f"r{i}", lambda d, i=i: pages.rotate_pages(d, [i], 90)))
    assert history.undo_count == 3
    while history.can_undo:
        history.undo()
    # Only the last 3 rotations could be undone.
    assert rotations(doc) == [90, 90, 0, 0, 0]


def test_eviction_by_bytes(doc: PdfDocument) -> None:
    snapshot_size = len(doc.to_bytes())
    history = History(doc, max_bytes=int(snapshot_size * 2.5))
    for i in range(5):
        history.execute(Command(f"r{i}", lambda d, i=i: pages.rotate_pages(d, [i], 90)))
    assert 1 <= history.undo_count <= 2
    assert history.stored_bytes <= history.max_bytes


def test_tiny_byte_budget_keeps_nothing(doc: PdfDocument) -> None:
    history = History(doc, max_bytes=1)
    history.execute(Command("r", lambda d: pages.rotate_pages(d, [0], 90)))
    assert not history.can_undo
    assert rotations(doc)[0] == 90
    assert doc.is_dirty


def test_clean_tracking(doc: PdfDocument, tmp_path: Path) -> None:
    history = History(doc)
    assert history.is_clean
    history.execute(Command("A", lambda d: pages.rotate_pages(d, [0], 90)))
    assert not history.is_clean and doc.is_dirty

    doc.save_as(tmp_path / "saved.pdf")
    history.mark_saved()
    assert history.is_clean and not doc.is_dirty

    history.execute(Command("B", lambda d: pages.rotate_pages(d, [1], 90)))
    assert not history.is_clean and doc.is_dirty
    history.undo()
    assert history.is_clean and not doc.is_dirty  # back at the saved point
    history.undo()
    assert not history.is_clean and doc.is_dirty  # before the saved point
    history.redo()
    assert history.is_clean and not doc.is_dirty

    # Diverging from the saved point makes it unreachable.
    history.undo()
    history.execute(Command("C", lambda d: pages.rotate_pages(d, [2], 90)))
    assert not history.is_clean
    history.undo()
    assert not history.is_clean and doc.is_dirty


def test_initially_dirty_doc_is_not_clean() -> None:
    with make_doc(2) as d:
        d.mark_dirty()
        history = History(d)
        assert not history.is_clean
        history.mark_saved()
        assert history.is_clean and not d.is_dirty


def test_clear(doc: PdfDocument) -> None:
    history = History(doc)
    history.execute(Command("A", lambda d: pages.rotate_pages(d, [0], 90)))
    history.execute(Command("B", lambda d: pages.rotate_pages(d, [0], 90)))
    history.undo()
    history.clear()
    assert not history.can_undo and not history.can_redo
    assert history.stored_bytes == 0
    assert rotations(doc)[0] == 90
    assert doc.is_dirty


def test_changed_listeners(doc: PdfDocument) -> None:
    history = History(doc)
    calls: list[str] = []
    history.changed.append(lambda: calls.append("x"))
    history.execute(Command("A", lambda d: pages.rotate_pages(d, [0], 90)))
    history.undo()
    history.redo()
    history.mark_saved()
    history.clear()
    assert len(calls) == 5
    with pytest.raises(RuntimeError):
        history.execute(Command("bad", lambda d: (_ for _ in ()).throw(RuntimeError())))
    assert len(calls) == 5  # no notification for a failed command


def test_listener_exception_is_logged_not_raised(
    doc: PdfDocument, caplog: pytest.LogCaptureFixture
) -> None:
    history = History(doc)
    calls: list[str] = []

    def bad() -> None:
        raise RuntimeError("listener broke")

    history.changed.extend([bad, lambda: calls.append("ok")])
    with caplog.at_level(logging.ERROR, logger="pdf_editor.core.history"):
        history.execute(Command("A", lambda d: pages.rotate_pages(d, [0], 90)))
    assert calls == ["ok"]
    assert history.can_undo
    assert "listener broke" in caplog.text


def test_noop_command_not_recorded(doc: PdfDocument) -> None:
    history = History(doc)
    calls: list[str] = []
    history.changed.append(lambda: calls.append("x"))
    history.execute(Command("noop", lambda d: pages.move_page(d, 2, 2)))
    assert not history.can_undo
    assert not doc.is_dirty and history.is_clean
    assert calls == []

    history.execute(Command("A", lambda d: pages.rotate_pages(d, [0], 90)))
    history.undo()
    assert history.can_redo
    doc.mark_dirty()  # e.g. an unrecorded external change
    history.execute(Command("noop", lambda d: pages.move_page(d, 1, 1)))
    assert doc.is_dirty  # dirty state preserved
    assert history.can_redo  # redo not cleared by a no-op
    assert history.undo_count == 0


def test_rollback_failure_keeps_original_exception(
    doc: PdfDocument, monkeypatch: pytest.MonkeyPatch
) -> None:
    history = History(doc)
    calls: list[str] = []
    history.changed.append(lambda: calls.append("x"))

    def broken_load(data: bytes) -> None:
        raise PdfError("reload failed")

    def failing(d: PdfDocument) -> None:
        pages.delete_pages(d, [0])
        monkeypatch.setattr(d, "load_bytes", broken_load)
        raise ValueError("original")

    with pytest.raises(ValueError, match="original") as info:
        history.execute(Command("bad", failing))
    assert any("reload failed" in note for note in info.value.__notes__)
    monkeypatch.undo()

    # UI is told, the doc is dirty, and the partial change can be undone.
    assert calls == ["x"]
    assert doc.is_dirty and not history.is_clean
    assert history.undo_label == "bad (failed)"
    history.undo()
    assert markers(doc) == ORIGINAL
    assert history.is_clean


def test_oversized_snapshot_reported_not_undoable(
    doc: PdfDocument, caplog: pytest.LogCaptureFixture
) -> None:
    history = History(doc, max_bytes=len(doc.to_bytes()) // 2)
    assert history.last_execute_undoable
    with caplog.at_level(logging.WARNING, logger="pdf_editor.core.history"):
        history.execute(Command("big", lambda d: pages.rotate_pages(d, [0], 90)))
    assert not history.last_execute_undoable
    assert not history.can_undo
    assert "cannot be undone" in caplog.text

    roomy = History(doc)
    roomy.execute(Command("ok", lambda d: pages.rotate_pages(d, [0], 90)))
    assert roomy.last_execute_undoable


def test_encryption_change_keeps_history_unclean(doc: PdfDocument) -> None:
    history = History(doc)
    history.execute(Command("A", lambda d: pages.rotate_pages(d, [0], 90)))
    history.mark_saved()
    doc.set_encryption(EncryptionPolicy(user_pw="u", owner_pw="o"))
    assert not history.is_clean and doc.is_dirty
    history.execute(Command("B", lambda d: pages.rotate_pages(d, [1], 90)))
    history.undo()  # back at the saved id, but the policy (set outside history) differs
    assert not history.is_clean and doc.is_dirty
    doc.set_encryption(None)  # back to the saved policy
    assert history.is_clean
    history.mark_saved()
    assert history.is_clean and not doc.is_dirty


def _encrypted_doc(tmp_path: Path) -> PdfDocument:
    src = tmp_path / "secret.pdf"
    with pymupdf.open(stream=make_pdf_bytes(3), filetype="pdf") as raw:
        raw.save(src, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="own")
    return PdfDocument.open(src, password="pw")


def test_remove_password_is_undoable(tmp_path: Path) -> None:
    with _encrypted_doc(tmp_path) as doc:
        original = doc.encryption
        assert original is not None
        history = History(doc)
        history.execute(Command("Remove password", lambda d: d.set_encryption(None)))
        assert doc.encryption is None
        assert history.can_undo and not history.is_clean and doc.is_dirty

        history.undo()
        assert doc.encryption == original
        assert history.is_clean and not doc.is_dirty

        history.redo()
        assert doc.encryption is None
        assert not history.is_clean and doc.is_dirty

        history.undo()
        out = tmp_path / "after_undo.pdf"
        history.save_as(out)
    with pytest.raises(PasswordRequiredError):
        PdfDocument.open(out)
    with PdfDocument.open(out, password="pw") as reopened:
        assert reopened.page_count == 3


def test_same_policy_is_noop(tmp_path: Path) -> None:
    with _encrypted_doc(tmp_path) as doc:
        history = History(doc)
        history.execute(Command("same", lambda d: d.set_encryption(d.encryption)))
        assert not history.can_undo and not doc.is_dirty


def test_failed_command_restores_encryption(tmp_path: Path) -> None:
    with _encrypted_doc(tmp_path) as doc:
        original = doc.encryption
        history = History(doc)

        def fail(d: PdfDocument) -> None:
            d.set_encryption(None)
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            history.execute(Command("bad", fail))
        assert doc.encryption == original
        assert not doc.is_dirty and not history.can_undo


def test_undo_page_edit_keeps_later_policy_consistent(tmp_path: Path) -> None:
    with _encrypted_doc(tmp_path) as doc:
        history = History(doc)
        history.execute(Command("rot", lambda d: pages.rotate_pages(d, [0], 90)))
        history.execute(Command("Remove password", lambda d: d.set_encryption(None)))
        history.undo()  # undo only the password removal
        assert doc.encryption is not None and rotations(doc)[0] == 90
        history.undo()
        assert doc.encryption is not None and rotations(doc)[0] == 0
        assert history.is_clean
        history.redo()
        history.redo()
        assert doc.encryption is None and rotations(doc)[0] == 90


def test_save_and_save_as_wrappers(doc: PdfDocument, tmp_path: Path) -> None:
    history = History(doc)
    history.execute(Command("A", lambda d: pages.rotate_pages(d, [0], 90)))
    out = tmp_path / "h.pdf"
    history.save_as(out)
    assert history.is_clean and not doc.is_dirty
    history.execute(Command("B", lambda d: pages.delete_pages(d, [4])))
    assert not history.is_clean
    history.save()
    assert history.is_clean and not doc.is_dirty
    with PdfDocument.open(out) as reopened:
        assert markers(reopened) == ORIGINAL[:4]
        assert rotations(reopened)[0] == 90


def test_saved_entry_evicted_stays_not_clean(doc: PdfDocument) -> None:
    history = History(doc, max_steps=1)
    history.execute(Command("A", lambda d: pages.rotate_pages(d, [0], 90)))
    history.mark_saved()
    history.execute(Command("B", lambda d: pages.rotate_pages(d, [1], 90)))
    history.execute(Command("C", lambda d: pages.rotate_pages(d, [2], 90)))
    assert history.undo_count == 1
    history.undo()  # back to state after B; saved state (after A) evicted
    assert not history.can_undo
    assert not history.is_clean and doc.is_dirty
    history.redo()
    assert not history.is_clean and doc.is_dirty


def test_byte_budget_holds_across_undo_redo(doc: PdfDocument) -> None:
    budget = int(len(doc.to_bytes()) * 3)
    history = History(doc, max_bytes=budget)
    for _ in range(4):  # each step makes the document (and later snapshots) bigger
        history.execute(Command("grow", lambda d: pages.insert_from_pdf(d, 0, make_pdf_bytes(5))))
        assert history.stored_bytes <= budget
    while history.can_undo:
        history.undo()
        assert history.stored_bytes <= budget
    while history.can_redo:
        history.redo()
        assert history.stored_bytes <= budget
    assert history.undo_count + history.redo_count < 4  # something was evicted


def test_encrypted_document_survives_undo_redo(tmp_path: Path) -> None:
    src = tmp_path / "secret.pdf"
    with pymupdf.open(stream=make_pdf_bytes(3), filetype="pdf") as raw:
        raw.save(src, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="own")
    out = tmp_path / "out.pdf"
    with PdfDocument.open(src, password="pw") as doc:
        history = History(doc)
        history.execute(Command("rot", lambda d: pages.rotate_pages(d, [1], 90)))
        history.undo()
        history.redo()
        assert doc.encryption is not None
        history.save_as(out)
    with pytest.raises(PasswordRequiredError):
        PdfDocument.open(out)
    with PdfDocument.open(out, password="pw") as reopened:
        assert rotations(reopened) == [0, 90, 0]


def test_invalid_limits(doc: PdfDocument) -> None:
    with pytest.raises(ValueError):
        History(doc, max_steps=0)
    with pytest.raises(ValueError):
        History(doc, max_bytes=0)
