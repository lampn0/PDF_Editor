"""T8 integration tests: editor flushing, encryption, save errors, key routing."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from pdf_editor.core.document import PdfDocument
from pdf_editor.core.errors import PasswordRequiredError, PdfSaveError
from pdf_editor.core.text import get_spans
from pdf_editor.ui.main_window import ENCRYPTED_SAVE_MESSAGE, MainWindow

WAIT_MS = 20_000


def _make_pdf(path: Path, pages: int = 1, **save_kwargs) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((100, 200), "Hello World", fontsize=20)
        page.insert_text((100, 400), f"Page {i + 1}", fontsize=14)
    doc.save(path, **save_kwargs)
    doc.close()
    return path


def texts(doc: PdfDocument, page: int = 0) -> list[str]:
    return [s.text for s in get_spans(doc, page)]


def open_editor(win: MainWindow, text: str = "Hello World"):
    span = next(s for s in get_spans(win.document, 0) if s.text == text)
    if not win.edit_text_action.isChecked():
        win.edit_text_action.trigger()
    win.text_overlay.open_editor(span)
    assert win.text_overlay.is_editing
    return span


@pytest.fixture
def win(make_window, tmp_path: Path) -> MainWindow:
    window = make_window()
    assert window.open_path(_make_pdf(tmp_path / "doc.pdf", pages=3))
    return window


# -- B1: pending editor text is applied before save / close ---------------------------


def test_save_applies_open_editor_text(win: MainWindow, tmp_path: Path) -> None:
    open_editor(win)
    win.text_overlay.editor.setText("Saved text")
    assert win.save()
    assert not win.text_overlay.is_editing
    assert not win.isWindowModified()
    with PdfDocument.open(tmp_path / "doc.pdf") as reopened:
        assert "Saved text" in texts(reopened)


def test_save_as_applies_open_editor_text(win: MainWindow, tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "other.pdf"
    monkeypatch.setattr(win, "ask_save_path", lambda start: str(target))
    open_editor(win)
    win.text_overlay.editor.setText("Copy text")
    assert win.save_as()
    with PdfDocument.open(target) as reopened:
        assert "Copy text" in texts(reopened)


def test_close_with_open_editor_prompts_on_clean_doc(win: MainWindow, monkeypatch) -> None:
    prompts: list[str] = []

    def warning(*args, **kwargs):
        prompts.append(args[2])
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(warning))
    assert not win.document.is_dirty
    open_editor(win)
    win.text_overlay.editor.setText("Unsaved")
    assert win.close() is False
    assert len(prompts) == 1
    assert win.document.is_dirty
    assert "Unsaved" in texts(win.document)


def test_unchanged_editor_does_not_dirty_on_close(win: MainWindow, monkeypatch) -> None:
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: 1 / 0))
    open_editor(win)
    assert win.close() is True


def test_deferred_commit_dropped_after_document_switch(
    win: MainWindow, tmp_path: Path, qtbot
) -> None:
    open_editor(win)
    win.text_overlay.editor.setText("Too late")
    win.text_overlay.commit()  # deferred to the next event-loop turn
    other = _make_pdf(tmp_path / "second.pdf")
    win._set_document(PdfDocument.open(other))  # switch before the commit runs
    qtbot.wait(50)
    assert "Too late" not in texts(win.document)
    assert not win.document.is_dirty


# -- S2: text edit only refreshes the edited thumbnail --------------------------------


def test_text_edit_updates_only_its_thumbnail(win: MainWindow, qtbot, monkeypatch) -> None:
    thumbs = win.thumbnails
    qtbot.waitUntil(lambda: thumbs.ready_count == 3, timeout=WAIT_MS)
    thumbs.select_pages([0, 2], 0)
    rebuilt: list[int] = []
    monkeypatch.setattr(thumbs, "rebuild", lambda *a, **k: rebuilt.append(1))
    other_key = thumbs.item(1).icon().cacheKey()
    span = next(s for s in get_spans(win.document, 0) if s.text == "Hello World")
    assert win.edit_span_text(span, "Hi") is not None
    qtbot.waitUntil(lambda: thumbs.ready_count == 3, timeout=WAIT_MS)
    assert rebuilt == []
    assert thumbs.item(1).icon().cacheKey() == other_key
    assert thumbs.selected_pages() == [0, 2]


# -- S3: unexpected exceptions are reported, document rolled back ---------------------


def test_unexpected_exception_in_command_is_reported(win: MainWindow, monkeypatch) -> None:
    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))

    def boom(doc):
        doc.mark_dirty()
        raise RuntimeError("kaboom")

    assert win._run("Boom", boom) is False
    assert shown and "kaboom" in shown[0]
    assert not win.document.is_dirty
    assert win.document.page_count == 3


# -- S4 / N7: save dialog and save failures --------------------------------------------


def test_save_dialog_adds_pdf_suffix(win: MainWindow, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_exec(dialog: QFileDialog) -> int:
        seen["suffix"] = dialog.defaultSuffix()
        seen["mode"] = dialog.acceptMode()
        return QFileDialog.DialogCode.Rejected

    monkeypatch.setattr(QFileDialog, "exec", fake_exec)
    assert win.ask_save_path("") == ""
    assert seen == {"suffix": "pdf", "mode": QFileDialog.AcceptMode.AcceptSave}


def test_save_failure_offers_save_as(win: MainWindow, tmp_path: Path, monkeypatch) -> None:
    def failing_save():
        raise PdfSaveError("disk full")

    monkeypatch.setattr(win.history, "save", failing_save)
    asked: list[str] = []

    def critical(*args, **kwargs):
        asked.append(args[2])
        return QMessageBox.StandardButton.Save

    monkeypatch.setattr(QMessageBox, "critical", staticmethod(critical))
    target = tmp_path / "rescue.pdf"
    monkeypatch.setattr(win, "ask_save_path", lambda start: str(target))
    win.rotate_right()
    assert win.save() is True
    assert "disk full" in asked[0]
    assert target.exists()
    assert not win.isWindowModified()


# -- N1: encrypted documents ------------------------------------------------------------


def test_encrypted_doc_stays_encrypted_after_edit_and_save(
    make_window, tmp_path: Path, monkeypatch
) -> None:
    path = _make_pdf(
        tmp_path / "locked.pdf",
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="secret",
        owner_pw="owner",
    )
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("secret", True)))
    win = make_window()
    assert win.open_path(path)
    assert win.remove_password_action.isEnabled()
    span = next(s for s in get_spans(win.document, 0) if s.text == "Hello World")
    assert win.edit_span_text(span, "Edited") is not None
    assert win.save()
    assert win.statusBar().currentMessage() == ENCRYPTED_SAVE_MESSAGE
    with pytest.raises(PasswordRequiredError):
        PdfDocument.open(path)
    with PdfDocument.open(path, "secret") as reopened:
        assert "Edited" in texts(reopened)

    assert win.remove_password_protection()
    assert win.isWindowModified()
    assert win.save()
    with PdfDocument.open(path) as reopened:  # no password needed any more
        assert "Edited" in texts(reopened)
    assert not win.remove_password_action.isEnabled()


def test_remove_password_disabled_for_plain_doc(win: MainWindow) -> None:
    assert not win.remove_password_action.isEnabled()
    assert win.remove_password_protection() is False


# -- N11: keys typed into the editor do not trigger window shortcuts --------------------


def test_editor_keys_do_not_trigger_shortcuts(win: MainWindow, qtbot) -> None:
    open_editor(win)
    editor = win.text_overlay.editor
    editor.setText("abc")
    editor.setCursorPosition(1)
    view = win.page_view
    # Keys arrive at the focused graphics view and are routed to the proxy widget.
    qtbot.keyClick(view, Qt.Key.Key_T, Qt.KeyboardModifier.NoModifier)
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert editor.text() == "atc"
    assert win.edit_text_action.isChecked()
    assert win.document.page_count == 3
    assert win.text_overlay.is_editing


# -- N4: steps too large to undo are surfaced -------------------------------------------


def test_not_undoable_step_warns(win: MainWindow) -> None:
    from pdf_editor.ui.main_window import NOT_UNDOABLE_MESSAGE

    win.history.max_bytes = 10  # every snapshot is now too large to keep
    assert win.rotate_right()
    assert win.statusBar().currentMessage() == NOT_UNDOABLE_MESSAGE
    assert not win.undo_action.isEnabled()


def test_close_action_has_all_platform_shortcuts(win: MainWindow) -> None:
    from PySide6.QtGui import QKeySequence

    expected = QKeySequence.keyBindings(QKeySequence.StandardKey.Close)
    assert win.close_action.shortcuts() == expected
