"""Sidebar panel listing page thumbnails.

The panel never reorders its own items: an internal drag-and-drop only emits
:attr:`ThumbnailPanel.page_moved` (one page) or :attr:`ThumbnailPanel.pages_moved`
(a multi-selection); whoever owns the document performs the move and then
calls :meth:`ThumbnailPanel.rebuild`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from PySide6.QtCore import QItemSelectionModel, QPoint, QSize, Qt, Signal, Slot
from PySide6.QtGui import QColor, QDropEvent, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QWidget,
)

from pdf_editor.core.document import PdfDocument
from pdf_editor.ui.render_worker import ThumbnailRenderer

THUMBNAIL_SIZE = 160
PAGE_ROLE = Qt.ItemDataRole.UserRole


def move_destination(src: int, insert_before: int) -> int:
    """Final index of an item moved from ``src`` to the gap before ``insert_before``.

    ``insert_before`` is an index in the list *before* the move (``count`` means
    "after the last item"); the result is the index in the list *after* the move.
    """
    return insert_before - 1 if insert_before > src else insert_before


def block_move_steps(count: int, block: Sequence[int], insert_before: int) -> list[tuple[int, int]]:
    """Single-page moves ``(src, dst)`` that move ``block`` to the gap before ``insert_before``.

    The pages of ``block`` keep their relative order and end up contiguous.
    Apply the steps in order with ``move_page(src, dst)`` semantics (``dst`` is
    the final index after that single move).
    """
    chosen = sorted(set(block))
    if not chosen:
        return []
    chosen_set = set(chosen)
    before = [i for i in range(insert_before) if i not in chosen_set]
    after = [i for i in range(insert_before, count) if i not in chosen_set]
    desired = before + chosen + after
    current = list(range(count))
    steps: list[tuple[int, int]] = []
    for target, page in enumerate(desired):
        pos = current.index(page)
        if pos != target:
            current.insert(target, current.pop(pos))
            steps.append((pos, target))
    return steps


class ThumbnailPanel(QListWidget):
    """Vertical list of page thumbnails with extended selection."""

    page_selected = Signal(int)  # user made this page current
    page_moved = Signal(int, int)  # src index, destination index (after the move)
    pages_moved = Signal(list, int)  # selected indices, insert-before index (before the move)
    thumbnails_finished = Signal()  # all thumbnails of the last request are rendered

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._ready: set[int] = set()
        self._failed: dict[int, str] = {}
        self._doc: PdfDocument | None = None

        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.TopToBottom)
        self.setWrapping(False)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.setSpacing(6)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # IconMode defaults to free movement; keep drag enabled but handle drops ourselves.
        self.setMovement(QListView.Movement.Snap)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setMinimumWidth(THUMBNAIL_SIZE + 40)

        self._placeholder = self._make_placeholder()
        self._error_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning)
        self._renderer = ThumbnailRenderer(self)
        self._renderer.thumbnail_ready.connect(self._on_thumbnail_ready)
        self._renderer.thumbnail_failed.connect(self._on_thumbnail_failed)
        self._renderer.finished.connect(self.thumbnails_finished)
        self.currentRowChanged.connect(self._on_current_row_changed)

    # -- public API ---------------------------------------------------------

    def rebuild(self, doc: PdfDocument | None, current: int = 0) -> None:
        """Recreate one item per page of ``doc`` and render thumbnails incrementally.

        ``current`` is the row made current (clamped); no signal is emitted.
        """
        self._renderer.cancel()
        self._ready.clear()
        self._failed.clear()
        self._doc = doc if doc is not None and not doc.is_closed else None
        count = self._doc.page_count if self._doc is not None else 0
        self._updating = True
        try:
            self.clear()
            for i in range(count):
                item = QListWidgetItem(self._placeholder, str(i + 1))
                item.setData(PAGE_ROLE, i)
                item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
                item.setSizeHint(QSize(THUMBNAIL_SIZE + 16, THUMBNAIL_SIZE + 28))
                self.addItem(item)
            if count:
                self.setCurrentRow(max(0, min(current, count - 1)))
        finally:
            self._updating = False
        if self._doc is not None and count:
            self._renderer.request(
                self._doc, range(count), THUMBNAIL_SIZE, self.devicePixelRatioF()
            )

    def update_pages(self, indices: Iterable[int]) -> None:
        """Re-render the thumbnails of ``indices`` only (page count unchanged).

        Falls back to a full re-render if a previous request is still running.
        """
        doc = self._doc
        if doc is None or doc.is_closed or doc.page_count != self.count():
            self.rebuild(doc, max(self.currentRow(), 0))
            return
        if self._renderer.is_busy:
            todo = set(range(self.count())) - self._ready
            todo.update(indices)
        else:
            todo = {i for i in indices if 0 <= i < self.count()}
        for i in todo:
            self._ready.discard(i)
            self._failed.pop(i, None)
        if todo:
            self._renderer.request(doc, sorted(todo), THUMBNAIL_SIZE, self.devicePixelRatioF())

    def set_current(self, index: int) -> None:
        """Highlight page ``index`` without emitting :attr:`page_selected`."""
        if not 0 <= index < self.count() or index == self.currentRow():
            return
        self._updating = True
        try:
            self.setCurrentRow(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            self.scrollToItem(self.item(index))
        finally:
            self._updating = False

    def select_pages(self, indices: Iterable[int], current: int) -> None:
        """Select ``indices`` with ``current`` as current item, without emitting signals."""
        if not 0 <= current < self.count():
            return
        self._updating = True
        try:
            self.setCurrentRow(current, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            for i in indices:
                item = self.item(i)
                if item is not None:
                    item.setSelected(True)
            self.scrollToItem(self.item(current))
        finally:
            self._updating = False

    def selected_pages(self) -> list[int]:
        """Sorted indices of all selected pages."""
        return sorted(self.row(item) for item in self.selectedItems())

    @property
    def ready_count(self) -> int:
        """Number of thumbnails successfully rendered for the current document."""
        return len(self._ready)

    @property
    def failed_pages(self) -> dict[int, str]:
        """Pages whose thumbnail failed to render, with the error message."""
        return dict(self._failed)

    def is_rendering(self) -> bool:
        return self._renderer.is_busy

    def shutdown(self) -> None:
        """Stop rendering and forget the document."""
        self._renderer.shutdown()
        self._doc = None

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _make_placeholder() -> QIcon:
        pixmap = QPixmap(int(THUMBNAIL_SIZE * 0.77), THUMBNAIL_SIZE)
        pixmap.fill(QColor(0xE8, 0xE8, 0xE8))
        return QIcon(pixmap)

    @Slot(int, QImage)
    def _on_thumbnail_ready(self, index: int, image: QImage) -> None:
        item = self.item(index)
        if item is None:
            return
        item.setIcon(QIcon(QPixmap.fromImage(image)))
        item.setToolTip(f"Page {index + 1}")
        self._ready.add(index)

    @Slot(int, str)
    def _on_thumbnail_failed(self, index: int, message: str) -> None:
        item = self.item(index)
        if item is None:
            return
        item.setIcon(self._error_icon)
        item.setToolTip(f"Page {index + 1}: cannot render thumbnail\n{message}")
        self._failed[index] = message

    @Slot(int)
    def _on_current_row_changed(self, row: int) -> None:
        if not self._updating and row >= 0:
            self.page_selected.emit(row)

    def _insert_index_at(self, pos: QPoint) -> int:
        item = self.itemAt(pos)
        if item is None:
            return self.count()
        row = self.row(item)
        rect = self.visualItemRect(item)
        return row + 1 if pos.y() > rect.center().y() else row

    # -- drag and drop ------------------------------------------------------

    def dropEvent(self, event: QDropEvent) -> None:
        if event.source() is not self:
            event.ignore()
            return
        insert_before = self._insert_index_at(event.position().toPoint())
        selected = self.selected_pages()
        if len(selected) > 1:
            if block_move_steps(self.count(), selected, insert_before):
                self.pages_moved.emit(selected, insert_before)
        else:
            src = self.currentRow()
            if src >= 0:
                dst = move_destination(src, insert_before)
                if dst != src:
                    self.page_moved.emit(src, dst)
        # Ignore so Qt neither moves nor removes any item; the owner rebuilds us.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.ignore()
