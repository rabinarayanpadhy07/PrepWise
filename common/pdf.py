"""
Minimal PDF documents (invoices, reports) built with fpdf2.

The built-in PDF fonts only cover Latin-1. Set PDF_UNICODE_FONT_PATH to a
TrueType font (for example DejaVuSans.ttf) to render any script; without it,
characters outside Latin-1 are transliterated where possible or shown as "?".
"""

import unicodedata

from django.conf import settings
from fpdf import FPDF
from fpdf.enums import XPos, YPos

_REPLACEMENTS = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "•": "-",
    "…": "...",
    "₹": "INR ",
    " ": " ",
}


class Document:
    def __init__(self, title):
        self.pdf = FPDF(format="A4")
        self.pdf.set_title(title)
        self.pdf.set_creator("PrepWise")
        self.pdf.set_auto_page_break(auto=True, margin=18)
        self.pdf.set_margins(18, 18, 18)
        self.font = "Helvetica"
        self.unicode = False
        font_path = getattr(settings, "PDF_UNICODE_FONT_PATH", "")
        if font_path:
            self.pdf.add_font("PrepWiseUnicode", "", font_path)
            self.pdf.add_font("PrepWiseUnicode", "B", font_path)
            self.font = "PrepWiseUnicode"
            self.unicode = True
        self.pdf.add_page()

    @property
    def width(self):
        return self.pdf.w - self.pdf.l_margin - self.pdf.r_margin

    def text(self, value):
        value = "" if value is None else str(value)
        if self.unicode:
            return value
        for char, replacement in _REPLACEMENTS.items():
            value = value.replace(char, replacement)
        try:
            value.encode("latin-1")
            return value
        except UnicodeEncodeError:
            pass
        normalised = unicodedata.normalize("NFKD", value)
        return "".join(
            char if ord(char) < 256 else "?"
            for char in normalised
            if not unicodedata.combining(char)
        )

    def _line(self, text, size, style="", height=None, color=(17, 24, 39)):
        self.pdf.set_font(self.font, style, size)
        self.pdf.set_text_color(*color)
        self.pdf.multi_cell(
            self.width,
            height or size * 0.55,
            self.text(text),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )

    def title(self, text):
        self._line(text, 18, "B", 10)
        self.space(2)

    def heading(self, text):
        self.space(3)
        self._line(text, 12.5, "B", 7)
        self.space(1)

    def paragraph(self, text, size=10.5):
        self._line(text, size, height=5.6)

    def muted(self, text, size=9):
        self._line(text, size, height=4.8, color=(75, 85, 99))

    def bullets(self, items, size=10.5):
        for item in items:
            self._line(f"- {item}", size, height=5.6)

    def key_values(self, rows, label_width=55):
        for label, value in rows:
            self.pdf.set_font(self.font, "B", 10)
            self.pdf.set_text_color(55, 65, 81)
            y = self.pdf.get_y()
            self.pdf.multi_cell(
                label_width, 6, self.text(label), new_x=XPos.LMARGIN, new_y=YPos.NEXT
            )
            label_bottom = self.pdf.get_y()
            self.pdf.set_xy(self.pdf.l_margin + label_width, y)
            self.pdf.set_font(self.font, "", 10)
            self.pdf.set_text_color(17, 24, 39)
            self.pdf.multi_cell(
                self.width - label_width,
                6,
                self.text(value),
                new_x=XPos.LMARGIN,
                new_y=YPos.NEXT,
            )
            # Continue below whichever column wrapped further.
            self.pdf.set_y(max(self.pdf.get_y(), label_bottom))

    def rule(self):
        self.space(2)
        y = self.pdf.get_y()
        self.pdf.set_draw_color(209, 213, 219)
        self.pdf.line(self.pdf.l_margin, y, self.pdf.l_margin + self.width, y)
        self.space(3)

    def space(self, height):
        self.pdf.ln(height)

    def output(self):
        return bytes(self.pdf.output())
