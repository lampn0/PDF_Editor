"""View that displays one rendered PDF page at a time.

Coordinate systems (all in PDF points, y pointing down):

* **page coordinates** - the *unrotated*, CropBox-relative space used by
  ``core.text`` (span bboxes, ``find_span_at``). :attr:`PageView.page_clicked`
  reports these.
* **scene coordinates** - the *displayed* (rotated) page, origin at its
  top-left; ``PageGeometry.to_display`` maps page to scene coordinates.

The zoom is a view transform, so scene coordinates do not depend on zoom.
:attr:`PageView.content_layer` is an item group carrying the page-to-scene
transform: children of it are positioned in page coordinates and follow page
rotation and zoom automatically (used for text editing overlays).
"""

from __future__ import annotations

import bisect
import math

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QBrush,
    QColor,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPixmap,
    QShowEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsItemGroup,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)

from pdf_editor.core.document import PdfDocument
from pdf_editor.core.errors import PdfError
from pdf_editor.core.render import PageGeometry, page_geometry, render_page
from pdf_editor.ui.render_worker import rendered_to_qimage

ZOOM_LEVELS: tuple[float, ...] = (
    0.25,
    0.33,
    0.5,
    0.67,
    0.75,
    0.9,
    1.0,
    1.1,
    1.25,
    1.5,
    1.75,
    2.0,
    2.5,
    3.0,
    4.0,
    5.0,
)
MIN_ZOOM = ZOOM_LEVELS[0]
MAX_ZOOM = ZOOM_LEVELS[-1]
MAX_RENDER_BYTES = 64 * 1024 * 1024  # cap for one page bitmap (RGB, 3 bytes/pixel)
ZOOM_RENDER_DELAY_MS = 100
_PAGE_MARGIN_PT = 12.0
_FIT_PADDING_PX = 24


def _geometry_transform(geo: PageGeometry) -> QTransform:
    a, b, c, d, e, f = geo.to_display
    return QTransform(a, b, c, d, e, f)


class PageView(QGraphicsView):
    """Shows the current page of a :class:`PdfDocument`."""

    page_clicked = Signal(int, float, float)  # page index, x, y in page (unrotated) coords
    page_double_clicked = Signal(int, float, float)
    current_page_changed = Signal(int)
    zoom_changed = Signal(float)
    render_failed = Signal(str)
    page_rendered = Signal(int)  # page index; emitted after every (re-)render
    page_hovered = Signal(int, float, float)  # mouse over the page, page (unrotated) coords
    page_hover_left = Signal()  # mouse left the page or the view

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._doc: PdfDocument | None = None
        self._page = 0
        self._zoom = 1.0
        self._geometry: PageGeometry | None = None
        self._page_transform = QTransform()
        self._rendered_scale = 0.0
        self._screen_hooked = False

        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QBrush(QColor(0x60, 0x60, 0x60)))
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._item.setShapeMode(QGraphicsPixmapItem.ShapeMode.BoundingRectShape)
        self._scene.addItem(self._item)
        self._content = QGraphicsItemGroup()
        self._content.setHandlesChildEvents(False)
        self._content.setZValue(1)
        self._scene.addItem(self._content)
        self.setScene(self._scene)

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(ZOOM_RENDER_DELAY_MS)
        self._render_timer.timeout.connect(self._render)

        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)
        self._hovering = False

    # -- document / page ----------------------------------------------------

    @property
    def document(self) -> PdfDocument | None:
        return self._doc

    def set_document(self, doc: PdfDocument | None, page: int = 0) -> None:
        """Show ``doc`` (or clear the view with ``None``) starting at ``page``."""
        self._doc = doc
        self._page = 0
        if doc is None or doc.is_closed or doc.page_count == 0:
            self._clear()
            self.current_page_changed.emit(0)
            return
        self._page = max(0, min(page, doc.page_count - 1))
        self._render()
        self.current_page_changed.emit(self._page)

    @property
    def page_count(self) -> int:
        if self._doc is None or self._doc.is_closed:
            return 0
        return self._doc.page_count

    @property
    def current_page(self) -> int:
        return self._page

    def set_current_page(self, index: int) -> None:
        """Show page ``index`` (clamped to the valid range)."""
        count = self.page_count
        if count == 0:
            return
        index = max(0, min(index, count - 1))
        if index == self._page and self._geometry is not None:
            return
        self._page = index
        self._render()
        self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
        self.current_page_changed.emit(index)

    def next_page(self) -> None:
        self.set_current_page(self._page + 1)

    def previous_page(self) -> None:
        self.set_current_page(self._page - 1)

    def refresh(self, page: int | None = None) -> None:
        """Re-render after the document was modified, optionally switching to ``page``.

        The page index is clamped to the (possibly changed) page count.
        """
        count = self.page_count
        if count == 0:
            self._clear()
            return
        target = self._page if page is None else page
        target = max(0, min(target, count - 1))
        changed = target != self._page
        self._page = target
        self._render()
        if changed:
            self.verticalScrollBar().setValue(self.verticalScrollBar().minimum())
            self.current_page_changed.emit(target)

    # -- zoom ---------------------------------------------------------------

    @property
    def zoom(self) -> float:
        """Current zoom factor (1.0 = 100% = 72 dpi in device-independent pixels)."""
        return self._zoom

    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        """Set the zoom, keeping viewport point ``anchor`` (default: centre) fixed.

        The view is rescaled immediately; the sharp bitmap follows after a
        short delay (see :meth:`flush_render`).
        """
        zoom = max(MIN_ZOOM, min(float(zoom), MAX_ZOOM))
        if abs(zoom - self._zoom) < 1e-6:
            return
        vbar = self.verticalScrollBar()
        # A view parked at the top of the page stays there for menu zooms
        # (Fit Width, Actual Size...), instead of jumping to mid-page.
        keep_top = anchor is None and vbar.value() <= vbar.minimum()
        if anchor is None:
            anchor = QPointF(self.viewport().rect().center())
        scene_anchor = self.viewport_to_scene(anchor)
        self._zoom = zoom
        self.setTransform(QTransform.fromScale(zoom, zoom))
        # Scroll so the scene point under ``anchor`` stays there.
        moved = self.scene_to_viewport(scene_anchor) - anchor
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + round(moved.x()))
        if keep_top:
            vbar.setValue(vbar.minimum())
        else:
            vbar.setValue(vbar.value() + round(moved.y()))
        if self.page_count:
            self._render_timer.start()
        self.zoom_changed.emit(zoom)

    def zoom_in(self, anchor: QPointF | None = None) -> None:
        i = bisect.bisect_right(ZOOM_LEVELS, self._zoom + 1e-6)
        self.set_zoom(ZOOM_LEVELS[min(i, len(ZOOM_LEVELS) - 1)], anchor)

    def zoom_out(self, anchor: QPointF | None = None) -> None:
        i = bisect.bisect_left(ZOOM_LEVELS, self._zoom - 1e-6) - 1
        self.set_zoom(ZOOM_LEVELS[max(i, 0)], anchor)

    def actual_size(self) -> None:
        self.set_zoom(1.0)

    def fit_width(self) -> None:
        """Zoom so the page width fills the viewport width."""
        if self._geometry is None:
            return
        available = self.viewport().width() - _FIT_PADDING_PX
        if available <= 0:
            return
        self.set_zoom(available / (self._geometry.width + 2 * _PAGE_MARGIN_PT))

    @property
    def render_pending(self) -> bool:
        """True while a delayed re-render (after zooming) is scheduled."""
        return self._render_timer.isActive()

    def flush_render(self) -> None:
        """Perform a scheduled re-render now."""
        if self._render_timer.isActive():
            self._render_timer.stop()
            self._render()

    # -- geometry and coordinates -------------------------------------------

    @property
    def geometry_info(self) -> PageGeometry | None:
        """Core geometry of the displayed page (None when nothing is shown)."""
        return self._geometry

    @property
    def page_size_pt(self) -> tuple[float, float]:
        """Displayed (rotated) page size in points; (0, 0) if none."""
        if self._geometry is None:
            return (0.0, 0.0)
        return (self._geometry.width, self._geometry.height)

    @property
    def content_layer(self) -> QGraphicsItemGroup:
        """Item group in page (unrotated) coordinates for overlays."""
        return self._content

    @property
    def page_transform(self) -> QTransform:
        """Transform from page (unrotated) coordinates to scene coordinates."""
        return QTransform(self._page_transform)

    def page_rect_scene(self) -> QRectF:
        """The displayed page's rectangle in scene coordinates."""
        w, h = self.page_size_pt
        return QRectF(0.0, 0.0, w, h)

    def page_to_scene(self, x: float, y: float) -> QPointF:
        """Map page (unrotated) coordinates to scene coordinates."""
        return self._page_transform.map(QPointF(x, y))

    def scene_to_page(self, point: QPointF) -> tuple[float, float]:
        """Map scene coordinates to page (unrotated) coordinates."""
        inverse, ok = self._page_transform.inverted()
        p = inverse.map(point) if ok else QPointF(point)
        return (p.x(), p.y())

    # Explicit aliases matching the core vocabulary.
    unrotated_to_scene = page_to_scene
    scene_to_unrotated = scene_to_page

    def scene_to_viewport(self, point: QPointF) -> QPointF:
        """Float-precision scene -> viewport mapping."""
        return self.viewportTransform().map(point)

    def viewport_to_scene(self, point: QPointF) -> QPointF:
        """Float-precision viewport -> scene mapping."""
        inverse, _ok = self.viewportTransform().inverted()
        return inverse.map(QPointF(point))

    def page_to_viewport(self, x: float, y: float) -> QPointF:
        """Map page (unrotated) coordinates to viewport pixels."""
        return self.scene_to_viewport(self.page_to_scene(x, y))

    def viewport_to_page(self, point: QPointF) -> tuple[float, float]:
        """Map viewport pixels to page (unrotated) coordinates."""
        return self.scene_to_page(self.viewport_to_scene(point))

    # -- inspection ---------------------------------------------------------

    def pixmap(self) -> QPixmap:
        """The bitmap currently displayed (null when nothing is shown)."""
        return self._item.pixmap()

    @property
    def rendered_scale(self) -> float:
        """Device pixels per point of the current bitmap (0 when nothing is shown)."""
        return self._rendered_scale

    # -- internals ----------------------------------------------------------

    def _clear(self) -> None:
        self._render_timer.stop()
        self._item.setPixmap(QPixmap())
        self._geometry = None
        self._rendered_scale = 0.0
        self._page_transform = QTransform()
        self._content.setTransform(self._page_transform)
        self._scene.setSceneRect(QRectF())

    def _device_scale(self, geo: PageGeometry) -> float:
        scale = self._zoom * max(self.devicePixelRatioF(), 1.0)
        area = max(geo.width * geo.height, 1.0)
        cap = math.sqrt(MAX_RENDER_BYTES / 3.0 / area)
        return min(scale, cap)

    @Slot()
    def _render(self) -> None:
        self._render_timer.stop()
        doc = self._doc
        if doc is None or doc.is_closed or self.page_count == 0:
            self._clear()
            return
        try:
            geo = page_geometry(doc, self._page)
            scale = self._device_scale(geo)
            rendered = render_page(doc, self._page, scale)
        except PdfError as exc:
            self._clear()
            self.render_failed.emit(str(exc))
            return
        self._geometry = geo
        self._rendered_scale = scale
        self._page_transform = _geometry_transform(geo)
        self._content.setTransform(self._page_transform)
        self._item.setPixmap(QPixmap.fromImage(rendered_to_qimage(rendered)))
        # Bitmap pixels -> points; tiny rounding differences are absorbed here.
        self._item.setTransform(
            QTransform.fromScale(geo.width / rendered.width, geo.height / rendered.height)
        )
        m = _PAGE_MARGIN_PT
        self._scene.setSceneRect(QRectF(-m, -m, geo.width + 2 * m, geo.height + 2 * m))
        self.page_rendered.emit(self._page)

    def _page_point(self, event: QMouseEvent) -> tuple[float, float] | None:
        if self._geometry is None:
            return None
        scene_pt = self.viewport_to_scene(event.position())
        if not self.page_rect_scene().contains(scene_pt):
            return None
        return self.scene_to_page(scene_pt)

    @Slot()
    def _on_screen_changed(self) -> None:
        if self.page_count:
            self._render()

    # -- events -------------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        handle = self.window().windowHandle()
        if handle is not None and not self._screen_hooked:
            handle.screenChanged.connect(self._on_screen_changed)
            self._screen_hooked = True

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.DevicePixelRatioChange:
            self._on_screen_changed()
        return super().event(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        event.ignore()
        super().mousePressEvent(event)
        if event.isAccepted():
            return  # a scene item (e.g. the text editor) handled the click
        if event.button() == Qt.MouseButton.LeftButton:
            pt = self._page_point(event)
            if pt is not None:
                self.page_clicked.emit(self._page, pt[0], pt[1])

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        event.ignore()
        super().mouseDoubleClickEvent(event)
        if event.isAccepted():
            return
        if event.button() == Qt.MouseButton.LeftButton:
            pt = self._page_point(event)
            if pt is not None:
                self.page_double_clicked.emit(self._page, pt[0], pt[1])

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        super().mouseMoveEvent(event)
        pt = self._page_point(event)
        if pt is not None:
            self._hovering = True
            self.page_hovered.emit(self._page, pt[0], pt[1])
        elif self._hovering:
            self._hovering = False
            self.page_hover_left.emit()

    def viewportEvent(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Leave and self._hovering:
            self._hovering = False
            self.page_hover_left.emit()
        return super().viewportEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in(event.position())
            elif delta < 0:
                self.zoom_out(event.position())
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if key == Qt.Key.Key_PageDown:
            self.next_page()
        elif key == Qt.Key.Key_PageUp:
            self.previous_page()
        elif key == Qt.Key.Key_Home and ctrl:
            self.set_current_page(0)
        elif key == Qt.Key.Key_End and ctrl:
            self.set_current_page(self.page_count - 1)
        else:
            super().keyPressEvent(event)
            return
        event.accept()
