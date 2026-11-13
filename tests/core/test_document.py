"""Tests for pdf_editor.core.document."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pymupdf
import pytest

from pdf_editor.core import document as document_module
from pdf_editor.core.document import EncryptionPolicy, PdfDocument
from pdf_editor.core.errors import PasswordRequiredError, PdfError, PdfOpenError, PdfSaveError

from .helpers import NOTO_REGULAR, copy_sample, make_pdf_bytes, markers

PERMS = pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ACCESSIBILITY


def _encrypted_bytes(
    count: int = 2, user_pw: str = "user-pw", owner_pw: str = "owner-pw", **kw
) -> bytes:
    with pymupdf.open(stream=make_pdf_bytes(count), filetype="pdf") as src:
        return src.tobytes(
            encryption=kw.pop("method", pymupdf.PDF_ENCRYPT_AES_256),
            user_pw=user_pw,
            owner_pw=owner_pw,
            **kw,
        )


@pytest.fixture
def sample(data_dir: Path, tmp_path: Path) -> Path:
    return copy_sample(data_dir, tmp_path)


@pytest.fixture
def encrypted_pdf(tmp_path: Path) -> Path:
    target = tmp_path / "secret.pdf"
    target.write_bytes(_encrypted_bytes())
    return target


@pytest.fixture
def small(tmp_path: Path) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(make_pdf_bytes(3))
    return path


# -- open ---------------------------------------------------------------------


def test_open_sample_has_pages(sample: Path) -> None:
    with PdfDocument.open(sample) as doc:
        assert doc.page_count > 0
        assert doc.path == sample
        assert not doc.is_dirty
        assert doc.encryption is None


def test_open_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PdfOpenError):
        PdfDocument.open(tmp_path / "does_not_exist.pdf")


def test_open_corrupt_file(tmp_path: Path) -> None:
    bad = tmp_path / "corrupt.pdf"
    bad.write_bytes(b"this is definitely not a PDF file\x00\x01\x02" * 10)
    with pytest.raises(PdfOpenError):
        PdfDocument.open(bad)


def test_from_bytes_corrupt() -> None:
    with pytest.raises(PdfOpenError):
        PdfDocument.from_bytes(b"%PDF-1.7\ngarbage")


def test_encrypted_without_password(encrypted_pdf: Path) -> None:
    with pytest.raises(PasswordRequiredError):
        PdfDocument.open(encrypted_pdf)


def test_encrypted_wrong_password(encrypted_pdf: Path) -> None:
    with pytest.raises(PasswordRequiredError):
        PdfDocument.open(encrypted_pdf, password="nope")


def test_encrypted_right_password(encrypted_pdf: Path) -> None:
    with PdfDocument.open(encrypted_pdf, password="user-pw") as doc:
        assert doc.page_count == 2
        assert markers(doc) == ["P0", "P1"]


def test_password_error_is_pdf_error() -> None:
    assert issubclass(PasswordRequiredError, PdfError)
    assert issubclass(PdfOpenError, PdfError)


# -- save ---------------------------------------------------------------------


def test_save_as_round_trip(sample: Path, tmp_path: Path) -> None:
    out = tmp_path / "out" / "copy.pdf"
    out.parent.mkdir()
    with PdfDocument.open(sample) as doc:
        count = doc.page_count
        texts = markers(doc)
        doc.mark_dirty()
        doc.save_as(out)
        assert doc.path == out.resolve()
        assert not doc.is_dirty
    with PdfDocument.open(out) as reopened:
        assert reopened.page_count == count
        assert markers(reopened) == texts
    assert not list(out.parent.glob("*.tmp"))


def test_save_overwrites_in_place(small: Path) -> None:
    with PdfDocument.open(small) as doc:
        doc.raw.delete_page(0)
        doc.mark_dirty()
        doc.save()
    with PdfDocument.open(small) as doc:
        assert markers(doc) == ["P1", "P2"]


def test_save_without_path_raises() -> None:
    with PdfDocument.from_bytes(make_pdf_bytes(1)) as doc, pytest.raises(PdfSaveError):
        doc.save()


def test_save_as_into_missing_dir(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "out.pdf"
    with PdfDocument.from_bytes(make_pdf_bytes(1)) as doc:
        doc.mark_dirty()
        with pytest.raises(PdfSaveError):
            doc.save_as(target)
        assert doc.is_dirty
        assert doc.path is None
    assert not target.exists()
    assert not list(tmp_path.rglob("*.tmp"))


def test_save_atomic_on_replace_failure(small: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = small.read_bytes()
    with PdfDocument.open(small) as doc:
        doc.raw.delete_page(0)
        doc.mark_dirty()

        def boom(src: object, dst: object) -> None:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(PdfSaveError):
            doc.save()
        monkeypatch.undo()
        assert doc.is_dirty

    assert small.read_bytes() == original
    assert not list(small.parent.glob("*.tmp"))


def test_save_retries_replace_on_permission_error(
    small: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    calls: list[int] = []

    def flaky(src, dst) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("locked")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(document_module, "_REPLACE_DELAY", 0)
    with PdfDocument.open(small) as doc:
        doc.raw.delete_page(0)
        doc.save()
    assert len(calls) == 3
    with PdfDocument.open(small) as doc:
        assert markers(doc) == ["P1", "P2"]


def test_save_gives_up_after_retries(small: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = small.read_bytes()

    def locked(src, dst) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", locked)
    monkeypatch.setattr(document_module, "_REPLACE_DELAY", 0)
    with PdfDocument.open(small) as doc, pytest.raises(PdfSaveError):
        doc.save()
    monkeypatch.undo()
    assert small.read_bytes() == original
    assert not list(small.parent.glob("*.tmp"))


@pytest.fixture
def read_only(small: Path):
    os.chmod(small, stat.S_IREAD)
    yield small
    os.chmod(small, stat.S_IREAD | stat.S_IWRITE)


def test_save_refuses_read_only_target(read_only: Path) -> None:
    original = read_only.read_bytes()
    with PdfDocument.open(read_only) as doc:
        doc.raw.delete_page(0)
        doc.mark_dirty()
        with pytest.raises(PdfSaveError, match="read-only"):
            doc.save()
        assert doc.is_dirty
    assert read_only.read_bytes() == original
    assert not list(read_only.parent.glob("*.tmp"))


def test_read_only_temp_is_cleaned_up(small: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = small.read_bytes()

    def copymode_read_only(src, dst, **kwargs) -> None:
        os.chmod(dst, stat.S_IREAD)

    def boom(src, dst) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(document_module.shutil, "copymode", copymode_read_only)
    monkeypatch.setattr(os, "replace", boom)
    with PdfDocument.open(small) as doc, pytest.raises(PdfSaveError):
        doc.save()
    monkeypatch.undo()
    assert small.read_bytes() == original
    assert not list(small.parent.glob("*.tmp"))


def test_dirty_flag() -> None:
    with PdfDocument.from_bytes(make_pdf_bytes(1)) as doc:
        assert not doc.is_dirty
        doc.mark_dirty()
        assert doc.is_dirty
        doc.mark_clean()
        assert not doc.is_dirty


def test_closed_document_raises() -> None:
    doc = PdfDocument.from_bytes(make_pdf_bytes(1))
    doc.close()
    assert doc.is_closed
    with pytest.raises(PdfError):
        _ = doc.page_count
    doc.close()  # idempotent


# -- bytes --------------------------------------------------------------------


@pytest.mark.parametrize("compact", [False, True])
def test_to_bytes_round_trip(compact: bool) -> None:
    with PdfDocument.from_bytes(make_pdf_bytes(4)) as doc:
        with PdfDocument.from_bytes(doc.to_bytes(compact=compact)) as again:
            assert markers(again) == markers(doc)


def test_snapshot_bytes_are_deterministic(sample: Path) -> None:
    with PdfDocument.open(sample) as doc:
        assert doc.to_bytes() == doc.to_bytes()


def test_load_bytes_keeps_identity_path_and_dirty(small: Path) -> None:
    with PdfDocument.open(small) as doc:
        doc.mark_dirty()
        doc.load_bytes(make_pdf_bytes(5, prefix="Q"))
        assert doc.page_count == 5
        assert markers(doc)[0] == "Q0"
        assert doc.path == small
        assert doc.is_dirty


def test_load_bytes_invalid_keeps_content() -> None:
    with PdfDocument.from_bytes(make_pdf_bytes(2)) as doc:
        with pytest.raises(PdfOpenError):
            doc.load_bytes(b"not a pdf")
        assert markers(doc) == ["P0", "P1"]


# -- encryption ---------------------------------------------------------------


def test_snapshot_bytes_are_unencrypted(encrypted_pdf: Path) -> None:
    with PdfDocument.open(encrypted_pdf, password="user-pw") as doc:
        assert doc.encryption is not None
        with pymupdf.open(stream=doc.to_bytes(), filetype="pdf") as plain:
            assert not plain.needs_pass
        doc.load_bytes(doc.to_bytes())
        assert doc.encryption is not None


def test_save_keeps_user_password(encrypted_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    with PdfDocument.open(encrypted_pdf, password="user-pw") as doc:
        policy = doc.encryption
        assert policy is not None
        assert policy.method == pymupdf.PDF_ENCRYPT_AES_256
        assert policy.user_pw == "user-pw"
        assert policy.owner_pw and policy.owner_pw != "user-pw"
        doc.save_as(out)
    with pytest.raises(PasswordRequiredError):
        PdfDocument.open(out)
    with PdfDocument.open(out, password="user-pw") as reopened:
        assert markers(reopened) == ["P0", "P1"]


def test_save_preserves_permissions(tmp_path: Path) -> None:
    src = tmp_path / "perm.pdf"
    src.write_bytes(_encrypted_bytes(permissions=PERMS))
    out = tmp_path / "out.pdf"
    with PdfDocument.open(src, password="user-pw") as doc:
        original_perms = doc.raw.permissions
        doc.save_as(out)
    with PdfDocument.open(out, password="user-pw") as reopened:
        assert reopened.raw.permissions == original_perms
        assert not reopened.raw.permissions & pymupdf.PDF_PERM_MODIFY


def test_owner_password_becomes_open_password(tmp_path: Path) -> None:
    src = tmp_path / "perm.pdf"
    src.write_bytes(_encrypted_bytes(permissions=PERMS))
    out = tmp_path / "out.pdf"
    with PdfDocument.open(src, password="owner-pw") as doc:
        assert doc.encryption is not None
        assert doc.encryption.permissions == _encrypted_permissions(src)
        doc.save_as(out)
    with pytest.raises(PasswordRequiredError):
        PdfDocument.open(out)
    with PdfDocument.open(out, password="owner-pw") as reopened:
        assert reopened.page_count == 2
    assert _encrypted_permissions(out) == _encrypted_permissions(src)


def _encrypted_permissions(path: Path) -> int:
    with pymupdf.open(path) as raw:
        return document_module._read_permissions(raw)


def test_owner_only_restrictions_kept(tmp_path: Path) -> None:
    src = tmp_path / "restricted.pdf"
    src.write_bytes(
        _encrypted_bytes(user_pw="", method=pymupdf.PDF_ENCRYPT_RC4_128, permissions=PERMS)
    )
    out = tmp_path / "out.pdf"
    with PdfDocument.open(src) as doc:
        original_perms = doc.raw.permissions
        policy = doc.encryption
        assert policy is not None
        assert policy.user_pw == ""
        assert policy.method == pymupdf.PDF_ENCRYPT_RC4_128
        doc.save_as(out)
    with PdfDocument.open(out) as reopened:  # still opens without a password
        assert reopened.raw.metadata["encryption"]
        assert reopened.raw.permissions == original_perms


def test_set_encryption_none_removes_it(encrypted_pdf: Path, tmp_path: Path) -> None:
    out = tmp_path / "plain.pdf"
    with PdfDocument.open(encrypted_pdf, password="user-pw") as doc:
        doc.set_encryption(None)
        assert doc.encryption is None
        assert doc.is_dirty
        doc.save_as(out)
    with PdfDocument.open(out) as reopened:
        assert not reopened.raw.metadata.get("encryption")


def test_restore_sets_content_and_policy_without_dirtying(encrypted_pdf: Path) -> None:
    with PdfDocument.open(encrypted_pdf, password="user-pw") as doc:
        original = doc.encryption
        snapshot = doc.to_bytes()
        doc.set_encryption(None)
        doc.raw.delete_page(0)
        doc.mark_clean()
        doc.restore(snapshot, original)
        assert doc.encryption == original
        assert doc.page_count == 2
        assert not doc.is_dirty
        with pytest.raises(PdfOpenError):
            doc.restore(b"junk", None)
        assert doc.encryption == original


def test_set_encryption_on_plain_document(small: Path, tmp_path: Path) -> None:
    out = tmp_path / "enc.pdf"
    with PdfDocument.open(small) as doc:
        doc.set_encryption(EncryptionPolicy(user_pw="new", owner_pw="own"))
        doc.save_as(out)
    with pytest.raises(PasswordRequiredError):
        PdfDocument.open(out)
    with PdfDocument.open(out, password="new") as reopened:
        assert markers(reopened) == ["P0", "P1", "P2"]


# -- font subsetting ----------------------------------------------------------


def _doc_with_noto_text(pages: int = 2) -> PdfDocument:
    doc = PdfDocument.from_bytes(make_pdf_bytes(pages))
    font_buffer = NOTO_REGULAR.read_bytes()
    for i, page in enumerate(doc.raw):
        page.insert_font(fontname="noto", fontbuffer=font_buffer)
        page.insert_text((72, 200), f"Xin chào {i}", fontname="noto", fontsize=14)
    doc.mark_dirty()
    return doc


@pytest.mark.skipif(not NOTO_REGULAR.exists(), reason="bundled Noto font missing")
def test_save_subsets_fonts(tmp_path: Path) -> None:
    out = tmp_path / "noto.pdf"
    with _doc_with_noto_text() as doc:
        unsubsetted = len(doc.to_bytes(compact=True))
        doc.save_as(out)
        assert len(doc.to_bytes(compact=True)) == unsubsetted  # in-memory doc unchanged
    assert out.stat().st_size < unsubsetted / 2
    with PdfDocument.open(out) as reopened:
        texts = [page.get_text() for page in reopened.raw]
        assert "Xin chào 0" in texts[0] and "Xin chào 1" in texts[1]


@pytest.mark.skipif(not NOTO_REGULAR.exists(), reason="bundled Noto font missing")
def test_save_falls_back_when_subsetting_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self, *args, **kwargs):
        raise RuntimeError("subset failed")

    monkeypatch.setattr(pymupdf.Document, "subset_fonts", broken)
    out = tmp_path / "noto.pdf"
    with _doc_with_noto_text() as doc:
        doc.save_as(out)
    with PdfDocument.open(out) as reopened:
        assert "Xin chào 1" in reopened.raw[1].get_text()
