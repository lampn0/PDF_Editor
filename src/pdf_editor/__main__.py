"""Entry point for ``python -m pdf_editor`` and the ``pdf-editor`` script.

Smoke-test exit codes: 0 loaded and all thumbnails rendered, 1 open failed,
2 timeout, 3 some thumbnails failed, 4 an unhandled exception occurred.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import QStandardPaths, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

SMOKE_TIMEOUT_MS = 30_000
_SMOKE_POLL_MS = 50
EXIT_OPEN_FAILED = 1
EXIT_TIMEOUT = 2
EXIT_THUMBNAILS_FAILED = 3
EXIT_UNHANDLED_EXCEPTION = 4

_log = logging.getLogger("pdf_editor")


def _parse_args(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(prog="pdf_editor", description="Desktop PDF editor.")
    parser.add_argument("path", nargs="?", help="PDF file to open")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="quit once the file is loaded and thumbnails are rendered (exit code 0 on success)",
    )
    args, unknown = parser.parse_known_args(list(argv))
    return args, unknown


def _install_excepthook(app: QApplication, smoke: bool, fired: list[bool]) -> None:
    """Log unhandled exceptions (raised in Qt slots) instead of losing them."""

    def hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        fired.append(True)
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        _log.error("Unhandled exception:\n%s", text)
        if smoke:
            app.exit(EXIT_UNHANDLED_EXCEPTION)
            return
        QMessageBox.critical(None, "PDF Editor", f"Unexpected error:\n{exc}")

    sys.excepthook = hook


def _setup_logging() -> None:
    """Log to stderr, or to a file when there is none (``pythonw`` / GUI script)."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if sys.stderr is not None:
        logging.basicConfig(level=logging.INFO, format=fmt)
        return
    folder = Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    )
    try:
        folder.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO, format=fmt, filename=folder / "pdf_editor.log", encoding="utf-8"
        )
    except OSError:
        logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()])


def main(argv: Sequence[str] | None = None) -> int:
    """Create the application and main window, open ``path`` if given, run the loop."""
    from pdf_editor.ui.main_window import MainWindow

    if argv is None:
        argv = sys.argv[1:]
    args, qt_args = _parse_args(argv)

    existing = QApplication.instance()
    app = existing or QApplication([sys.argv[0] if sys.argv else "pdf_editor", *qt_args])
    app.setApplicationName("PDF Editor")
    _setup_logging()
    fired: list[bool] = []
    previous_hook = sys.excepthook
    _install_excepthook(app, args.smoke_test, fired)

    window = MainWindow()
    window.show()

    if args.smoke_test:
        # Armed before opening so a modal error dialog cannot hang the run.
        QTimer.singleShot(SMOKE_TIMEOUT_MS, lambda: app.exit(EXIT_TIMEOUT))

    opened = window.open_path(args.path) if args.path else False

    if args.smoke_test:
        if args.path and not opened:
            QTimer.singleShot(0, lambda: app.exit(EXIT_OPEN_FAILED))
        else:
            poll = QTimer(window)

            def check() -> None:
                if window.thumbnails.is_rendering():
                    return
                poll.stop()
                doc = window.document
                expected = doc.page_count if doc is not None else 0
                ok = window.thumbnails.ready_count == expected
                app.exit(0 if ok else EXIT_THUMBNAILS_FAILED)

            poll.timeout.connect(check)
            poll.start(_SMOKE_POLL_MS)

    try:
        code = app.exec()
    finally:
        window.shutdown()
        sys.excepthook = previous_hook
    if fired and code == 0:
        code = EXIT_UNHANDLED_EXCEPTION
    return code


if __name__ == "__main__":
    sys.exit(main())
