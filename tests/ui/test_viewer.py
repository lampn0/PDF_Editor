"""Tests for the T5 viewer shell (main window, page view, thumbnails)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox

from pdf_editor.core.text import find_span_at, get_spans
from pdf_editor.ui.main_window import MainWindow
from pdf_editor.ui.thumbnail_panel import block_move_steps, move_destination

SAMPLE = "Thiet_ke_VLSI_Exercise.pdf"
WAIT_MS = 20_000
ROOT = Path(__file__).resolve().parents[2]
UI_SOURCES = sorted((ROOT / "src" / "pdf_editor" / "ui").glob("*.py")) + [
    ROOT / "src" / "pdf_editor" / "__main__.py"
]


@pytest.fixture
def sample(data_dir: Path) -> Path:
    return data_dir / SAMPLE


@pytest.fixture
def window(make_window) -> MainWindow:
    return make_window()


@pytest.fixture
def opened(window: MainWindow, sample: Path, qtbot) -> MainWindow:
    assert window.open_path(sample)
    qtbot.waitUntil(
        lambda: window.thumbnails.ready_count == window.document.page_count, timeout=WAIT_MS
    )
    return window


def test_open_sample_thumbnails_match_page_count(opened: MainWindow) -> None:
    count = opened.document.page_count
    assert count > 1
    assert opened.thumbnails.count() == count
    assert opened.thumbnails.ready_count == count
    assert not opened.thumbnails.is_rendering()
    assert opened.current_page == 0
    assert not opened.page_view.pixmap().isNull()


def test_title_contains_filename_and_modified_flag(opened: MainWindow, sample: Path) -> None:
    assert sample.name in opened.windowTitle()
    assert opened.windowTitle().endswith("[*] - PDF Editor")
    assert not opened.isWindowModified()
    opened.document.mark_dirty()
    opened.refresh()
    assert opened.isWindowModified()


def test_selecting_thumbnail_changes_current_page(opened: MainWindow, qtbot) -> None:
    thumbs = opened.thumbnails
    with qtbot.waitSignal(thumbs.page_selected) as blocker:
        thumbs.setCurrentRow(2)
    assert blocker.args == [2]
    assert opened.current_page == 2

    item = thumbs.item(1)
    thumbs.scrollToItem(item)
    rect = thumbs.visualItemRect(item)
    qtbot.mouseClick(thumbs.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert opened.current_page == 1
    assert "Page 2 /" in opened._page_label.text()


def test_page_navigation_updates_thumbnail(opened: MainWindow, qtbot) -> None:
    view = opened.page_view
    view.setFocus()
    qtbot.keyClick(view, Qt.Key.Key_PageDown)
    assert opened.current_page == 1
    assert opened.thumbnails.currentRow() == 1
    qtbot.keyClick(view, Qt.Key.Key_PageUp)
    qtbot.keyClick(view, Qt.Key.Key_PageUp)  # clamped at first page
    assert opened.current_page == 0
    opened.set_current_page(99)
    assert opened.current_page == opened.document.page_count - 1


def test_zoom_in_renders_bigger_pixmap(opened: MainWindow, qtbot) -> None:
    view = opened.page_view
    before = view.pixmap().size()
    zoom_before = view.zoom
    opened.zoom_in_action.trigger()
    assert view.zoom > zoom_before
    assert view.render_pending  # debounced re-render
    qtbot.waitUntil(lambda: not view.render_pending, timeout=WAIT_MS)
    after = view.pixmap().size()
    assert after.width() > before.width()
    assert after.height() > before.height()
    opened.zoom_out_action.trigger()
    view.flush_render()
    assert view.zoom == pytest.approx(zoom_before)
    assert view.pixmap().size() == before
    opened.actual_size_action.trigger()
    assert view.zoom == pytest.approx(1.0)
    opened.fit_width_action.trigger()
    assert view.zoom > 0


def test_render_size_is_capped(opened: MainWindow, monkeypatch) -> None:
    import pdf_editor.ui.page_view as page_view_module

    monkeypatch.setattr(page_view_module, "MAX_RENDER_BYTES", 1_000_000)
    view = opened.page_view
    view.set_zoom(5.0)
    view.flush_render()
    pm = view.pixmap()
    assert pm.width() * pm.height() * 3 <= 1_000_000 * 1.01
    # The page still occupies its full zoomed size on screen.
    width, _ = view.page_size_pt
    assert view.mapFromScene(view.page_rect_scene()).boundingRect().width() >= width * 5 - 2


def test_page_size_from_geometry(opened: MainWindow) -> None:
    view = opened.page_view
    geo = view.geometry_info
    assert geo is not None
    width, height = view.page_size_pt
    assert (width, height) == (geo.width, geo.height)
    view.set_zoom(2.0)
    view.flush_render()
    assert view.page_size_pt == (width, height)


def test_coordinate_mapping_round_trip(opened: MainWindow) -> None:
    view = opened.page_view
    view.set_zoom(2.0)
    scene = view.page_to_scene(100.25, 200.75)
    assert view.scene_to_page(scene) == pytest.approx((100.25, 200.75))
    vp = view.page_to_viewport(100.25, 200.75)
    assert view.viewport_to_page(vp) == pytest.approx((100.25, 200.75), abs=1e-6)
    assert view.unrotated_to_scene(1.0, 2.0) == view.page_to_scene(1.0, 2.0)


def test_ctrl_wheel_zoom_keeps_point_under_cursor(opened: MainWindow) -> None:
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    view = opened.page_view
    # Start zoomed in so both axes can scroll (a page narrower than the viewport is
    # always centred, so it cannot be anchored).
    view.set_zoom(2.0)
    view.flush_render()
    for bar in (view.horizontalScrollBar(), view.verticalScrollBar()):
        bar.setValue((bar.minimum() + bar.maximum()) // 2)
    pos = QPointF(view.viewport().width() * 0.3, view.viewport().height() * 0.4)
    before = view.viewport_to_page(pos)
    event = QWheelEvent(
        pos,
        view.viewport().mapToGlobal(pos),
        QPoint(),
        QPoint(0, 120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    view.wheelEvent(event)
    assert view.zoom > 2.0
    after = view.viewport_to_page(pos)
    assert after == pytest.approx(before, abs=1.5 / view.zoom)


def test_page_click_emits_page_coordinates(opened: MainWindow, qtbot) -> None:
    view = opened.page_view
    vp = view.page_to_viewport(50.0, 60.0).toPoint()
    with qtbot.waitSignal(view.page_clicked) as blocker:
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp)
    page, x, y = blocker.args
    assert page == 0
    assert x == pytest.approx(50.0, abs=1.0 / view.zoom)
    assert y == pytest.approx(60.0, abs=1.0 / view.zoom)


def _text_pdf(path: Path, rotation: int, crop: tuple[float, ...] | None) -> Path:
    pymupdf = pytest.importorskip("pymupdf")
    raw = pymupdf.open()
    page = raw.new_page(width=595, height=842)
    page.insert_text((150, 250), "TARGET", fontsize=24)
    page.insert_text((150, 500), "OTHER", fontsize=24)
    if crop:
        page.set_cropbox(pymupdf.Rect(*crop))
    page.set_rotation(rotation)
    raw.save(path)
    raw.close()
    return path


@pytest.mark.parametrize(
    ("rotation", "crop"),
    [(0, None), (90, None), (180, None), (270, None), (90, (40, 60, 500, 760))],
)
def test_click_on_displayed_span_hits_it(
    window: MainWindow, qtbot, tmp_path: Path, rotation: int, crop
) -> None:
    assert window.open_path(_text_pdf(tmp_path / "rot.pdf", rotation, crop))
    view = window.page_view
    doc = window.document
    span = next(s for s in get_spans(doc, 0) if s.text == "TARGET")
    cx = (span.bbox[0] + span.bbox[2]) / 2
    cy = (span.bbox[1] + span.bbox[3]) / 2
    vp = view.page_to_viewport(cx, cy)
    if not view.viewport().rect().contains(vp.toPoint()):
        view.centerOn(view.page_to_scene(cx, cy))
        vp = view.page_to_viewport(cx, cy)
    assert view.viewport().rect().contains(vp.toPoint())
    with qtbot.waitSignal(view.page_clicked) as blocker:
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=vp.toPoint())
    _page, x, y = blocker.args
    hit = find_span_at(doc, 0, x, y)
    assert hit is not None and hit.text == "TARGET"
    # The displayed bitmap has ink around that viewport position.
    item_pt = view._item.mapFromScene(view.viewport_to_scene(vp))
    image = view.pixmap().toImage()
    ix, iy = int(item_pt.x()), int(item_pt.y())
    values = [
        image.pixelColor(ix + dx, iy + dy).value()
        for dx in range(-8, 9)
        for dy in range(-8, 9)
        if image.valid(ix + dx, iy + dy)
    ]
    assert values and min(values) < 128


def test_content_layer_follows_page_transform(window: MainWindow, tmp_path: Path) -> None:
    from PySide6.QtWidgets import QGraphicsRectItem

    assert window.open_path(_text_pdf(tmp_path / "rot.pdf", 90, None))
    view = window.page_view
    span = next(s for s in get_spans(window.document, 0) if s.text == "TARGET")
    rect = QGraphicsRectItem(span.bbox[0], span.bbox[1], span.width, span.bbox[3] - span.bbox[1])
    rect.setParentItem(view.content_layer)
    scene_rect = rect.sceneBoundingRect()
    expected = view.page_transform.mapRect(rect.rect())
    assert scene_rect.center().x() == pytest.approx(expected.center().x(), abs=1.0)
    assert scene_rect.center().y() == pytest.approx(expected.center().y(), abs=1.0)
    # Rotated by 90 degrees: the span is taller than wide on screen.
    assert scene_rect.height() > scene_rect.width()


def test_open_invalid_file_shows_error(window: MainWindow, tmp_path: Path, monkeypatch) -> None:
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"this is not a pdf")
    calls: list[str] = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: calls.append(a[2])))
    assert window.open_path(bad) is False
    assert window.open_path(tmp_path / "missing.pdf") is False
    assert len(calls) == 2
    assert window.document is None
    assert window.windowTitle() == "PDF Editor"


def test_password_prompt_retries(window: MainWindow, tmp_path: Path, monkeypatch, qtbot) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    path = tmp_path / "secret.pdf"
    src = pymupdf.open()
    src.new_page()
    src.new_page()
    src.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="owner")
    src.close()

    answers = iter([("wrong", True), ("pw", True)])
    prompts: list[str] = []

    def fake_get_text(*args, **kwargs):
        prompts.append(args[2])
        return next(answers)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_get_text))
    assert window.open_path(path) is True
    assert len(prompts) == 2
    assert window.document.page_count == 2
    qtbot.waitUntil(lambda: window.thumbnails.ready_count == 2, timeout=WAIT_MS)

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))
    window.close_document()
    assert window.open_path(path) is False
    assert window.document is None


def test_dirty_close_prompts(opened: MainWindow, monkeypatch) -> None:
    answers: list[QMessageBox.StandardButton] = []

    def fake_warning(*args, **kwargs):
        answers.append(QMessageBox.StandardButton.Cancel)
        return answers[-1]

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake_warning))
    opened.document.mark_dirty()
    assert opened.close() is False
    assert len(answers) == 1
    assert opened.isVisible()
    assert opened.document is not None

    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard)
    )
    assert opened.close() is True
    assert not opened.isVisible()


def test_clean_close_does_not_prompt(opened: MainWindow, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("should not prompt")

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fail))
    assert opened.close() is True


def test_refresh_after_external_change(opened: MainWindow, qtbot) -> None:
    opened.set_current_page(1)
    opened.refresh()
    qtbot.waitUntil(
        lambda: opened.thumbnails.ready_count == opened.document.page_count, timeout=WAIT_MS
    )
    assert opened.current_page == 1
    assert opened.thumbnails.currentRow() == 1


def test_thumbnail_multi_select(opened: MainWindow) -> None:
    thumbs = opened.thumbnails
    thumbs.item(0).setSelected(True)
    thumbs.item(2).setSelected(True)
    assert 0 in thumbs.selected_pages() and 2 in thumbs.selected_pages()


@pytest.mark.parametrize(
    ("src", "insert_before", "expected"),
    [(0, 3, 2), (3, 0, 0), (2, 2, 2), (2, 3, 2), (1, 5, 4)],
)
def test_move_destination(src: int, insert_before: int, expected: int) -> None:
    assert move_destination(src, insert_before) == expected


def test_ui_does_not_import_pymupdf() -> None:
    pattern = re.compile(r"^\s*(?:import|from)\s+(?:pymupdf|fitz)\b", re.MULTILINE)
    offenders = [p.name for p in UI_SOURCES if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_smoke_launch(sample: Path) -> None:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    result = subprocess.run(
        [sys.executable, "-m", "pdf_editor", str(sample), "--smoke-test"],
        env=env,
        capture_output=True,
        timeout=60,
        cwd=ROOT,
    )
    stderr = result.stderr.decode(errors="replace")
    assert result.returncode == 0, stderr
    assert "Traceback" not in stderr


@pytest.mark.parametrize(
    ("count", "block", "insert_before", "order"),
    [
        (5, [1, 3], 0, [1, 3, 0, 2, 4]),
        (5, [0, 1], 5, [2, 3, 4, 0, 1]),
        (5, [0, 2], 4, [1, 3, 0, 2, 4]),
        (4, [1, 2], 2, [0, 1, 2, 3]),
    ],
)
def test_block_move_steps(count: int, block, insert_before: int, order) -> None:
    current = list(range(count))
    for src, dst in block_move_steps(count, block, insert_before):
        current.insert(dst, current.pop(src))
    assert current == order


def test_menu_zoom_keeps_top_of_page_visible(opened: MainWindow) -> None:
    view = opened.page_view
    vbar = view.verticalScrollBar()
    assert vbar.value() == vbar.minimum()
    opened.zoom_in_action.trigger()
    opened.zoom_in_action.trigger()
    view.flush_render()
    assert vbar.maximum() > vbar.minimum()  # page now taller than viewport
    assert vbar.value() == vbar.minimum()
    vbar.setValue((vbar.minimum() + vbar.maximum()) // 2)
    mid = vbar.value()
    opened.zoom_out_action.trigger()
    assert vbar.value() != vbar.minimum() or mid == vbar.minimum()
