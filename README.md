# PDF Editor

A desktop PDF editor written in Python with PySide6 (Qt) and PyMuPDF.

## Features

- **Viewer**: page view with zoom (Ctrl+wheel, fit width, actual size), page thumbnails,
  keyboard navigation, HiDPI rendering.
- **Edit text in place**: turn on *Edit Text* mode, click a line of text, type, and press
  Enter. The original font is reused when it has the glyphs; otherwise an embedded
  Noto Sans fallback is used and the status bar tells you. Text that no longer fits can
  be shrunk to fit or left overflowing. Works on rotated pages.
- **Page operations**: insert blank pages, insert pages (a chosen page range) from another PDF, replace a page,
  delete, rotate, and reorder pages (menu, thumbnail context menu, or drag and drop;
  multi-selection supported).
- **Undo / redo** for every edit, with a modified marker in the title bar and a save
  prompt on close.
- **Encrypted PDFs**: password prompt on open; saving keeps the original password
  protection (File > Remove Password Protection saves without it).
- Saves are atomic: the file is written to a temporary file and then swapped in.

## Install and run (uv)

Requires Python 3.11 or newer (3.13 recommended).

```powershell
uv venv --python 3.13
uv pip install -e ".[dev]"
uv run python -m pdf_editor              # optionally: path\to\file.pdf
```

### pip alternative

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
python -m pdf_editor path\to\file.pdf
```

After installation the `pdf-editor` command is also available.

## Test and lint

```powershell
uv run pytest                 # or: python -m pytest
uv run ruff check .
uv run ruff format --check .
```

UI tests run headless (`QT_QPA_PLATFORM=offscreen` is set by `tests/ui/conftest.py`).
`python -m pdf_editor file.pdf --smoke-test` opens a file, renders all thumbnails and
exits with code 0 on success.

## Keyboard shortcuts

| Action                       | Shortcut                     |
| ---------------------------- | ---------------------------- |
| Open                         | Ctrl+O                       |
| Save                         | Ctrl+S                       |
| Save As                      | Ctrl+Shift+S                 |
| Close document               | Ctrl+F4, Ctrl+W (platform)   |
| Exit                         | Ctrl+Q                       |
| Undo                         | Ctrl+Z                       |
| Redo                         | Ctrl+Y, Ctrl+Shift+Z         |
| Edit Text mode               | Ctrl+E or T                  |
| Commit / cancel text edit    | Enter / Esc                  |
| Zoom in / out                | Ctrl+= (Ctrl++) / Ctrl+-     |
| Actual size                  | Ctrl+0                       |
| Zoom at cursor               | Ctrl+mouse wheel             |
| Next / previous page         | PageDown / PageUp            |
| First / last page            | Ctrl+Home / Ctrl+End         |
| Delete selected page(s)      | Delete                       |
| Rotate left / right          | Ctrl+L / Ctrl+R              |

## License

GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later); see [LICENSE](LICENSE).
This project depends on PyMuPDF, which is distributed under the AGPL (or a commercial
license from Artifex), so the project as a whole is licensed under the AGPL. If you
distribute the application or offer it over a network, you must make the corresponding
source code available under the same license.

The bundled Noto Sans fonts (`src/pdf_editor/resources/fonts/`) are licensed under the
SIL Open Font License 1.1; see `src/pdf_editor/resources/fonts/OFL.txt`.
