"""Font discovery, embedding and fallback handling.

``resolve_font`` picks the font used to re-insert edited text: the span's own
embedded font when it provably maps every character, otherwise a bundled
Unicode font (Noto Sans, with Vietnamese coverage). ``register_font`` makes
that font available on a page under a stable, content-derived resource name.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf

from pdf_editor.core.errors import PdfError

if TYPE_CHECKING:
    from pdf_editor.core.document import PdfDocument
    from pdf_editor.core.text import TextSpan

FONT_DIR = Path(__file__).resolve().parent.parent / "resources" / "fonts"

_BUNDLED = {False: "NotoSans-Regular.ttf", True: "NotoSans-Bold.ttf"}
# Built-in PyMuPDF names (need the optional ``pymupdf-fonts`` package).
_BUILTIN = {False: "notos", True: "notosbo"}
_SYSTEM = {
    False: (Path(r"C:\Windows\Fonts\arial.ttf"),),
    True: (Path(r"C:\Windows\Fonts\arialbd.ttf"), Path(r"C:\Windows\Fonts\arial.ttf")),
}
# Font extensions PyMuPDF reports for embedded fonts it can load from a buffer.
_LOADABLE_EXT = {"ttf", "otf", "cff", "pfa", "pfb"}
_RESOURCE_PREFIX = "PE"
_MAX_NAME_SUFFIX = 100

_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")


def _base_name(name: str) -> str:
    """Strip a subset tag ("ABCDEF+Arial" -> "Arial")."""
    return _SUBSET_PREFIX.sub("", name)


def _name_matches(basefont: str, span_font: str) -> bool:
    """Span font names may be truncated by the extractor, so allow a prefix match."""
    b, want = _base_name(basefont), _base_name(span_font)
    return bool(want) and (b == want or b.startswith(want))


def _covers(font: pymupdf.Font, text: str) -> bool:
    return all(font.has_glyph(ord(ch)) for ch in text)


def is_bold(flags: int) -> bool:
    return bool(flags & pymupdf.TEXT_FONT_BOLD)


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def resource_name(font: pymupdf.Font) -> str:
    """Stable resource name derived from the font program ("PE" + 10 hex digits).

    Independent of xref numbers, so it survives garbage collection on save.
    """
    return _RESOURCE_PREFIX + _sha1(font.buffer)[:10]


# -- fallback font ------------------------------------------------------------


@lru_cache(maxsize=2)
def _fallback(bold: bool) -> tuple[bytes, str]:
    """Return (font buffer, description) for the fallback font.

    Order: bundled Noto Sans, PyMuPDF built-in Noto Sans, Windows Arial.
    """
    bundled = FONT_DIR / _BUNDLED[bold]
    if not bundled.is_file() and bold:
        bundled = FONT_DIR / _BUNDLED[False]
    if bundled.is_file():
        return bundled.read_bytes(), f"bundled {bundled.name}"
    try:
        return pymupdf.Font(_BUILTIN[bold]).buffer, f"built-in {_BUILTIN[bold]}"
    except Exception:  # pymupdf-fonts not installed
        pass
    for path in _SYSTEM[bold]:
        if path.is_file():
            return path.read_bytes(), f"system {path.name}"
    raise PdfError("No fallback Unicode font available (bundled Noto Sans is missing)")


def fallback_font(bold: bool = False) -> tuple[pymupdf.Font, str]:
    """The fallback font and its resource name."""
    buffer, _ = _fallback(bold)
    font = pymupdf.Font(fontbuffer=buffer)
    return font, resource_name(font)


def fallback_source(bold: bool = False) -> str:
    """Human-readable description of where the fallback font came from."""
    return _fallback(bold)[1]


# -- glyph trace of a span ----------------------------------------------------


def span_render_mode(page: pymupdf.Page, span: TextSpan) -> int:
    """Render mode of the glyphs drawn inside ``span`` (texttrace "type").

    0 fill, 1 stroke, 2 clip, 3 invisible. Characters belong to the span when
    their box centre lies inside the span bbox and the direction matches.
    """
    x0, y0, x1, y1 = span.bbox
    for tspan in page.get_texttrace():
        tdir = tspan.get("dir", (1.0, 0.0))
        if abs(tdir[0] - span.dir[0]) > 1e-3 or abs(tdir[1] - span.dir[1]) > 1e-3:
            continue
        for ch in tspan["chars"]:
            bx0, by0, bx1, by1 = ch[3]
            if x0 <= (bx0 + bx1) / 2 <= x1 and y0 <= (by0 + by1) / 2 <= y1:
                return int(tspan.get("type", 0))
    return 0


# -- embedded font reuse ------------------------------------------------------


def _embedded_candidates(
    raw: pymupdf.Document, page: pymupdf.Page, span_font: str
) -> list[pymupdf.Font]:
    """Embedded Type0 / Identity-H fonts on ``page`` whose name matches ``span_font``.

    Only CID fonts with Identity-H encoding are reused: their programs carry a
    Unicode cmap that PyMuPDF can re-encode reliably. Simple (8-bit) fonts
    often have symbolic or custom-encoded cmaps where ``has_glyph`` lies.
    """
    out: list[pymupdf.Font] = []
    for entry in page.get_fonts(full=True):
        xref, ext, ftype, basefont, encoding = entry[0], entry[1], entry[2], entry[3], entry[5]
        if ext not in _LOADABLE_EXT or ftype != "Type0" or encoding != "Identity-H":
            continue
        if not _name_matches(basefont, span_font):
            continue
        try:
            buffer = raw.extract_font(xref)[3]
            if buffer:
                out.append(pymupdf.Font(fontbuffer=buffer))
        except Exception:  # unreadable / unsupported font program
            continue
    return out


def resolve_font(
    doc: PdfDocument, page: pymupdf.Page, span: TextSpan, text: str
) -> tuple[pymupdf.Font, str, bool]:
    """Choose the font used to write ``text`` in place of ``span``.

    Returns ``(font, fontname_for_insert, substituted)``; pass the name to
    ``register_font``, which may return a different (suffixed) name.

    An embedded font is reused only if it is a Type0/Identity-H font with the
    span's font name and has a glyph for every character of both the original
    span text and ``text``. (Matching the glyph ids drawn on the page was
    rejected: a PDF may draw one visual span with a simple and a CID font of
    the same name, so ids legitimately differ.) Otherwise the fallback font
    is used (bold variant when the span is bold) and ``substituted`` is True.
    """
    for font in _embedded_candidates(doc.raw, page, span.font):
        if _covers(font, span.text) and _covers(font, text):
            return font, resource_name(font), False
    font, name = fallback_font(is_bold(span.flags))
    return font, name, True


# -- registering a font on a page ---------------------------------------------


def _utf16_hex(cp: int) -> str:
    return chr(cp).encode("utf-16-be", "surrogatepass").hex()


def _tounicode_cmap(font: pymupdf.Font) -> bytes:
    """A ToUnicode CMap mapping each glyph id (= CID) to its lowest code point.

    When several code points share a glyph (U+0020/U+00A0, U+00B5/U+03BC,
    U+03A9/U+2126, ...) the lowest is chosen; this is the basic/canonical form
    in practice. PyMuPDF's generated map may otherwise pick e.g. U+00A0 for
    the space glyph, so extracted text would contain no-break spaces.
    """
    best: dict[int, int] = {}
    for cp in font.valid_codepoints():
        gid = font.has_glyph(cp)
        if gid and (gid not in best or cp < best[gid]):
            best[gid] = cp
    items = sorted(best.items())
    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    for start in range(0, len(items), 100):
        chunk = items[start : start + 100]
        lines.append(f"{len(chunk)} beginbfchar")
        lines.extend(f"<{gid:04x}> <{_utf16_hex(cp)}>" for gid, cp in chunk)
        lines.append("endbfchar")
    lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("ascii")


def _fix_tounicode(raw: pymupdf.Document, xref: int, font: pymupdf.Font) -> None:
    kind, value = raw.xref_get_key(xref, "ToUnicode")
    if kind == "xref" and raw.xref_get_key(xref, "Subtype")[1] == "/Type0":
        raw.update_stream(int(value.split()[0]), _tounicode_cmap(font))


def _program_digest(raw: pymupdf.Document, xref: int) -> str | None:
    try:
        buffer = raw.extract_font(xref)[3]
    except Exception:
        return None
    return _sha1(buffer) if buffer else None


# id(raw document) -> {program digest: font xref}. pymupdf.Document does not
# support weak references, so every hit is re-validated by digest (an id may
# be reused after a document is closed).
_XREF_CACHE: dict[int, dict[str, int]] = {}
_XREF_CACHE_MAX_DOCS = 16


def _remember(raw: pymupdf.Document, digest: str, xref: int) -> None:
    if id(raw) not in _XREF_CACHE and len(_XREF_CACHE) >= _XREF_CACHE_MAX_DOCS:
        _XREF_CACHE.clear()
    _XREF_CACHE.setdefault(id(raw), {})[digest] = xref


def _find_doc_font(raw: pymupdf.Document, fontname: str, digest: str) -> int | None:
    """Xref of a font object anywhere in the document with this exact program."""
    cached = _XREF_CACHE.get(id(raw), {}).get(digest)
    if cached is not None and cached < raw.xref_length():
        if _program_digest(raw, cached) == digest:
            return cached
    seen: set[int] = set()
    for pno in range(raw.page_count):
        for entry in raw.get_page_fonts(pno, full=True):
            xref = entry[0]
            if xref in seen or not entry[4].startswith(fontname):
                continue
            seen.add(xref)
            if _program_digest(raw, xref) == digest:
                _remember(raw, digest, xref)
                return xref
    return None


def _bind(raw: pymupdf.Document, page: pymupdf.Page, name: str, xref: int) -> bool:
    """Add ``/name xref 0 R`` to the page's own /Resources /Font dictionary."""
    ref = f"{xref} 0 R"
    kind, value = raw.xref_get_key(page.xref, "Resources")
    if kind == "xref":
        holder, prefix = int(value.split()[0]), ""
    elif kind == "dict":
        holder, prefix = page.xref, "Resources/"
    else:  # inherited resources: do not shadow them with a new dictionary
        return False
    fkind, fvalue = raw.xref_get_key(holder, prefix + "Font")
    if fkind == "xref":
        raw.xref_set_key(int(fvalue.split()[0]), name, ref)
    elif fkind in ("dict", "null"):
        raw.xref_set_key(holder, f"{prefix}Font/{name}", ref)
    else:
        return False
    return True


def register_font(page: pymupdf.Page, fontname: str, font: pymupdf.Font) -> str:
    """Make ``font`` available on ``page``; return the resource name to use.

    * If the page already has ``fontname`` bound to the same program, it is
      reused. If bound to a different program (e.g. a subsetted copy after
      save), a suffixed name (``fontname_1``...) is used instead.
    * An identical font object elsewhere in the document is shared rather
      than embedded again (also after the document was reloaded).
    * Newly embedded Type0 fonts get a normalised ToUnicode map so inserted
      text extracts exactly as written.
    """
    raw = page.parent
    digest = _sha1(font.buffer)
    page_fonts = {entry[4]: entry[0] for entry in page.get_fonts(full=True)}
    for i in range(_MAX_NAME_SUFFIX):
        name = fontname if i == 0 else f"{fontname}_{i}"
        if name in page_fonts:
            if _program_digest(raw, page_fonts[name]) == digest:
                page.insert_font(fontname=name)  # builds PyMuPDF's font info
                return name
            continue
        existing = _find_doc_font(raw, fontname, digest)
        if existing is not None and _bind(raw, page, name, existing):
            page.insert_font(fontname=name)
            return name
        xref = page.insert_font(fontname=name, fontbuffer=font.buffer)
        _fix_tounicode(raw, xref, font)
        _remember(raw, digest, xref)
        return name
    raise PdfError(f"Cannot find a free font resource name for {fontname}")
