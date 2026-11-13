"""Application main window."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QSpinBox,
    QWidget,
)

from pdf_editor.core import pages
from pdf_editor.core.document import PdfDocument
from pdf_editor.core.errors import PasswordRequiredError, PdfError
from pdf_editor.core.history import Command, History
from pdf_editor.core.text import EditResult, TextSpan, measure_replacement, replace_span
from pdf_editor.ui.page_view import PageView
from pdf_editor.ui.text_edit_overlay import TextEditOverlay
from pdf_editor.ui.thumbnail_panel import ThumbnailPanel, block_move_steps

APP_NAME = "PDF Editor"
PDF_FILTER = "PDF files (*.pdf);;All files (*)"
OVERFLOW_SHRINK = "shrink"
OVERFLOW_KEEP = "keep"
OVERFLOW_CANCEL = "cancel"
NO_TEXT_MESSAGE = "No editable text on this page (scanned?)"
NOT_UNDOABLE_MESSAGE = "This change is too large to be undone."
ENCRYPTED_SAVE_MESSAGE = "Saved. The file remains password-protected."


class MainWindow(QMainWindow):
    """Main window: menus, toolbar, thumbnail dock, page view and status bar.

    Every document mutation goes through :attr:`history` (``History.execute``);
    the window re-renders whenever the history reports a change.
    """

    document_changed = Signal()  # a document was opened or closed

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._doc: PdfDocument | None = None
        self._history: History | None = None
        # Page/selection to show after the next history change (set by _run).
        self._pending_page: int | None = None
        self._pending_selection: list[int] | None = None
        self._pending_changed: list[int] | None = None
        self._saving = False

        self.page_view = PageView(self)
        self.setCentralWidget(self.page_view)
        self.text_overlay = TextEditOverlay(self.page_view)

        self.thumbnails = ThumbnailPanel(self)
        self.thumbnail_dock = QDockWidget("Pages", self)
        self.thumbnail_dock.setObjectName("thumbnail_dock")
        self.thumbnail_dock.setWidget(self.thumbnails)
        self.thumbnail_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
        )
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.thumbnail_dock)

        self._page_label = QLabel()
        self._zoom_label = QLabel()
        self.statusBar().addPermanentWidget(self._page_label)
        self.statusBar().addPermanentWidget(self._zoom_label)

        self._create_actions()
        self._create_menus()
        self._create_toolbar()
        self.thumbnails.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        self.thumbnails.addActions(self._page_actions)

        self.page_view.current_page_changed.connect(self._on_page_changed)
        self.page_view.zoom_changed.connect(self._on_zoom_changed)
        self.page_view.render_failed.connect(self._on_render_failed)
        self.thumbnails.page_selected.connect(self.set_current_page)
        self.thumbnails.page_moved.connect(self.move_page)
        self.thumbnails.pages_moved.connect(self.move_pages)
        self.text_overlay.commit_requested.connect(self.edit_span_text)
        self.text_overlay.no_text_on_page.connect(
            lambda _page: self.statusBar().showMessage(NO_TEXT_MESSAGE, 5000)
        )

        self.resize(1100, 800)
        self._update_ui()

    # -- setup --------------------------------------------------------------

    def _action(
        self,
        text: str,
        slot: Callable[..., object],
        shortcut: str | QKeySequence | QKeySequence.StandardKey | None = None,
    ) -> QAction:
        action = QAction(text, self)
        if isinstance(shortcut, QKeySequence.StandardKey):
            action.setShortcuts(shortcut)  # all platform bindings (e.g. Ctrl+F4 and Ctrl+W)
        elif shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def _create_actions(self) -> None:
        self.open_action = self._action("&Open...", self._on_open, "Ctrl+O")
        self.save_action = self._action("&Save", self.save, "Ctrl+S")
        self.save_as_action = self._action("Save &As...", self.save_as, "Ctrl+Shift+S")
        self.close_action = self._action(
            "&Close", self.close_document, QKeySequence.StandardKey.Close
        )
        self.remove_password_action = self._action(
            "Remove &Password Protection", self.remove_password_protection
        )
        self.exit_action = self._action("E&xit", self.close, "Ctrl+Q")

        self.undo_action = self._action("&Undo", self.undo, "Ctrl+Z")
        self.redo_action = self._action("&Redo", self.redo)
        self.redo_action.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])

        self.zoom_in_action = self._action("Zoom &In", self.page_view.zoom_in)
        self.zoom_in_action.setShortcuts([QKeySequence("Ctrl+="), QKeySequence("Ctrl++")])
        self.zoom_out_action = self._action("Zoom &Out", self.page_view.zoom_out, "Ctrl+-")
        self.fit_width_action = self._action("&Fit Width", self.page_view.fit_width)
        self.actual_size_action = self._action("&Actual Size", self.page_view.actual_size, "Ctrl+0")

        self.insert_blank_before_action = self._action(
            "Insert Blank Page &Before", self.insert_blank_before
        )
        self.insert_blank_after_action = self._action(
            "Insert Blank Page &After", self.insert_blank_after
        )
        self.insert_from_file_action = self._action(
            "Insert Pages from &File...", self.insert_from_file
        )
        self.replace_page_action = self._action(
            "Re&place Page from File...", self.replace_page_from_file
        )
        self.delete_pages_action = self._action(
            "&Delete Page(s)", self.delete_selected_pages, QKeySequence.StandardKey.Delete
        )
        self.rotate_left_action = self._action("Rotate &Left", self.rotate_left, "Ctrl+L")
        self.rotate_right_action = self._action("Rotate &Right", self.rotate_right, "Ctrl+R")
        self.move_up_action = self._action("Move Page &Up", self.move_page_up)
        self.move_down_action = self._action("Move Page Do&wn", self.move_page_down)
        self._page_actions = [
            self.insert_blank_before_action,
            self.insert_blank_after_action,
            self.insert_from_file_action,
            self.replace_page_action,
            self.delete_pages_action,
            self.rotate_left_action,
            self.rotate_right_action,
            self.move_up_action,
            self.move_down_action,
        ]

        self.edit_text_action = QAction("Edit &Text", self)
        self.edit_text_action.setCheckable(True)
        self.edit_text_action.setShortcuts([QKeySequence("Ctrl+E"), QKeySequence("T")])
        self.edit_text_action.setStatusTip("Click a text span on the page to edit it")
        self.edit_text_action.toggled.connect(self.text_overlay.set_enabled)

        self._document_actions = [
            self.save_action,
            self.save_as_action,
            self.close_action,
            self.zoom_in_action,
            self.zoom_out_action,
            self.fit_width_action,
            self.actual_size_action,
        ]

    def _create_menus(self) -> None:
        bar = self.menuBar()
        self.file_menu = bar.addMenu("&File")
        self.file_menu.addActions([self.open_action, self.save_action, self.save_as_action])
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.remove_password_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.close_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.exit_action)

        self.edit_menu = bar.addMenu("&Edit")
        self.edit_menu.addActions([self.undo_action, self.redo_action])
        self.edit_menu.addSeparator()
        self.edit_menu.addAction(self.edit_text_action)

        self.view_menu = bar.addMenu("&View")
        self.view_menu.addActions(
            [
                self.zoom_in_action,
                self.zoom_out_action,
                self.fit_width_action,
                self.actual_size_action,
            ]
        )
        self.view_menu.addSeparator()
        self.view_menu.addAction(self.thumbnail_dock.toggleViewAction())

        self.page_menu = bar.addMenu("&Page")
        self.page_menu.addActions(
            [
                self.insert_blank_before_action,
                self.insert_blank_after_action,
                self.insert_from_file_action,
                self.replace_page_action,
            ]
        )
        self.page_menu.addSeparator()
        self.page_menu.addAction(self.delete_pages_action)
        self.page_menu.addSeparator()
        self.page_menu.addActions([self.rotate_left_action, self.rotate_right_action])
        self.page_menu.addSeparator()
        self.page_menu.addActions([self.move_up_action, self.move_down_action])

    def _create_toolbar(self) -> None:
        self.toolbar = self.addToolBar("Main")
        self.toolbar.setObjectName("main_toolbar")
        self.toolbar.addActions([self.open_action, self.save_action, self.save_as_action])
        self.toolbar.addSeparator()
        self.toolbar.addActions([self.undo_action, self.redo_action, self.edit_text_action])
        self.toolbar.addSeparator()
        self.toolbar.addActions(
            [
                self.zoom_out_action,
                self.zoom_in_action,
                self.fit_width_action,
                self.actual_size_action,
            ]
        )
        self.toolbar.addSeparator()
        self.toolbar.addActions(
            [
                self.insert_blank_after_action,
                self.insert_from_file_action,
                self.delete_pages_action,
                self.rotate_left_action,
                self.rotate_right_action,
            ]
        )

    # -- public API: document -------------------------------------------------

    @property
    def document(self) -> PdfDocument | None:
        return self._doc

    @property
    def history(self) -> History | None:
        """Undo/redo history of the open document (None when nothing is open)."""
        return self._history

    @property
    def current_page(self) -> int:
        return self.page_view.current_page

    @Slot(int)
    def set_current_page(self, index: int) -> None:
        self.page_view.set_current_page(index)

    def open_path(self, path: str | os.PathLike[str]) -> bool:
        """Open ``path``, replacing the current document. Returns True on success.

        Errors are reported in a message box; an encrypted file prompts for a
        password until it is correct or the user cancels.
        """
        doc = self._open_with_password(path)
        if doc is None:
            return False
        if not self.maybe_save():
            doc.close()
            return False
        self._set_document(doc)
        return True

    def refresh(
        self,
        page: int | None = None,
        selected: Iterable[int] | None = None,
        changed_pages: Iterable[int] | None = None,
    ) -> None:
        """Re-render page and thumbnails after the document changed in place.

        ``page`` optionally selects the page to show (clamped); ``selected``
        optionally restores a thumbnail multi-selection. ``changed_pages``
        limits the thumbnail update to those pages when the page count did not
        change (otherwise all thumbnails are rebuilt).
        """
        self.page_view.refresh(page)
        current = self.page_view.current_page
        doc = self._doc
        if (
            changed_pages is not None
            and doc is not None
            and not doc.is_closed
            and doc.page_count == self.thumbnails.count()
        ):
            self.thumbnails.update_pages(changed_pages)
        else:
            self.thumbnails.rebuild(doc, current)
        if selected is not None:
            self.thumbnails.select_pages(selected, current)
        else:
            self.thumbnails.set_current(current)
        self._update_ui()

    @Slot()
    def save(self) -> bool:
        """Save to the current path (asks for one if needed). Returns True on success."""
        self._flush_editor()
        if self._doc is None or self._history is None:
            return False
        if self._doc.path is None:
            return self.save_as()
        return self._save_via_history(self._history.save, offer_save_as=True)

    @Slot()
    def save_as(self) -> bool:
        """Ask for a file name and save there. Returns True on success."""
        self._flush_editor()
        if self._doc is None or self._history is None:
            return False
        start = str(self._doc.path) if self._doc.path else ""
        path = self.ask_save_path(start)
        if not path:
            return False
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        history = self._history
        return self._save_via_history(lambda: history.save_as(path))

    def ask_save_path(self, start: str) -> str:
        """Ask for a target file. The dialog adds ".pdf" itself, so its overwrite
        confirmation covers the final name. Returns "" when cancelled."""
        dialog = QFileDialog(self, "Save As", start, PDF_FILTER)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setDefaultSuffix("pdf")
        if dialog.exec() != QFileDialog.DialogCode.Accepted:
            return ""
        files = dialog.selectedFiles()
        return files[0] if files else ""

    @Slot()
    def remove_password_protection(self) -> bool:
        """Save future copies without encryption (undoable edit; marks the doc modified)."""
        if self._doc is None or self._doc.encryption is None:
            return False
        return self._run(
            "Remove Password Protection",
            lambda d: d.set_encryption(None),
            self.current_page,
            selection=self.thumbnails.selected_pages(),
            changed_pages=[],
        )

    @Slot()
    def close_document(self) -> bool:
        """Close the current document (prompting if dirty). Returns False if cancelled."""
        if not self.maybe_save():
            return False
        self._set_document(None)
        return True

    def maybe_save(self) -> bool:
        """Offer to save unsaved changes. Returns False if the user cancelled.

        Text still open in the in-place editor is applied first.
        """
        self._flush_editor()
        if self._doc is None or not self._doc.is_dirty:
            return True
        answer = QMessageBox.warning(
            self,
            APP_NAME,
            f"Save changes to {self._display_name()} before closing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    # -- public API: undo/redo ------------------------------------------------

    @Slot()
    def undo(self) -> None:
        if self._history is None or not self._history.can_undo:
            return
        try:
            self._history.undo()
        except PdfError as exc:
            QMessageBox.critical(self, APP_NAME, f"Undo failed:\n{exc}")
            self.refresh()

    @Slot()
    def redo(self) -> None:
        if self._history is None or not self._history.can_redo:
            return
        try:
            self._history.redo()
        except PdfError as exc:
            QMessageBox.critical(self, APP_NAME, f"Redo failed:\n{exc}")
            self.refresh()

    # -- public API: page operations ------------------------------------------

    # -- public API: text editing ----------------------------------------------

    def ask_overflow_choice(self, span: TextSpan, new_text: str) -> str:
        """Ask what to do when ``new_text`` is wider than ``span``.

        Returns OVERFLOW_SHRINK, OVERFLOW_KEEP or OVERFLOW_CANCEL.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(APP_NAME)
        box.setText("The new text is wider than the original text.")
        box.setInformativeText("Shrink the font to fit, or keep the size and let it overflow?")
        shrink = box.addButton("Shrink to Fit", QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton("Keep Overflowing", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(shrink)
        box.exec()
        clicked = box.clickedButton()
        if clicked is shrink:
            return OVERFLOW_SHRINK
        if clicked is keep:
            return OVERFLOW_KEEP
        return OVERFLOW_CANCEL

    @Slot(object, str)
    def edit_span_text(self, span: TextSpan, new_text: str) -> EditResult | None:
        """Replace ``span``'s text through the history (one undo step).

        The edit is first measured (``measure_replacement``); if the text would overflow
        the user chooses to shrink, keep or cancel. Returns the result, or None
        if nothing was changed.
        """
        if self._doc is None or self._history is None or new_text == span.text:
            return None
        try:
            trial = measure_replacement(self._doc, span, new_text)
        except Exception as exc:  # PdfError or anything unexpected; doc untouched
            QMessageBox.warning(self, APP_NAME, f"Cannot edit this text:\n{exc}")
            return None
        shrink = False
        if trial.overflow:
            choice = self.ask_overflow_choice(span, new_text)
            if choice == OVERFLOW_CANCEL:
                return None
            shrink = choice == OVERFLOW_SHRINK
        result: list[EditResult] = []

        def apply(doc: PdfDocument) -> EditResult:
            res = replace_span(doc, span, new_text, shrink_to_fit=shrink)
            result.append(res)
            return res

        self._pending_page = span.page_index
        self._pending_selection = self.thumbnails.selected_pages() or None
        self._pending_changed = [span.page_index]
        try:
            self._history.execute(Command("Edit Text", apply))
        except Exception as exc:  # the history has rolled the document back
            QMessageBox.warning(self, APP_NAME, f"Cannot edit this text:\n{exc}")
            return None
        finally:
            self._pending_page = None
            self._pending_selection = None
            self._pending_changed = None
        if not result:
            return None
        self._warn_if_not_undoable()
        if result[0].substituted:
            self.statusBar().showMessage(f"Font substituted: {result[0].font_used}", 8000)
        return result[0]

    # -- public API: page operations (continued) --------------------------------

    def target_pages(self) -> list[int]:
        """Pages an operation applies to: the thumbnail selection, else the current page."""
        selected = self.thumbnails.selected_pages()
        return selected if selected else [self.current_page]

    @Slot()
    def insert_blank_before(self) -> bool:
        index = self.current_page
        return self._run("Insert Blank Page", lambda d: pages.insert_blank(d, index), index)

    @Slot()
    def insert_blank_after(self) -> bool:
        index = self.current_page + 1
        return self._run("Insert Blank Page", lambda d: pages.insert_blank(d, index), index)

    @Slot()
    def insert_from_file(self) -> bool:
        """Insert a chosen page range of another PDF after the current page."""
        if self._doc is None:
            return False
        path = self._ask_source_file("Insert Pages from File")
        if not path:
            return False
        src = self._open_with_password(path)
        if src is None:
            return False
        index = self.current_page + 1
        try:
            page_range = self.ask_page_range(Path(path).name, src.page_count)
            if page_range is None:
                return False
            first, last = page_range
            return self._run(
                "Insert Pages",
                lambda d: pages.insert_from_pdf(d, index, src, from_page=first, to_page=last),
                index,
            )
        finally:
            src.close()

    def ask_page_range(self, name: str, count: int) -> tuple[int, int] | None:
        """Ask which pages (0-based, inclusive) of a ``count``-page source to use."""
        if count <= 1:
            return (0, max(count - 1, 0))
        dialog = QDialog(self)
        dialog.setWindowTitle("Insert Pages")
        form = QFormLayout(dialog)
        form.addRow(QLabel(f"{name} has {count} pages."))
        first = QSpinBox(dialog)
        first.setRange(1, count)
        first.setValue(1)
        last = QSpinBox(dialog)
        last.setRange(1, count)
        last.setValue(count)
        first.valueChanged.connect(lambda v: last.setMinimum(v))
        form.addRow("From page:", first)
        form.addRow("To page:", last)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return (first.value() - 1, last.value() - 1)

    @Slot()
    def replace_page_from_file(self) -> bool:
        """Replace the current page with a page (chosen by number) of another PDF."""
        if self._doc is None:
            return False
        path = self._ask_source_file("Replace Page from File")
        if not path:
            return False
        src = self._open_with_password(path)
        if src is None:
            return False
        try:
            count = src.page_count
            number, ok = QInputDialog.getInt(
                self,
                APP_NAME,
                f"Page of {Path(path).name} to use (1-{count}):",
                1,
                1,
                count,
            )
            if not ok:
                return False
            index = self.current_page
            src_page = number - 1
            return self._run(
                "Replace Page",
                lambda d: pages.replace_page(d, index, src, src_page),
                index,
                changed_pages=[index],
            )
        finally:
            src.close()

    @Slot()
    def delete_selected_pages(self) -> bool:
        """Delete the target pages after confirmation; refuses to delete every page."""
        if self._doc is None:
            return False
        targets = self.target_pages()
        count = self._doc.page_count
        if len(targets) >= count:
            QMessageBox.warning(self, APP_NAME, "A document must keep at least one page.")
            return False
        what = f"page {targets[0] + 1}" if len(targets) == 1 else f"{len(targets)} pages"
        answer = QMessageBox.question(
            self,
            APP_NAME,
            f"Delete {what}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        next_page = min(targets[0], count - len(targets) - 1)
        label = "Delete Page" if len(targets) == 1 else "Delete Pages"
        return self._run(label, lambda d: pages.delete_pages(d, targets), next_page)

    @Slot()
    def rotate_left(self) -> bool:
        return self._rotate(-90)

    @Slot()
    def rotate_right(self) -> bool:
        return self._rotate(90)

    @Slot(int, int)
    def move_page(self, src: int, dst: int) -> bool:
        """Move page ``src`` to final index ``dst`` and follow it."""
        if self._doc is None or src == dst:
            return False
        return self._run("Move Page", lambda d: pages.move_page(d, src, dst), dst)

    @Slot(list, int)
    def move_pages(self, indices: list[int], insert_before: int) -> bool:
        """Move the block ``indices`` to the gap before ``insert_before`` (one undo step).

        The moved pages stay selected and the first of them becomes current.
        """
        if self._doc is None:
            return False
        count = self._doc.page_count
        steps = block_move_steps(count, indices, insert_before)
        if not steps:
            return False

        def apply(d: PdfDocument) -> None:
            for src, dst in steps:
                pages.move_page(d, src, dst)

        n = len(set(indices))
        first = len([i for i in range(insert_before) if i not in set(indices)])
        moved = list(range(first, first + n))
        return self._run("Move Pages", apply, first, selection=moved)

    @Slot()
    def move_page_up(self) -> bool:
        cur = self.current_page
        return self.move_page(cur, cur - 1) if cur > 0 else False

    @Slot()
    def move_page_down(self) -> bool:
        cur = self.current_page
        if self._doc is None or cur >= self._doc.page_count - 1:
            return False
        return self.move_page(cur, cur + 1)

    # -- internals: operations ------------------------------------------------

    def _rotate(self, degrees: int) -> bool:
        if self._doc is None:
            return False
        targets = self.target_pages()
        label = "Rotate Page" if len(targets) == 1 else "Rotate Pages"
        return self._run(
            label,
            lambda d: pages.rotate_pages(d, targets, degrees),
            self.current_page,
            selection=targets,
            changed_pages=targets,
        )

    def _run(
        self,
        label: str,
        apply: Callable[[PdfDocument], object],
        page_after: int | None = None,
        selection: list[int] | None = None,
        changed_pages: list[int] | None = None,
    ) -> bool:
        """Execute ``apply`` through the history. Errors are shown; returns success."""
        if self._history is None:
            return False
        self._pending_page = page_after
        self._pending_selection = selection
        self._pending_changed = changed_pages
        try:
            self._history.execute(Command(label, apply))
        except PdfError as exc:
            QMessageBox.critical(self, APP_NAME, f"{label} failed:\n{exc}")
            return False
        except Exception as exc:  # unexpected; the history has rolled back
            QMessageBox.warning(self, APP_NAME, f"{label} failed unexpectedly:\n{exc}")
            return False
        finally:
            self._pending_page = None
            self._pending_selection = None
            self._pending_changed = None
        self._warn_if_not_undoable()
        return True

    def _warn_if_not_undoable(self) -> None:
        if self._history is not None and not self._history.last_execute_undoable:
            self.statusBar().showMessage(NOT_UNDOABLE_MESSAGE, 10000)

    def _save_via_history(self, save: Callable[[], None], offer_save_as: bool = False) -> bool:
        self._saving = True
        try:
            save()
        except PdfError as exc:
            self._saving = False
            if offer_save_as:
                answer = QMessageBox.critical(
                    self,
                    APP_NAME,
                    f"Cannot save file:\n{exc}\n\nSave to a different file instead?",
                    QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Save,
                )
                if answer == QMessageBox.StandardButton.Save:
                    return self.save_as()
            else:
                QMessageBox.critical(self, APP_NAME, f"Cannot save file:\n{exc}")
            return False
        finally:
            self._saving = False
        self._update_ui()
        if self._doc is not None and self._doc.encryption is not None:
            self.statusBar().showMessage(ENCRYPTED_SAVE_MESSAGE, 8000)
        return True

    def _flush_editor(self) -> None:
        """Apply text still open in the in-place editor (before save/close/open)."""
        pending = self.text_overlay.take_pending()
        if pending is not None:
            self.edit_span_text(*pending)

    def _ask_source_file(self, title: str) -> str:
        start = ""
        if self._doc is not None and self._doc.path is not None:
            start = str(self._doc.path.parent)
        path, _ = QFileDialog.getOpenFileName(self, title, start, PDF_FILTER)
        return path

    def _open_with_password(self, path: str | os.PathLike[str]) -> PdfDocument | None:
        """Open ``path``, prompting for a password if needed; None on error/cancel."""
        password: str | None = None
        while True:
            try:
                return PdfDocument.open(path, password)
            except PasswordRequiredError:
                prompt = "Enter the password for this PDF:"
                if password is not None:
                    prompt = "Wrong password. Try again:"
                text, ok = QInputDialog.getText(
                    self,
                    APP_NAME,
                    f"{Path(path).name}\n{prompt}",
                    QLineEdit.EchoMode.Password,
                )
                if not ok:
                    return None
                password = text
            except PdfError as exc:
                QMessageBox.critical(self, APP_NAME, f"Cannot open file:\n{exc}")
                return None

    # -- internals: state -----------------------------------------------------

    def _set_document(self, doc: PdfDocument | None) -> None:
        old = self._doc
        if self._history is not None:
            self._history.changed.clear()
        self.text_overlay.cancel()
        self._doc = doc
        self._history = History(doc) if doc is not None else None
        if self._history is not None:
            self._history.changed.append(self._on_history_changed)
        self.page_view.set_document(doc)
        self.thumbnails.rebuild(doc, 0)
        if old is not None and old is not doc:
            old.close()
        self._update_ui()
        self.document_changed.emit()

    def _on_history_changed(self) -> None:
        if self._saving:
            self._update_ui()  # only the clean state changed
            return
        self.text_overlay.cancel()  # its span is stale after any document change
        self.refresh(self._pending_page, self._pending_selection, self._pending_changed)

    def _display_name(self) -> str:
        if self._doc is None:
            return ""
        return self._doc.path.name if self._doc.path else "Untitled"

    def _update_ui(self) -> None:
        doc = self._doc
        has_doc = doc is not None
        for action in self._document_actions:
            action.setEnabled(has_doc)
        self.edit_text_action.setEnabled(has_doc)
        self.remove_password_action.setEnabled(doc is not None and doc.encryption is not None)
        if not has_doc and self.edit_text_action.isChecked():
            self.edit_text_action.setChecked(False)
        if doc is not None:
            self.setWindowTitle(f"{self._display_name()}[*] - {APP_NAME}")
            self.setWindowModified(doc.is_dirty)
        else:
            self.setWindowTitle(APP_NAME)
            self.setWindowModified(False)
        self._update_history_actions()
        self._update_page_actions()
        self._update_status()

    def _update_history_actions(self) -> None:
        history = self._history
        can_undo = history is not None and history.can_undo
        can_redo = history is not None and history.can_redo
        self.undo_action.setEnabled(can_undo)
        self.redo_action.setEnabled(can_redo)
        self.undo_action.setText(f"&Undo {history.undo_label}" if can_undo else "&Undo")
        self.redo_action.setText(f"&Redo {history.redo_label}" if can_redo else "&Redo")

    def _update_page_actions(self) -> None:
        count = self.page_view.page_count if self._doc is not None else 0
        cur = self.page_view.current_page
        for action in (
            self.insert_blank_before_action,
            self.insert_blank_after_action,
            self.insert_from_file_action,
            self.replace_page_action,
            self.rotate_left_action,
            self.rotate_right_action,
        ):
            action.setEnabled(count > 0)
        self.delete_pages_action.setEnabled(count > 1)
        self.move_up_action.setEnabled(count > 1 and cur > 0)
        self.move_down_action.setEnabled(count > 1 and cur < count - 1)

    def _update_status(self) -> None:
        count = self.page_view.page_count
        if count:
            self._page_label.setText(f"Page {self.page_view.current_page + 1} / {count}")
        else:
            self._page_label.setText("No document")
        self._zoom_label.setText(f"{round(self.page_view.zoom * 100)}%")

    @Slot()
    def _on_open(self) -> None:
        path = self._ask_source_file("Open PDF")
        if path:
            self.open_path(path)

    @Slot(int)
    def _on_page_changed(self, index: int) -> None:
        self.thumbnails.set_current(index)
        self._update_page_actions()
        self._update_status()

    @Slot(float)
    def _on_zoom_changed(self, _zoom: float) -> None:
        self._update_status()

    @Slot(str)
    def _on_render_failed(self, message: str) -> None:
        self.statusBar().showMessage(f"Render failed: {message}", 5000)

    # -- events ---------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.maybe_save():
            event.ignore()
            return
        self.shutdown()
        event.accept()

    def shutdown(self) -> None:
        """Stop background work and release the document (idempotent)."""
        self.thumbnails.shutdown()
        if self._history is not None:
            self._history.changed.clear()
            self._history = None
        if self._doc is not None:
            self.page_view.set_document(None)
            self._doc.close()
            self._doc = None
