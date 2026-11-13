"""Tests for T7: in-place text editing through the overlay."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtWidgets import QMessageBox

from pdf_editor.core.text import get_spans
from pdf_editor.ui.main_window import (
    NO_TEXT_MESSAGE,
    OVERFLOW_CANCEL,
    OVERFLOW_KEEP,
    OVERFLOW_SHRINK,
    MainWindow,
)

WAIT_MS = 20_000
SAMPLE = "Thiet_ke_VLSI_Exercise.pdf"


def _make_pdf(path: Path, *, rotation: int = 0, text_rotate: int = 0, blank: bool = False) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    if not blank:
        page.insert_text((100, 200), "Hello World", fontsize=20, rotate=text_rotate)
        page.insert_text((100, 400), "Second", fontsize=14, rotate=text_rotate)
    page.set_rotation(rotation)
    doc.save(path)
    doc.close()
    return path


def texts(win: MainWindow, page: int = 0) -> list[str]:
    return [s.text for s in get_spans(win.document, page)]


def span_with(win: MainWindow, text: str, page: int = 0):
    return next(s for s in get_spans(win.document, page) if s.text == text)


def display_rect(win: MainWindow, span) -> QRectF:
    x0, y0, x1, y1 = span.bbox
    return win.page_view.page_transform.mapRect(QRectF(x0, y0, x1 - x0, y1 - y0))


@pytest.fixture
def editor_win(make_window, tmp_path: Path, qtbot) -> MainWindow:
    win = make_window()
    assert win.open_path(_make_pdf(tmp_path / "text.pdf"))
    return win


def open_by_click(win: MainWindow, qtbot, span) -> None:
    view = win.page_view
    cx = (span.bbox[0] + span.bbox[2]) / 2
    cy = (span.bbox[1] + span.bbox[3]) / 2
    if not win.edit_text_action.isChecked():
        win.edit_text_action.trigger()
    pos = view.page_to_viewport(cx, cy).toPoint()
    assert view.viewport().rect().contains(pos)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    qtbot.waitUntil(lambda: win.text_overlay.is_editing, timeout=WAIT_MS)


def type_and_enter(win: MainWindow, qtbot, text: str) -> None:
    editor = win.text_overlay.editor
    assert editor.isVisible() or win.text_overlay.editor_proxy.isVisible()
    editor.setText(text)
    qtbot.keyClick(editor, Qt.Key.Key_Return)
    assert not win.text_overlay.is_editing


# -- basic flow -----------------------------------------------------------------


def test_edit_sample_span_via_overlay_and_undo(make_window, data_dir: Path, qtbot) -> None:
    win = make_window()
    assert win.open_path(data_dir / SAMPLE)
    span = next(
        s
        for s in get_spans(win.document, 0)
        if len(s.text.strip()) >= 6 and s.text.isascii() and s.dir == (1.0, 0.0)
    )
    view = win.page_view
    if (
        not view.viewport()
        .rect()
        .contains(view.page_to_viewport((span.bbox[0] + span.bbox[2]) / 2, span.bbox[1]).toPoint())
    ):
        view.centerOn(view.page_to_scene(span.bbox[0], span.bbox[1]))
    open_by_click(win, qtbot, span)
    assert win.text_overlay.editor.text() == span.text
    new_text = span.text.strip()[:3]
    before_key = view.pixmap().cacheKey()
    with qtbot.waitSignal(view.page_rendered, timeout=WAIT_MS):
        type_and_enter(win, qtbot, new_text)
    assert new_text in texts(win)
    assert view.pixmap().cacheKey() != before_key
    assert win.isWindowModified()
    assert win.history.undo_count == 1
    assert win.undo_action.text() == "&Undo Edit Text"

    win.undo()
    assert span.text in texts(win)
    assert not win.isWindowModified()


def test_unchanged_text_closes_without_edit(editor_win: MainWindow, qtbot) -> None:
    span = span_with(editor_win, "Hello World")
    open_by_click(editor_win, qtbot, span)
    type_and_enter(editor_win, qtbot, "Hello World")
    qtbot.wait(20)
    assert not editor_win.document.is_dirty
    assert editor_win.history.undo_count == 0


def test_escape_cancels(editor_win: MainWindow, qtbot) -> None:
    span = span_with(editor_win, "Hello World")
    open_by_click(editor_win, qtbot, span)
    editor_win.text_overlay.editor.setText("Changed")
    qtbot.keyClick(editor_win.text_overlay.editor, Qt.Key.Key_Escape)
    qtbot.wait(20)
    assert not editor_win.text_overlay.is_editing
    assert not editor_win.document.is_dirty
    assert "Hello World" in texts(editor_win)


def test_zoom_and_page_change_cancel_editor(editor_win: MainWindow, qtbot) -> None:
    span = span_with(editor_win, "Hello World")
    editor_win.text_overlay.open_editor(span)
    assert editor_win.text_overlay.is_editing
    editor_win.page_view.zoom_in()
    assert not editor_win.text_overlay.is_editing
    editor_win.text_overlay.open_editor(span)
    editor_win.insert_blank_after()  # document change cancels too
    assert not editor_win.text_overlay.is_editing


def test_hover_highlights_span_in_edit_mode(editor_win: MainWindow, qtbot) -> None:
    view = editor_win.page_view
    span = span_with(editor_win, "Hello World")
    pos = view.page_to_viewport(
        (span.bbox[0] + span.bbox[2]) / 2, (span.bbox[1] + span.bbox[3]) / 2
    ).toPoint()
    highlight = editor_win.text_overlay._highlight
    qtbot.mouseMove(view.viewport(), pos)
    assert not highlight.isVisible()  # not in edit mode
    editor_win.edit_text_action.trigger()
    # Offscreen mouse-move delivery is unreliable; emit the view's hover signal.
    view.page_hovered.emit(0, *view.viewport_to_page(pos.toPointF()))
    assert highlight.isVisible()
    assert highlight.rect() == QRectF(
        span.bbox[0], span.bbox[1], span.bbox[2] - span.bbox[0], span.bbox[3] - span.bbox[1]
    )
    assert view.viewport().cursor().shape() == Qt.CursorShape.IBeamCursor
    editor_win.edit_text_action.trigger()
    assert not highlight.isVisible()


def test_edit_mode_shortcuts_and_enabled_state(make_window) -> None:
    win = make_window()
    assert not win.edit_text_action.isEnabled()
    keys = {k.toString() for k in win.edit_text_action.shortcuts()}
    assert keys == {"Ctrl+E", "T"}


def test_no_text_page_shows_status(make_window, tmp_path: Path) -> None:
    win = make_window()
    assert win.open_path(_make_pdf(tmp_path / "blank.pdf", blank=True))
    win.edit_text_action.trigger()
    assert win.statusBar().currentMessage() == NO_TEXT_MESSAGE


# -- overflow / substitution / errors -------------------------------------------


LONG_TEXT = "This replacement is very much longer than before"


@pytest.mark.parametrize("choice", [OVERFLOW_SHRINK, OVERFLOW_KEEP])
def test_overflow_choices_leave_one_undo_step(
    editor_win: MainWindow, qtbot, monkeypatch, choice: str
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        editor_win, "ask_overflow_choice", lambda span, text: (asked.append(text), choice)[1]
    )
    span = span_with(editor_win, "Second")
    open_by_click(editor_win, qtbot, span)
    type_and_enter(editor_win, qtbot, LONG_TEXT)
    qtbot.waitUntil(lambda: LONG_TEXT in texts(editor_win), timeout=WAIT_MS)
    assert asked == [LONG_TEXT]
    assert editor_win.history.undo_count == 1
    assert not editor_win.history.can_redo
    new = span_with(editor_win, LONG_TEXT)
    if choice == OVERFLOW_SHRINK:
        assert new.size < span.size
    else:
        assert new.size == pytest.approx(span.size, abs=0.01)


def test_overflow_cancel_leaves_document_clean(editor_win: MainWindow, qtbot, monkeypatch) -> None:
    monkeypatch.setattr(editor_win, "ask_overflow_choice", lambda span, text: OVERFLOW_CANCEL)
    span = span_with(editor_win, "Second")
    assert editor_win.edit_span_text(span, LONG_TEXT) is None
    assert not editor_win.document.is_dirty
    assert not editor_win.isWindowModified()
    assert editor_win.history.undo_count == 0
    assert not editor_win.history.can_redo
    assert "Second" in texts(editor_win)


def test_font_substitution_notice(editor_win: MainWindow) -> None:
    span = span_with(editor_win, "Hello World")
    result = editor_win.edit_span_text(span, "Việt Nam")
    assert result is not None and result.substituted
    assert editor_win.statusBar().currentMessage().startswith("Font substituted:")
    assert "Việt Nam" in texts(editor_win)


def test_stale_span_shows_warning_and_keeps_document(editor_win: MainWindow, monkeypatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    span = span_with(editor_win, "Hello World")
    assert editor_win.edit_span_text(span, "Hi") is not None
    assert editor_win.edit_span_text(span, "Again") is None  # old span is stale now
    assert len(warnings) == 1
    assert editor_win.history.undo_count == 1
    assert "Hi" in texts(editor_win)


# -- placement --------------------------------------------------------------------


def _assert_editor_over(win: MainWindow, span) -> None:
    rect = display_rect(win, span)
    editor_rect = win.text_overlay.editor_proxy.sceneBoundingRect()
    assert editor_rect.contains(rect.center())
    assert editor_rect.left() == pytest.approx(rect.left(), abs=1.0)
    assert editor_rect.top() <= rect.top() + 1.0
    assert editor_rect.bottom() >= rect.bottom() - 1.0


def test_editor_placed_over_span_at_zoom_2(editor_win: MainWindow, qtbot) -> None:
    view = editor_win.page_view
    view.set_zoom(2.0)
    view.flush_render()
    span = span_with(editor_win, "Hello World")
    view.centerOn(view.page_to_scene(span.bbox[0], span.bbox[1]))
    open_by_click(editor_win, qtbot, span)
    _assert_editor_over(editor_win, span)
    # On screen the editor's font is about span.size * zoom pixels high.
    font_px = editor_win.text_overlay.editor.font().pixelSize() * view.transform().m22()
    assert font_px == pytest.approx(span.size * 2.0, rel=0.1)


def test_editor_placed_over_upright_span_on_rotated_page(
    make_window, tmp_path: Path, qtbot
) -> None:
    win = make_window()
    assert win.open_path(_make_pdf(tmp_path / "rot.pdf", rotation=90, text_rotate=90))
    span = span_with(win, "Hello World")
    rect = display_rect(win, span)
    assert rect.width() > rect.height()  # text is upright on screen
    open_by_click(win, qtbot, span)
    _assert_editor_over(win, span)
    type_and_enter(win, qtbot, "Rotated ok")
    qtbot.waitUntil(lambda: "Rotated ok" in texts(win), timeout=WAIT_MS)
