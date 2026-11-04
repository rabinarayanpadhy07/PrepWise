import pytest

from apps.resumes import extraction
from apps.resumes.extraction import ExtractionError, extract_resume, normalise_text
from apps.resumes.tests.files import (
    ENTITY_BOMB_XML,
    make_blank_pdf,
    make_docx,
    make_encrypted_pdf,
    make_pdf,
)


def assert_fails(filename, data, code):
    with pytest.raises(ExtractionError) as excinfo:
        extract_resume(filename, data)
    assert excinfo.value.code == code
    return excinfo.value


def test_extracts_text_from_pdf():
    result = extract_resume("resume.pdf", make_pdf())

    assert result.file_type == "pdf"
    assert result.page_count == 1
    assert "Senior Backend Engineer" in result.text
    assert "PostgreSQL" in result.text
    assert len(result.sha256) == 64


def test_extracts_text_from_docx():
    result = extract_resume("Resume.DOCX", make_docx())

    assert result.file_type == "docx"
    assert result.page_count is None
    assert result.text.splitlines()[0] == "Jane Doe - Senior Backend Engineer"


def test_pdf_with_owner_password_only_is_readable():
    result = extract_resume("resume.pdf", make_encrypted_pdf(user_password=""))

    assert "Django" in result.text


def test_password_protected_pdf_is_rejected():
    error = assert_fails("resume.pdf", make_encrypted_pdf(user_password="secret"), "encrypted_file")
    assert "password-protected" in error.message


def test_scanned_pdf_without_text_is_reported_not_guessed():
    error = assert_fails("scan.pdf", make_blank_pdf(), "no_text_found")
    assert "scanned" in error.message


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.7\nthis is not really a pdf", b"%PDF-1.4\n" + b"\x00\xff" * 500],
)
def test_malformed_pdf_is_rejected(data):
    assert_fails("resume.pdf", data, "unreadable_file")


def test_pdf_page_limit():
    assert_fails("long.pdf", make_blank_pdf(pages=extraction.MAX_PDF_PAGES + 1), "too_many_pages")


def test_pdf_disguised_as_docx_is_rejected():
    assert_fails("resume.docx", make_pdf(), "file_type_mismatch")


@pytest.mark.parametrize(
    "filename, data",
    [
        ("resume.txt", b"plain text resume"),
        ("resume", make_pdf()),
        ("resume.pdf", b"GIF89a fake image"),
        ("resume.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1legacy word document"),
    ],
)
def test_unsupported_files_are_rejected(filename, data):
    assert_fails(filename, data, "unsupported_file_type")


def test_corrupt_docx_is_rejected():
    assert_fails("resume.docx", b"PK\x03\x04 truncated zip", "unreadable_file")


def test_docx_without_document_part_is_rejected():
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.xml", "<x/>")

    assert_fails("resume.docx", buffer.getvalue(), "unreadable_file")


def test_docx_xml_entities_are_refused():
    assert_fails("resume.docx", make_docx(document_xml=ENTITY_BOMB_XML), "unreadable_file")


def test_docx_size_limits_guard_against_zip_bombs(monkeypatch):
    monkeypatch.setattr(extraction, "MAX_DOCX_UNCOMPRESSED_BYTES", 100)

    assert_fails("resume.docx", make_docx(), "unreadable_file")


def test_too_little_text_is_rejected():
    assert_fails("resume.docx", make_docx(lines=["Jane Doe"]), "no_text_found")


def test_too_much_text_is_rejected(monkeypatch):
    monkeypatch.setattr(extraction, "MAX_TEXT_CHARS", 100)

    assert_fails("resume.docx", make_docx(), "too_much_text")


def test_normalise_text_removes_nul_and_control_characters():
    text = normalise_text("Line\x00 one\x07\r\n\r\n\r\n\r\nLine\t\t two   ")

    assert text == "Line one\n\nLine two"
