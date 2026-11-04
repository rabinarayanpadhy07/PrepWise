"""PDF output must survive whatever text users and AI providers put into it."""

import pytest

from common.pdf import Document

HOSTILE_TEXT = [
    "<script>alert(1)</script> **bold** __under__ --strike--",
    "x" * 5000,  # one unbroken word, far wider than the page
    "Emoji 🎯🚀 and CJK 面接 and RTL مقابلة",
    "Control\x00chars\x07and\x1bescape\ttab\r\nnewline",
    "‮Reversed‬ zero​width",
    "",
    None,
]


@pytest.mark.parametrize("value", HOSTILE_TEXT, ids=range(len(HOSTILE_TEXT)))
def test_document_renders_hostile_text(value):
    document = Document("Report")
    document.title(value)
    document.heading(value)
    document.paragraph(value)
    document.muted(value)
    document.bullets([value, value])
    document.key_values([(value, value), ("Label", value)])

    output = document.output()

    assert output.startswith(b"%PDF-")


def test_text_falls_back_to_latin1_without_a_unicode_font(settings):
    settings.PDF_UNICODE_FONT_PATH = ""
    document = Document("Report")

    assert document.text("“Smart” — ₹5,000") == '"Smart" - INR 5,000'
    assert document.text("面接") == "??"
