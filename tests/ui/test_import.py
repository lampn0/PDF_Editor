"""Smoke tests for the UI package."""

import importlib

UI_MODULES = ["main_window", "page_view", "thumbnail_panel", "text_edit_overlay", "render_worker"]


def test_ui_modules_import() -> None:
    for name in UI_MODULES:
        importlib.import_module(f"pdf_editor.ui.{name}")


def test_qapplication_available(qapp) -> None:
    assert qapp is not None
