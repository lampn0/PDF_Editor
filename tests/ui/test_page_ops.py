"""Tests for T6: page operations and undo/redo wired into the main window."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from pdf_editor.ui.main_window import MainWindow


def _make_pdf(path: Path, prefix: str, count: int, **save_kwargs) -> Path:
    doc = pymupdf.open()
    for i in range(count):
        page = doc.new_page()
        page.insert_text((72, 72), f"{prefix}-{i + 1}")
    doc.save(path, **save_kwargs)
    doc.close()
    return path


def labels(win: MainWindow) -> list[str]:
    return [page.get_text().strip() for page in win.document.raw]


def wait_thumbs(win: MainWindow, qtbot) -> None:
    qtbot.waitUntil(
        lambda: (
            win.thumbnails.count() == win.document.page_count
            and win.thumbnails.ready_count == win.document.page_count
        ),
        timeout=20_000,
    )


@pytest.fixture
def editor(make_window, qtbot, tmp_path: Path) -> MainWindow:
    path = _make_pdf(tmp_path / "doc.pdf", "P", 4)
    win = make_window()
    assert win.open_path(path)
    wait_thumbs(win, qtbot)
    return win


@pytest.fixture
def no_dialogs(monkeypatch) -> list[tuple[str, str]]:
    """Record critical/warning boxes instead of showing them; confirm questions."""
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox, "critical", staticmethod(lambda *a, **k: shown.append(("critical", a[2])))
    )

    def warning(*a, **k):
        shown.append(("warning", a[2]))
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(warning))
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    return shown


def choose_file(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(path), ""))
    )


# -- insert ------------------------------------------------------------------


def test_insert_blank_before_and_after(editor: MainWindow, qtbot) -> None:
    editor.set_current_page(1)
    assert editor.insert_blank_before_action.isEnabled()
    editor.insert_blank_before_action.trigger()
    assert labels(editor) == ["P-1", "", "P-2", "P-3", "P-4"]
    assert editor.current_page == 1
    wait_thumbs(editor, qtbot)
    assert editor.thumbnails.currentRow() == 1

    editor.set_current_page(4)
    editor.insert_blank_after_action.trigger()
    assert labels(editor) == ["P-1", "", "P-2", "P-3", "P-4", ""]
    assert editor.current_page == 5
    wait_thumbs(editor, qtbot)
    assert editor.isWindowModified()


def test_insert_pages_from_file(editor: MainWindow, qtbot, tmp_path, monkeypatch) -> None:
    src = _make_pdf(tmp_path / "src.pdf", "S", 2)
    choose_file(monkeypatch, src)
    asked = []
    monkeypatch.setattr(
        editor, "ask_page_range", lambda name, count: asked.append(count) or (0, count - 1)
    )
    editor.set_current_page(0)
    assert editor.insert_from_file()
    assert asked == [2]
    assert labels(editor) == ["P-1", "S-1", "S-2", "P-2", "P-3", "P-4"]
    assert editor.current_page == 1
    wait_thumbs(editor, qtbot)


def test_insert_page_range_from_file(editor: MainWindow, qtbot, tmp_path, monkeypatch) -> None:
    src = _make_pdf(tmp_path / "src.pdf", "S", 4)
    choose_file(monkeypatch, src)
    monkeypatch.setattr(editor, "ask_page_range", lambda name, count: (1, 2))
    editor.set_current_page(0)
    assert editor.insert_from_file()
    assert labels(editor) == ["P-1", "S-2", "S-3", "P-2", "P-3", "P-4"]
    editor.undo()
    assert labels(editor) == ["P-1", "P-2", "P-3", "P-4"]
    wait_thumbs(editor, qtbot)


def test_insert_page_range_cancel_changes_nothing(
    editor: MainWindow, tmp_path, monkeypatch
) -> None:
    src = _make_pdf(tmp_path / "src.pdf", "S", 3)
    choose_file(monkeypatch, src)
    monkeypatch.setattr(editor, "ask_page_range", lambda name, count: None)
    assert editor.insert_from_file() is False
    assert labels(editor) == ["P-1", "P-2", "P-3", "P-4"]
    assert not editor.history.can_undo
    assert not editor.isWindowModified()


def test_ask_page_range_dialog(editor: MainWindow, qtbot) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QSpinBox

    def fill() -> None:
        dlg = next(
            w for w in QApplication.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible()
        )
        first, last = dlg.findChildren(QSpinBox)
        first.setValue(2)
        last.setValue(3)
        dlg.accept()

    QTimer.singleShot(0, fill)
    assert editor.ask_page_range("x.pdf", 5) == (1, 2)
    assert editor.ask_page_range("one.pdf", 1) == (0, 0)


def test_insert_from_encrypted_file_prompts(
    editor: MainWindow, qtbot, tmp_path, monkeypatch
) -> None:
    src = _make_pdf(
        tmp_path / "locked.pdf",
        "E",
        1,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="pw",
        owner_pw="own",
    )
    choose_file(monkeypatch, src)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("pw", True)))
    editor.set_current_page(3)
    assert editor.insert_from_file()
    assert labels(editor) == ["P-1", "P-2", "P-3", "P-4", "E-1"]
    wait_thumbs(editor, qtbot)


def test_insert_from_invalid_file_shows_error(
    editor: MainWindow, tmp_path, monkeypatch, no_dialogs
) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")
    choose_file(monkeypatch, bad)
    assert editor.insert_from_file() is False
    assert [kind for kind, _ in no_dialogs] == ["critical"]
    assert editor.document.page_count == 4
    assert not editor.history.can_undo


def test_replace_page_from_file(editor: MainWindow, qtbot, tmp_path, monkeypatch) -> None:
    src = _make_pdf(tmp_path / "src.pdf", "R", 3)
    choose_file(monkeypatch, src)
    asked: list[tuple] = []

    def get_int(*args, **kwargs):
        asked.append(args[3:6])
        return 2, True

    monkeypatch.setattr(QInputDialog, "getInt", staticmethod(get_int))
    editor.set_current_page(2)
    assert editor.replace_page_from_file()
    assert asked == [(1, 1, 3)]
    assert labels(editor) == ["P-1", "P-2", "R-2", "P-4"]
    assert editor.current_page == 2
    wait_thumbs(editor, qtbot)


# -- delete ------------------------------------------------------------------


def test_delete_selected_pages(editor: MainWindow, qtbot, no_dialogs) -> None:
    editor.thumbnails.select_pages([1, 2], 2)
    editor.delete_pages_action.trigger()
    assert labels(editor) == ["P-1", "P-4"]
    assert editor.current_page == 1
    wait_thumbs(editor, qtbot)
    assert editor.thumbnails.count() == 2


def test_delete_last_page_keeps_valid_current(editor: MainWindow, no_dialogs) -> None:
    editor.set_current_page(3)
    assert editor.delete_selected_pages()
    assert labels(editor) == ["P-1", "P-2", "P-3"]
    assert editor.current_page == 2


def test_delete_cancelled_or_all_refused(editor: MainWindow, monkeypatch, no_dialogs) -> None:
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
    )
    assert editor.delete_selected_pages() is False
    assert editor.document.page_count == 4

    editor.thumbnails.selectAll()
    assert editor.delete_selected_pages() is False
    assert [kind for kind, _ in no_dialogs] == ["warning"]
    assert editor.document.page_count == 4
    assert not editor.history.can_undo


def test_delete_disabled_with_single_page(editor: MainWindow, no_dialogs) -> None:
    for _ in range(3):
        editor.set_current_page(0)
        assert editor.delete_selected_pages()
    assert editor.document.page_count == 1
    assert not editor.delete_pages_action.isEnabled()
    assert editor.delete_pages_action.shortcut() == QKeySequence(QKeySequence.StandardKey.Delete)


# -- rotate ------------------------------------------------------------------


def test_rotate_selected_pages(editor: MainWindow, qtbot) -> None:
    editor.thumbnails.select_pages([0, 2], 0)
    editor.rotate_right_action.trigger()
    assert [p.rotation for p in editor.document.raw] == [90, 0, 90, 0]
    assert editor.thumbnails.selected_pages() == [0, 2]  # selection kept
    editor.rotate_left_action.trigger()
    editor.rotate_left_action.trigger()
    assert [p.rotation for p in editor.document.raw] == [270, 0, 270, 0]
    assert editor.rotate_left_action.shortcut().toString() == "Ctrl+L"
    assert editor.rotate_right_action.shortcut().toString() == "Ctrl+R"
    wait_thumbs(editor, qtbot)


def test_rotate_rerenders_page(editor: MainWindow) -> None:
    before = editor.page_view.pixmap().size()
    editor.rotate_right()
    after = editor.page_view.pixmap().size()
    assert (after.width(), after.height()) == (before.height(), before.width())


# -- move --------------------------------------------------------------------


def test_move_up_down_follows_page(editor: MainWindow, qtbot) -> None:
    editor.set_current_page(0)
    assert not editor.move_up_action.isEnabled()
    assert editor.move_down_action.isEnabled()
    editor.move_down_action.trigger()
    assert labels(editor) == ["P-2", "P-1", "P-3", "P-4"]
    assert editor.current_page == 1
    editor.set_current_page(3)
    assert not editor.move_down_action.isEnabled()
    editor.move_up_action.trigger()
    assert labels(editor) == ["P-2", "P-1", "P-4", "P-3"]
    assert editor.current_page == 2
    wait_thumbs(editor, qtbot)
    assert editor.thumbnails.currentRow() == 2


def test_thumbnail_drag_signal_moves_page(editor: MainWindow, qtbot) -> None:
    editor.thumbnails.page_moved.emit(0, 3)
    assert labels(editor) == ["P-2", "P-3", "P-4", "P-1"]
    assert editor.current_page == 3
    wait_thumbs(editor, qtbot)
    assert editor.thumbnails.item(3).text() == "4"


# -- undo / redo ---------------------------------------------------------------


def test_undo_redo_refreshes_view_and_thumbnails(editor: MainWindow, qtbot) -> None:
    assert not editor.undo_action.isEnabled()
    assert not editor.redo_action.isEnabled()
    editor.set_current_page(3)
    editor.insert_blank_after()
    wait_thumbs(editor, qtbot)
    assert editor.thumbnails.count() == 5
    assert editor.undo_action.isEnabled()
    assert editor.undo_action.text() == "&Undo Insert Blank Page"

    editor.undo_action.trigger()
    assert editor.document.page_count == 4
    assert editor.current_page == 3  # clamped to a valid page
    wait_thumbs(editor, qtbot)
    assert editor.thumbnails.count() == 4
    assert not editor.undo_action.isEnabled()
    assert editor.redo_action.text() == "&Redo Insert Blank Page"
    assert not editor.isWindowModified()
    assert not editor.page_view.pixmap().isNull()

    editor.redo_action.trigger()
    assert labels(editor) == ["P-1", "P-2", "P-3", "P-4", ""]
    wait_thumbs(editor, qtbot)
    assert editor.thumbnails.count() == 5
    assert editor.isWindowModified()


def test_undo_redo_shortcuts(make_window) -> None:
    win = make_window()
    assert win.undo_action.shortcut().toString() == "Ctrl+Z"
    shortcuts = {s.toString() for s in win.redo_action.shortcuts()}
    assert shortcuts == {"Ctrl+Y", "Ctrl+Shift+Z"}
    assert not win.rotate_left_action.isEnabled()
    assert not win.insert_from_file_action.isEnabled()
    assert not win.undo_action.isEnabled()


# -- save ----------------------------------------------------------------------


def test_save_via_history_then_undo_marks_modified(editor: MainWindow, qtbot) -> None:
    editor.rotate_right()
    assert editor.isWindowModified()
    assert editor.save()
    assert not editor.isWindowModified()
    assert editor.history.is_clean
    with pymupdf.open(editor.document.path) as saved:
        assert saved[0].rotation == 90

    editor.undo()
    assert editor.isWindowModified()
    assert editor.document.is_dirty
    editor.redo()
    assert not editor.isWindowModified()


def test_save_as_via_history(editor: MainWindow, tmp_path, monkeypatch) -> None:
    target = tmp_path / "copy"
    monkeypatch.setattr(editor, "ask_save_path", lambda start: str(target))
    editor.insert_blank_before()
    assert editor.save_as()
    assert editor.document.path == tmp_path / "copy.pdf"
    assert "copy.pdf" in editor.windowTitle()
    assert not editor.isWindowModified()
    with pymupdf.open(tmp_path / "copy.pdf") as saved:
        assert saved.page_count == 5


def test_dirty_after_edit_prompts_on_close(editor: MainWindow, monkeypatch) -> None:
    calls: list[int] = []

    def warning(*a, **k):
        calls.append(1)
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(warning))
    editor.rotate_left()
    assert editor.close() is False
    assert calls == [1]
    editor.undo()
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: 1 / 0))
    assert editor.close() is True  # back to the saved state: no prompt


# -- multi-page drag and partial thumbnail updates -------------------------------


def test_block_drag_moves_selected_pages_in_one_step(editor: MainWindow, qtbot) -> None:
    editor.thumbnails.pages_moved.emit([0, 2], 4)
    assert labels(editor) == ["P-2", "P-4", "P-1", "P-3"]
    assert editor.thumbnails.selected_pages() == [2, 3]
    assert editor.current_page == 2
    assert editor.undo_action.text() == "&Undo Move Pages"
    editor.undo()
    assert labels(editor) == ["P-1", "P-2", "P-3", "P-4"]
    wait_thumbs(editor, qtbot)


def test_rotate_updates_only_changed_thumbnails(editor: MainWindow, qtbot, monkeypatch) -> None:
    calls: list[str] = []
    thumbs = editor.thumbnails
    original_rebuild = thumbs.rebuild
    monkeypatch.setattr(
        thumbs, "rebuild", lambda *a, **k: (calls.append("rebuild"), original_rebuild(*a, **k))
    )
    first = thumbs.item(1).icon().cacheKey()
    other = thumbs.item(0).icon().cacheKey()
    editor.set_current_page(1)
    editor.rotate_right()
    wait_thumbs(editor, qtbot)
    assert calls == []
    assert thumbs.item(1).icon().cacheKey() != first
    assert thumbs.item(0).icon().cacheKey() == other


def test_open_cancelled_by_save_prompt_keeps_current(
    editor: MainWindow, tmp_path, monkeypatch
) -> None:
    other = _make_pdf(tmp_path / "other.pdf", "O", 2)
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel)
    )
    editor.rotate_right()
    assert editor.open_path(other) is False
    assert labels(editor) == ["P-1", "P-2", "P-3", "P-4"]
    assert editor.isWindowModified()
