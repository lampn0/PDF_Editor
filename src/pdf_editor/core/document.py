"""Document model: open, save and track state of a PDF file."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from pdf_editor.core.errors import PasswordRequiredError, PdfError, PdfOpenError, PdfSaveError

# Snapshots (undo/redo) favour speed; files on disk favour size. In-memory bytes
# keep the file /ID so serialising an unchanged document is deterministic.
_SNAPSHOT_OPTIONS: dict[str, Any] = {"garbage": 1, "deflate": False, "no_new_id": True}
_COMPACT_OPTIONS: dict[str, Any] = {"garbage": 3, "deflate": True, "no_new_id": True}
_DISK_OPTIONS: dict[str, Any] = {"garbage": 3, "deflate": True}

_REPLACE_ATTEMPTS = 5
_REPLACE_DELAY = 0.075  # seconds; Windows may hold a target briefly (AV, indexer, viewer)

# authenticate() return codes (bit flags): 1 = no password needed, 2 = user, 4 = owner.
_AUTH_OWNER = 4


@dataclass(frozen=True)
class EncryptionPolicy:
    """How a document is encrypted when written to disk by ``save_as``.

    ``method`` is a ``pymupdf.PDF_ENCRYPT_*`` constant, ``permissions`` the raw
    PDF ``/P`` value. An empty ``user_pw`` means no password is needed to open.
    """

    method: int = pymupdf.PDF_ENCRYPT_AES_256
    permissions: int = -1
    user_pw: str = ""
    owner_pw: str = ""


def _method_from_text(text: str) -> int:
    t = text.upper()
    if "AES" in t:
        return pymupdf.PDF_ENCRYPT_AES_128 if "128" in t else pymupdf.PDF_ENCRYPT_AES_256
    if "RC4" in t:
        return pymupdf.PDF_ENCRYPT_RC4_40 if "40-BIT" in t else pymupdf.PDF_ENCRYPT_RC4_128
    return pymupdf.PDF_ENCRYPT_AES_256


def _read_permissions(doc: pymupdf.Document) -> int:
    """The raw /P value from the Encrypt dictionary (independent of who authenticated)."""
    try:
        kind, value = doc.xref_get_key(-1, "Encrypt")
        if kind == "xref":
            value = doc.xref_object(int(value.split()[0]), compressed=True)
        match = re.search(r"/P\s*(-?\d+)", value or "")
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return doc.permissions


def _read_encryption(
    doc: pymupdf.Document, password: str | None, auth_rc: int
) -> EncryptionPolicy | None:
    """Derive the policy that re-creates ``doc``'s protection on save."""
    method_text = (doc.metadata or {}).get("encryption")
    if not method_text:
        return None
    method = _method_from_text(method_text)
    permissions = _read_permissions(doc)
    if not password or auth_rc == 0 or auth_rc == 1:
        # Owner-only protection (opens without a password): keep restrictions.
        user_pw, owner_pw = "", secrets.token_urlsafe(24)
    elif auth_rc & _AUTH_OWNER:
        # Owner (or identical user/owner) password: the user password, if it
        # differs, is unknown, so the owner password also becomes the open one.
        user_pw = owner_pw = password
    else:
        user_pw, owner_pw = password, secrets.token_urlsafe(24)
    return EncryptionPolicy(method, permissions, user_pw, owner_pw)


def _open_stream(data: bytes, password: str | None, source: str) -> tuple[pymupdf.Document, int]:
    """Open PDF bytes with PyMuPDF, authenticating if needed.

    Returns the document and the ``authenticate`` result (0 when not needed).
    """
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several types for bad data
        raise PdfOpenError(f"Cannot open PDF {source}: {exc}") from exc
    if not doc.is_pdf or (doc.page_count == 0 and not doc.needs_pass):
        doc.close()
        raise PdfOpenError(f"Not a valid PDF: {source}")
    auth_rc = 0
    if doc.needs_pass:
        auth_rc = doc.authenticate(password) if password is not None else 0
        if not auth_rc:
            doc.close()
            msg = "Password required" if password is None else "Wrong password"
            raise PasswordRequiredError(f"{msg} for {source}")
        if doc.page_count == 0:
            doc.close()
            raise PdfOpenError(f"Not a valid PDF: {source}")
    return doc, auth_rc


class PdfDocument:
    """An open PDF held fully in memory, so the source file is never locked."""

    def __init__(
        self,
        doc: pymupdf.Document,
        path: Path | None = None,
        password: str | None = None,
        encryption: EncryptionPolicy | None = None,
    ) -> None:
        self._doc: pymupdf.Document | None = doc
        self._path = path
        self._password = password
        self._encryption = encryption
        self._dirty = False

    # -- construction -------------------------------------------------------

    @classmethod
    def _from_data(
        cls, data: bytes, password: str | None, source: str, path: Path | None
    ) -> PdfDocument:
        doc, auth_rc = _open_stream(data, password, source)
        return cls(doc, path, password, _read_encryption(doc, password, auth_rc))

    @classmethod
    def open(cls, path: str | os.PathLike[str], password: str | None = None) -> PdfDocument:
        """Open ``path``. Raises PdfOpenError or PasswordRequiredError."""
        p = Path(path)
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise PdfOpenError(f"Cannot read {p}: {exc}") from exc
        return cls._from_data(data, password, str(p), p)

    @classmethod
    def from_bytes(cls, data: bytes, password: str | None = None) -> PdfDocument:
        """Open a PDF from in-memory bytes (no associated path)."""
        return cls._from_data(bytes(data), password, "<bytes>", None)

    def load_bytes(self, data: bytes) -> None:
        """Replace the content of this object in place with the PDF in ``data``.

        Path, dirty flag and encryption policy are kept, so external references
        to this object stay valid (used by undo/redo). Raises PdfOpenError on
        invalid data, in which case the current content is left untouched.
        """
        new_doc, _ = _open_stream(bytes(data), self._password, "<snapshot>")
        self._replace_raw(new_doc)

    def restore(self, data: bytes, encryption: EncryptionPolicy | None) -> None:
        """Restore a history state: content from ``data`` plus its encryption policy.

        The dirty flag is not touched (History manages it). On invalid data
        PdfOpenError is raised and neither content nor policy changes.
        """
        self.load_bytes(data)
        self._encryption = encryption

    def _replace_raw(self, new_doc: pymupdf.Document) -> None:
        """Swap the underlying PyMuPDF document, closing the previous one."""
        old = self._doc
        self._doc = new_doc
        if old is not None and old is not new_doc:
            old.close()

    # -- properties ---------------------------------------------------------

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def raw(self) -> pymupdf.Document:
        """Underlying PyMuPDF document (core-internal use)."""
        if self._doc is None:
            raise PdfError("Document is closed")
        return self._doc

    @property
    def is_closed(self) -> bool:
        return self._doc is None

    @property
    def page_count(self) -> int:
        return self.raw.page_count

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    def mark_dirty(self) -> None:
        self._dirty = True

    def mark_clean(self) -> None:
        self._dirty = False

    @property
    def encryption(self) -> EncryptionPolicy | None:
        """Encryption applied by ``save_as`` (recorded at open); None = unencrypted."""
        return self._encryption

    def set_encryption(self, policy: EncryptionPolicy | None) -> None:
        """Change the encryption used on the next save; ``None`` removes it."""
        if policy != self._encryption:
            self._encryption = policy
            self.mark_dirty()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    def __enter__(self) -> PdfDocument:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- serialisation ------------------------------------------------------

    def to_bytes(self, *, compact: bool = False) -> bytes:
        """Serialise the full document, always unencrypted.

        ``compact=False`` (default) is fast and used for undo snapshots;
        ``compact=True`` garbage-collects and deflates.
        """
        options = _COMPACT_OPTIONS if compact else _SNAPSHOT_OPTIONS
        try:
            return self.raw.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE, **options)
        except PdfError:
            raise
        except Exception as exc:
            raise PdfSaveError(f"Cannot serialise document: {exc}") from exc

    def _encryption_options(self) -> dict[str, Any]:
        policy = self._encryption
        if policy is None:
            return {"encryption": pymupdf.PDF_ENCRYPT_NONE}
        return {
            "encryption": policy.method,
            "permissions": policy.permissions,
            "user_pw": policy.user_pw,
            "owner_pw": policy.owner_pw,
        }

    def _bytes_for_disk(self) -> bytes:
        """Compact, font-subsetted, encrypted bytes; the in-memory doc is unchanged."""
        options = {**_DISK_OPTIONS, **self._encryption_options()}
        try:
            copy = pymupdf.open(stream=self.to_bytes(), filetype="pdf")
            try:
                copy.subset_fonts()
                return copy.tobytes(**options)
            finally:
                copy.close()
        except PdfError:
            raise
        except Exception:
            pass  # subsetting is an optimisation only; fall back below
        try:
            return self.raw.tobytes(**options)
        except Exception as exc:
            raise PdfSaveError(f"Cannot serialise document: {exc}") from exc

    def save(self) -> None:
        """Atomically overwrite the original file."""
        if self._path is None:
            raise PdfSaveError("Document has no path; use save_as()")
        self.save_as(self._path)

    def save_as(self, path: str | os.PathLike[str]) -> None:
        """Write to ``path`` atomically: temp file in the same directory, then os.replace.

        Embedded fonts are subsetted and the recorded encryption policy applied.
        On any failure the existing target is left intact and PdfSaveError is raised.
        """
        target = Path(path).resolve()
        if target.exists() and not os.access(target, os.W_OK):
            raise PdfSaveError(f"Cannot save to {target}: file is read-only")
        data = self._bytes_for_disk()
        tmp_name: str | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            try:
                fh = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            if target.exists():
                try:
                    shutil.copymode(target, tmp_name)
                except OSError:
                    pass
            _replace_with_retry(tmp_name, target)
            tmp_name = None
        except OSError as exc:
            raise PdfSaveError(f"Cannot save to {target}: {exc}") from exc
        finally:
            if tmp_name is not None:
                try:
                    # copymode may have made it read-only, which blocks removal on Windows.
                    os.chmod(tmp_name, stat.S_IREAD | stat.S_IWRITE)
                    os.remove(tmp_name)
                except OSError:
                    pass
        self._path = target
        self._dirty = False


def _replace_with_retry(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY)
