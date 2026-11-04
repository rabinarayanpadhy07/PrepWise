"""
Safe text extraction from uploaded resumes (PDF and DOCX).

The file type is decided by its content (magic bytes), not by the name or the
browser-supplied content type. Every failure raises ExtractionError with a
stable code and a user-facing message; nothing is ever guessed or invented.
OCR is not performed, so image-only (scanned) PDFs are reported as such.
"""

import hashlib
import io
import logging
import re
import zipfile
from dataclasses import dataclass

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

logger = logging.getLogger("prepwise.resumes")

PDF = "pdf"
DOCX = "docx"
EXTENSIONS = {".pdf": PDF, ".docx": DOCX}

MAX_PDF_PAGES = 20
MAX_TEXT_CHARS = 60_000
MIN_TEXT_CHARS = 50

# DOCX (zip) safety limits against zip bombs.
MAX_DOCX_ENTRIES = 2_000
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_DOCX_XML_BYTES = 15 * 1024 * 1024

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy .doc and encrypted Office files
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class ExtractionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExtractedResume:
    file_type: str
    text: str
    page_count: int | None
    sha256: str


def extract_resume(filename, data):
    """Validate `data` (the uploaded bytes) and return its text."""
    file_type = _detect_type(filename, data)
    if file_type == PDF:
        text, pages = _extract_pdf(data)
    else:
        text, pages = _extract_docx(data), None

    text = normalise_text(text)
    if len(text) < MIN_TEXT_CHARS:
        if file_type == PDF:
            raise ExtractionError(
                "no_text_found",
                "We couldn't find any text in this PDF. It may be a scanned image. "
                "Upload a PDF exported from a word processor, or a DOCX file.",
            )
        raise ExtractionError("no_text_found", "We couldn't find any text in this document.")
    if len(text) > MAX_TEXT_CHARS:
        raise ExtractionError(
            "too_much_text",
            f"This document has more than {MAX_TEXT_CHARS:,} characters of text. "
            "Upload a shorter resume.",
        )
    return ExtractedResume(
        file_type=file_type, text=text, page_count=pages, sha256=hashlib.sha256(data).hexdigest()
    )


def _detect_type(filename, data):
    extension = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    expected = EXTENSIONS.get(extension)
    if expected is None:
        raise ExtractionError("unsupported_file_type", "Upload a PDF or DOCX file.")

    if data.startswith(_PDF_MAGIC):
        detected = PDF
    elif data.startswith(_ZIP_MAGIC):
        detected = DOCX
    elif data.startswith(_OLE_MAGIC):
        raise ExtractionError(
            "unsupported_file_type",
            "This looks like an old .doc file or a password-protected Word document. "
            "Save it as an unprotected DOCX or PDF and upload it again.",
        )
    else:
        raise ExtractionError("unsupported_file_type", "This file isn't a valid PDF or DOCX file.")

    if detected != expected:
        raise ExtractionError(
            "file_type_mismatch", "The file's contents don't match its .pdf or .docx extension."
        )
    return detected


def _extract_pdf(data):
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            # Some PDFs are "encrypted" only to restrict editing and open without a
            # password. Anything that needs a real password is rejected.
            try:
                if not reader.decrypt(""):
                    raise _encrypted_pdf()
            except (FileNotDecryptedError, NotImplementedError) as exc:
                raise _encrypted_pdf() from exc

        page_count = len(reader.pages)
        if page_count == 0:
            raise ExtractionError("unreadable_file", "This PDF has no pages.")
        if page_count > MAX_PDF_PAGES:
            raise ExtractionError(
                "too_many_pages",
                f"This PDF has {page_count} pages. "
                f"Upload a resume of {MAX_PDF_PAGES} pages or fewer.",
            )
        parts = [page.extract_text() or "" for page in reader.pages]
    except ExtractionError:
        raise
    except (PdfReadError, FileNotDecryptedError) as exc:
        raise _unreadable() from exc
    except Exception as exc:  # noqa: BLE001 - malformed PDFs raise many exception types
        logger.info("PDF parsing failed: %s", type(exc).__name__)
        raise _unreadable() from exc
    return "\n".join(parts), page_count


def _extract_docx(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES or (
                sum(entry.file_size for entry in entries) > MAX_DOCX_UNCOMPRESSED_BYTES
            ):
                raise ExtractionError("unreadable_file", "This DOCX file is too large to process.")
            try:
                document = archive.getinfo("word/document.xml")
            except KeyError as exc:
                raise ExtractionError(
                    "unreadable_file", "This file isn't a valid Word (DOCX) document."
                ) from exc
            if document.file_size > MAX_DOCX_XML_BYTES:
                raise ExtractionError("unreadable_file", "This DOCX file is too large to process.")
            xml = archive.read(document)
        root = ElementTree.fromstring(xml)
    except ExtractionError:
        raise
    except (zipfile.BadZipFile, ElementTree.ParseError, DefusedXmlException, ValueError) as exc:
        raise _unreadable() from exc

    paragraphs = []
    for paragraph in root.iter(f"{_W_NS}p"):
        pieces = []
        for node in paragraph.iter():
            if node.tag == f"{_W_NS}t" and node.text:
                pieces.append(node.text)
            elif node.tag in (f"{_W_NS}tab",):
                pieces.append("\t")
            elif node.tag in (f"{_W_NS}br", f"{_W_NS}cr"):
                pieces.append("\n")
        paragraphs.append("".join(pieces))
    return "\n".join(paragraphs)


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACES = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def normalise_text(text):
    """Strip control characters (PostgreSQL rejects NUL) and collapse whitespace."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def _encrypted_pdf():
    return ExtractionError(
        "encrypted_file",
        "This PDF is password-protected. Remove the password and upload it again.",
    )


def _unreadable():
    return ExtractionError(
        "unreadable_file", "We couldn't read this file. It may be damaged or in an unusual format."
    )
