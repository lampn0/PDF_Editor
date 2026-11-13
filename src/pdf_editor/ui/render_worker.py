"""Incremental thumbnail rendering on the GUI thread.

MuPDF uses one global context and is not thread-safe, even across separate
documents, so all rendering happens on the GUI thread. To keep the UI
responsive, :class:`ThumbnailRenderer` renders a few pages per event-loop
tick (0 ms ``QTimer``). Every request bumps a generation counter; pending work
of an older request is dropped.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QImage

from pdf_editor.core.document import PdfDocument
from pdf_editor.core.errors import PdfError
from pdf_editor.core.render import RenderedPage, render_thumbnail

PAGES_PER_TICK = 2


def rendered_to_qimage(rendered: RenderedPage) -> QImage:
    """Convert a core ``RenderedPage`` to a standalone (deep-copied) ``QImage``."""
    fmt = QImage.Format.Format_RGB888 if rendered.rgb else QImage.Format.Format_RGBA8888
    image = QImage(rendered.samples, rendered.width, rendered.height, rendered.stride, fmt)
    # The QImage above only wraps ``samples``; copy() detaches it from the buffer.
    return image.copy()


class ThumbnailRenderer(QObject):
    """Renders thumbnails of a document a few pages per event-loop tick.

    The document must not be modified between ticks without a new
    :meth:`request` (or :meth:`cancel`); the owner re-requests after every edit.
    """

    thumbnail_ready = Signal(int, QImage)  # page index, image (device pixel ratio set)
    thumbnail_failed = Signal(int, str)  # page index, message
    finished = Signal()  # the current request has completed

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._generation = 0
        self._doc: PdfDocument | None = None
        self._queue: deque[int] = deque()
        self._max_size = 1
        self._dpr = 1.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(False)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._tick)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def is_busy(self) -> bool:
        """True while pages of the current request are still queued."""
        return bool(self._queue)

    def request(
        self,
        doc: PdfDocument,
        indices: Iterable[int],
        max_size: int,
        device_pixel_ratio: float = 1.0,
    ) -> int:
        """Queue ``indices`` of ``doc``, cancelling earlier work; returns the generation.

        Thumbnails are rendered at ``max_size * device_pixel_ratio`` pixels and
        tagged with that ratio so they look sharp on HiDPI screens.
        """
        self.cancel()
        self._doc = doc
        self._queue = deque(indices)
        self._max_size = max_size
        self._dpr = max(float(device_pixel_ratio), 1.0)
        if self._queue:
            self._timer.start()
        return self._generation

    def cancel(self) -> None:
        """Drop all pending work."""
        self._generation += 1
        self._queue.clear()
        self._doc = None
        self._timer.stop()

    def shutdown(self) -> None:
        """Stop rendering (kept for API symmetry; nothing runs off-thread)."""
        self.cancel()

    @Slot()
    def _tick(self) -> None:
        doc = self._doc
        generation = self._generation
        for _ in range(PAGES_PER_TICK):
            if not self._queue or doc is None or generation != self._generation:
                break
            index = self._queue.popleft()
            try:
                if doc.is_closed:
                    raise PdfError("Document is closed")
                size = max(1, round(self._max_size * self._dpr))
                image = rendered_to_qimage(render_thumbnail(doc, index, size))
            except PdfError as exc:
                self.thumbnail_failed.emit(index, str(exc))
                continue
            image.setDevicePixelRatio(self._dpr)
            self.thumbnail_ready.emit(index, image)
        if generation == self._generation and not self._queue:
            self._timer.stop()
            self._doc = None
            self.finished.emit()
