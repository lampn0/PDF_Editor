"""Exception types raised by the core package."""


class PdfError(Exception):
    """Base class for all errors raised by ``pdf_editor.core``."""


class PdfOpenError(PdfError):
    """The file is missing, unreadable, or not a valid PDF."""


class PasswordRequiredError(PdfError):
    """The PDF is encrypted and no password, or a wrong password, was supplied."""


class PdfSaveError(PdfError):
    """Writing the document to disk failed; the original file is left untouched."""
