"""UI test configuration: run Qt headless at a fixed scale, plus shared helpers."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QT_SCALE_FACTOR"] = "1"

import pytest  # noqa: E402

WAIT_MS = 20_000


@pytest.fixture
def make_window(qtbot):
    """Factory for shown MainWindows that are torn down without save prompts.

    Windows are not registered with ``qtbot.addWidget``: pytest-qt would close()
    them before this fixture's teardown, which could pop a real save prompt.
    """
    from PySide6.QtCore import QCoreApplication, QEvent

    from pdf_editor.ui.main_window import MainWindow

    windows: list[MainWindow] = []

    def factory() -> MainWindow:
        win = MainWindow()
        win.show()
        qtbot.waitExposed(win)
        windows.append(win)
        return win

    yield factory
    for win in windows:
        win.text_overlay.cancel()  # pending editor text would otherwise dirty the doc
        if win.document is not None:
            win.document.mark_clean()
        win.close()
        win.shutdown()
        win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
