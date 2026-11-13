"""In-place text editing on a :class:`PageView`.

:class:`TextEditOverlay` implements the UI half of "Edit Text" mode:

* hover highlights the span under the cursor (a rectangle in the page view's
  ``content_layer``, i.e. unrotated page coordinates) and shows an I-beam;
* a click on a span opens a ``QLineEdit`` (inside a ``QGraphicsProxyWidget``)
  over the span's *displayed* box, so it is placed and scaled correctly at
  any zoom and page rotation; spans that appear vertical on screen get a
  horizontal editor anchored at the span's top-left;
* Enter commits, Esc cancels. Losing focus to a mouse click or Tab commits;
  losing focus because the window was deactivated or a popup opened keeps
  the editor open. Zooming, changing page and any document change cancel.

Committing only emits :attr:`TextEditOverlay.commit_requested`; the owner
(``MainWindow``) applies the edit through the undo history. Commits and
click-to-open are deferred to the next event-loop iteration so that the
document is never changed from inside the editor's own event handlers.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFocusEvent, QFont, QFontMetricsF, QKeyEvent, QPen
from PySide6.QtWidgets import QGraphicsProxyWidget, QGraphicsRectItem, QLineEdit

from pdf_editor.core.errors import PdfError
from pdf_editor.core.text import TextSpan, get_spans
from pdf_editor.ui.page_view import PageView

_MIN_EDITOR_WIDTH_PT = 40.0
_EDITOR_EXTRA_WIDTH_PT = 24.0


class SpanLineEdit(QLineEdit):
    """Line edit that reports Esc and the reason it lost focus."""

    escape_pressed = Signal()
    focus_lost = Signal(object)  # Qt.FocusReason

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.escape_pressed.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self.focus_lost.emit(event.reason())


def span_at(spans: list[TextSpan], x: float, y: float) -> TextSpan | None:
    """Smallest span whose bbox contains (x, y) (same rule as ``core.text.find_span_at``)."""
    hits = [s for s in spans if s.bbox[0] <= x <= s.bbox[2] and s.bbox[1] <= y <= s.bbox[3]]
    if not hits:
        return None
    return min(hits, key=lambda s: (s.bbox[2] - s.bbox[0]) * (s.bbox[3] - s.bbox[1]))


class TextEditOverlay(QObject):
    """Hover highlight and in-place span editor for a :class:`PageView`."""

    commit_requested = Signal(object, str)  # TextSpan, new text (only when changed)
    editing_started = Signal(object)  # TextSpan
    editing_finished = Signal()  # editor closed (committed or cancelled)
    no_text_on_page = Signal(int)  # page index without any editable span

    def __init__(self, view: PageView) -> None:
        super().__init__(view)
        self._view = view
        self._enabled = False
        self._spans_page: int | None = None
        self._spans: list[TextSpan] = []
        self._span: TextSpan | None = None  # span being edited
        self._open_token = 0

        self._highlight = QGraphicsRectItem()
        pen = QPen(QColor(0x21, 0x6F, 0xD8))
        pen.setCosmetic(True)
        pen.setWidthF(1.5)
        self._highlight.setPen(pen)
        self._highlight.setBrush(QBrush(QColor(0x21, 0x6F, 0xD8, 40)))
        self._highlight.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._highlight.setParentItem(view.content_layer)
        self._highlight.hide()

        self._editor = SpanLineEdit()
        self._editor.setFrame(False)
        self._editor.setStyleSheet(
            "QLineEdit { background: #ffffff; color: #000000; border: 1px solid #216fd8;"
            " padding: 0px; margin: 0px; }"
        )
        self._proxy = QGraphicsProxyWidget()
        self._proxy.setWidget(self._editor)
        self._proxy.setZValue(10)
        self._proxy.hide()
        view.scene().addItem(self._proxy)

        self._editor.returnPressed.connect(self.commit)
        self._editor.escape_pressed.connect(self.cancel)
        self._editor.focus_lost.connect(self._on_focus_lost)

        view.page_hovered.connect(self._on_hover)
        view.page_hover_left.connect(self._hide_highlight)
        view.page_clicked.connect(self._on_click)
        view.page_double_clicked.connect(self._on_click)
        view.page_rendered.connect(self._on_page_rendered)
        view.zoom_changed.connect(self.cancel)
        view.current_page_changed.connect(self._on_page_changed)

    # -- public API ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        """Turn Edit Text mode on or off (off cancels an open editor)."""
        if enabled == self._enabled:
            return
        self._enabled = enabled
        if not enabled:
            self.cancel()
            self._hide_highlight()
        else:
            self._check_page_has_text()

    @property
    def editor(self) -> SpanLineEdit:
        """The line edit widget (visible only while editing)."""
        return self._editor

    @property
    def editor_proxy(self) -> QGraphicsProxyWidget:
        return self._proxy

    @property
    def is_editing(self) -> bool:
        return self._span is not None

    @property
    def editing_span(self) -> TextSpan | None:
        return self._span

    def spans(self) -> list[TextSpan]:
        """Spans of the current page (cached until the next re-render)."""
        page = self._view.current_page
        doc = self._view.document
        if doc is None or doc.is_closed or self._view.page_count == 0:
            return []
        if self._spans_page != page:
            try:
                self._spans = get_spans(doc, page)
            except PdfError:
                self._spans = []
            self._spans_page = page
        return self._spans

    def span_at(self, x: float, y: float) -> TextSpan | None:
        """Span under page (unrotated) coordinates on the current page."""
        return span_at(self.spans(), x, y)

    def open_at(self, page: int, x: float, y: float) -> bool:
        """Open the editor on the span at (x, y) of ``page`` now. Returns success."""
        if page != self._view.current_page:
            return False
        span = self.span_at(x, y)
        if span is None:
            return False
        self.open_editor(span)
        return True

    def open_editor(self, span: TextSpan) -> None:
        """Show the editor over ``span`` (a span of the current page)."""
        self.cancel()
        self._span = span
        self._place_editor(span)
        self._editor.setText(span.text)
        self._proxy.show()
        self._view.setFocus(Qt.FocusReason.OtherFocusReason)
        self._proxy.setFocus(Qt.FocusReason.OtherFocusReason)
        self._editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self._editor.selectAll()
        self._hide_highlight()
        self.editing_started.emit(span)

    @Slot()
    def commit(self) -> None:
        """Close the editor and request the edit if the text changed (deferred)."""
        span = self._span
        if span is None:
            return
        text = self._editor.text()
        self._close()
        if text != span.text:
            doc = self._view.document

            def emit() -> None:
                # Drop the edit if the document was replaced or closed meanwhile.
                if self._view.document is doc and doc is not None and not doc.is_closed:
                    self.commit_requested.emit(span, text)

            QTimer.singleShot(0, emit)

    def take_pending(self) -> tuple[TextSpan, str] | None:
        """Close the editor synchronously; return (span, text) if the text changed.

        Used before save/close/open so typed text is not lost.
        """
        span = self._span
        if span is None:
            return None
        text = self._editor.text()
        self._close()
        return (span, text) if text != span.text else None

    @Slot()
    def cancel(self) -> None:
        """Close the editor without changing anything."""
        if self._span is not None:
            self._close()

    # -- internals ----------------------------------------------------------

    def _close(self) -> None:
        self._span = None
        self._proxy.hide()
        self._editor.clearFocus()
        if self._view.isVisible():
            self._view.setFocus(Qt.FocusReason.OtherFocusReason)
        self.editing_finished.emit()

    def _place_editor(self, span: TextSpan) -> None:
        x0, y0, x1, y1 = span.bbox
        rect = self._view.page_transform.mapRect(QRectF(x0, y0, x1 - x0, y1 - y0))
        font = QFont(self._editor.font())
        font.setPixelSize(max(1, round(span.size)))
        self._editor.setFont(font)
        metrics = QFontMetricsF(font)
        upright_on_screen = rect.width() >= rect.height() or len(span.text) <= 1
        line_height = max(metrics.height() + 2.0, 1.0)
        if upright_on_screen:
            height = max(rect.height(), line_height)
            width = rect.width()
            top = rect.center().y() - height / 2
        else:
            height = line_height
            width = rect.height()  # text length on screen
            top = rect.top()
        width = max(width + _EDITOR_EXTRA_WIDTH_PT, _MIN_EDITOR_WIDTH_PT)
        self._editor.setFixedSize(round(width), round(height))
        self._proxy.setPos(rect.left(), top)

    def _show_highlight(self, span: TextSpan) -> None:
        x0, y0, x1, y1 = span.bbox
        self._highlight.setRect(QRectF(x0, y0, x1 - x0, y1 - y0))
        self._highlight.show()
        self._view.viewport().setCursor(Qt.CursorShape.IBeamCursor)

    @Slot()
    def _hide_highlight(self) -> None:
        self._highlight.hide()
        self._view.viewport().unsetCursor()

    def _check_page_has_text(self) -> None:
        if self._enabled and self._view.page_count and not self.spans():
            self.no_text_on_page.emit(self._view.current_page)

    @Slot(int, float, float)
    def _on_hover(self, page: int, x: float, y: float) -> None:
        if not self._enabled or self._span is not None:
            return
        span = self.span_at(x, y)
        if span is None:
            self._hide_highlight()
        else:
            self._show_highlight(span)

    @Slot(int, float, float)
    def _on_click(self, page: int, x: float, y: float) -> None:
        if not self._enabled:
            return
        # Deferred: a commit triggered by this same click (focus loss) runs first.
        self._open_token += 1
        token = self._open_token
        QTimer.singleShot(0, lambda: self._open_deferred(token, page, x, y))

    def _open_deferred(self, token: int, page: int, x: float, y: float) -> None:
        if token == self._open_token and self._enabled and self._span is None:
            self.open_at(page, x, y)

    @Slot(object)
    def _on_focus_lost(self, reason: Qt.FocusReason) -> None:
        if self._span is None:
            return
        if reason in (
            Qt.FocusReason.MouseFocusReason,
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
        ):
            self.commit()

    @Slot(int)
    def _on_page_rendered(self, _page: int) -> None:
        self._spans_page = None  # content or geometry may have changed
        self._hide_highlight()

    @Slot(int)
    def _on_page_changed(self, _page: int) -> None:
        self.cancel()
        self._spans_page = None
        self._hide_highlight()
        self._check_page_has_text()
