"""Smoke tests for the core package."""

import importlib
import subprocess
import sys

CORE_MODULES = ["document", "render", "text", "fonts", "pages", "history", "errors"]


def test_core_modules_import() -> None:
    for name in CORE_MODULES:
        importlib.import_module(f"pdf_editor.core.{name}")


def test_core_does_not_import_pyside6() -> None:
    code = (
        "import sys, pdf_editor.core\n"
        f"for m in {CORE_MODULES!r}: __import__('pdf_editor.core.' + m)\n"
        "sys.exit(1 if any(k.startswith('PySide6') for k in sys.modules) else 0)\n"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_pymupdf_opens_sample(data_dir) -> None:
    import pymupdf

    with pymupdf.open(data_dir / "Thiet_ke_VLSI_Exercise.pdf") as doc:
        assert doc.page_count > 0
