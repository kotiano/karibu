"""PDF and CSV report generation.

WHY fpdf2 AND NOT WEASYPRINT. WeasyPrint renders real HTML/CSS and would give
prettier output, but it binds to cairo, pango and gdk-pixbuf — system libraries
that are not present on Render's free tier and cannot be installed there. A
report generator that works on a laptop and 500s in production is worse than a
plainer one that works everywhere. fpdf2 is pure Python with no system deps.

Money is formatted from INTEGER CENTS at the edge, never accumulated as float.
Everything upstream keeps cents; only the strings written into a cell are
decimal, and they are produced by one function so a report cannot disagree with
the screen it was exported from.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime

from fpdf import FPDF

# Brand, as RGB triples. Kept here rather than inlined so a colour change lands
# in one place, the same way globals.css works for the web app.
INK = (26, 28, 27)
INK_MUTED = (91, 97, 93)
PRIMARY = (0, 92, 57)
PAPER_LINE = (231, 228, 218)
PAPER_SUNKEN = (242, 240, 233)


def money(cents: int) -> str:
    """Integer cents -> 'Ksh 12,345.00'. The single formatting authority."""
    return f"Ksh {cents / 100:,.2f}"


# fpdf's built-in fonts are Latin-1 only, and it RAISES on anything outside that
# range rather than substituting. Text in these reports is user data — item
# names, payees, notes, the restaurant's own name — so a single smart quote
# pasted from a phone keyboard would 500 the entire export.
#
# Transliterating is the right trade here rather than bundling a Unicode TTF:
# the characters that actually turn up are typographic punctuation, which has
# exact ASCII equivalents, and a font file is ~500KB of repo plus a licence to
# track. Anything genuinely outside Latin-1 (a name in a non-Latin script)
# degrades to "?" instead of taking the report down.
_TRANSLITERATE = str.maketrans({
    "–": "-", "—": "-",           # en/em dash
    "‘": "'", "’": "'",           # curly single quotes
    "“": '"', "”": '"',           # curly double quotes
    "…": "...",                        # ellipsis
    " ": " ",                          # non-breaking space
    "•": "-",                          # bullet
    "€": "EUR", "£": "GBP",
})


def safe(text: object) -> str:
    """Make any value printable by a Latin-1 core font, losing nothing legible."""
    s = str(text if text is not None else "").translate(_TRANSLITERATE)
    return s.encode("latin-1", "replace").decode("latin-1")


class Report(FPDF):
    """A4 portrait with a branded header and a page-numbered footer.

    The header/footer hooks are called by fpdf during rendering, which is why
    the title has to be state on the instance rather than an argument.
    """

    def __init__(self, title: str, restaurant: str, subtitle: str):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.report_title = title
        self.restaurant = restaurant
        self.subtitle = subtitle
        self.set_auto_page_break(auto=True, margin=20)
        self.set_title(f"{title} - {restaurant}")
        self.set_creator("Karibu POS")

    def header(self) -> None:
        self.set_fill_color(*PRIMARY)
        self.rect(0, 0, self.w, 26, style="F")

        self.set_xy(12, 7)
        self.set_font("helvetica", "B", 14)
        self.set_text_color(255, 255, 255)
        self.cell(0, 6, safe(self.restaurant), new_x="LMARGIN", new_y="NEXT")

        self.set_x(12)
        self.set_font("helvetica", "", 9)
        self.cell(0, 5, safe(f"{self.report_title} - {self.subtitle}"))

        # "Karibu POS" right-aligned on the same band.
        self.set_xy(-70, 7)
        self.set_font("helvetica", "B", 11)
        self.cell(58, 6, "Karibu POS", align="R")

        self.set_y(34)
        self.set_text_color(*INK)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("helvetica", "", 8)
        self.set_text_color(*INK_MUTED)
        stamp = datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
        self.cell(0, 5, f"Generated {stamp}", align="L")
        self.set_y(-15)
        self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    # ── Building blocks ────────────────────────────────────────────────────
    def section(self, label: str) -> None:
        self.ln(4)
        self.set_font("helvetica", "B", 11)
        self.set_text_color(*INK)
        self.cell(0, 7, safe(label), new_x="LMARGIN", new_y="NEXT")

    def stat_row(self, pairs: list[tuple[str, str]]) -> None:
        """A row of headline figures, evenly divided across the page width."""
        if not pairs:
            return
        usable = self.w - 24
        width = usable / len(pairs)
        y = self.get_y()

        self.set_font("helvetica", "", 8)
        self.set_text_color(*INK_MUTED)
        for i, (label, _) in enumerate(pairs):
            self.set_xy(12 + i * width, y)
            self.cell(width, 5, safe(label).upper())

        self.set_font("helvetica", "B", 13)
        self.set_text_color(*INK)
        for i, (_, value) in enumerate(pairs):
            self.set_xy(12 + i * width, y + 5)
            self.cell(width, 7, safe(value))

        self.set_y(y + 14)
        self.set_draw_color(*PAPER_LINE)
        self.line(12, self.get_y(), self.w - 12, self.get_y())
        self.ln(2)

    def table(
        self,
        headers: list[str],
        rows: list[list[str]],
        widths: list[float],
        align: list[str] | None = None,
        empty_message: str = "Nothing to report for this period.",
    ) -> None:
        """A simple banded table.

        An empty table says so in words. A header row with nothing under it
        reads as a rendering failure, and the reader cannot tell it apart from
        one.
        """
        if not rows:
            self.set_font("helvetica", "I", 9)
            self.set_text_color(*INK_MUTED)
            self.cell(0, 8, safe(empty_message), new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(*INK)
            return

        align = align or ["L"] * len(headers)

        def draw_head() -> None:
            self.set_font("helvetica", "B", 8)
            self.set_fill_color(*PAPER_SUNKEN)
            self.set_text_color(*INK_MUTED)
            for h, w, a in zip(headers, widths, align):
                self.cell(w, 7, safe(h).upper(), border=0, align=a, fill=True)
            self.ln()
            self.set_text_color(*INK)

        draw_head()
        self.set_font("helvetica", "", 9)
        for i, row in enumerate(rows):
            # Repeat the header after a page break, or the continuation reads as
            # an unlabelled block of numbers.
            if self.will_page_break(7):
                self.add_page()
                draw_head()
                self.set_font("helvetica", "", 9)
            fill = i % 2 == 1
            self.set_fill_color(250, 249, 245)
            for cell, w, a in zip(row, widths, align):
                self.cell(w, 6.5, _fit(self, safe(cell), w), border=0, align=a, fill=fill)
            self.ln()

        self.set_draw_color(*PAPER_LINE)
        self.line(12, self.get_y(), self.w - 12, self.get_y())


def _fit(pdf: FPDF, text: str, width: float) -> str:
    """Truncate with an ellipsis so a long name cannot overrun its column.

    fpdf does not clip: an oversized string simply draws over the next cell,
    which silently corrupts the row rather than failing.
    """
    if pdf.get_string_width(text) <= width - 2:
        return text
    while text and pdf.get_string_width(text + "...") > width - 2:
        text = text[:-1]
    return text + "..."


def render(pdf: Report) -> bytes:
    """Finalise to bytes. alias_nb_pages makes {nb} resolve in the footer."""
    return bytes(pdf.output())


def csv_bytes(headers: list[str], rows: list[list]) -> bytes:
    """UTF-8 CSV with a BOM.

    The BOM is not decoration: without it Excel on Windows reads UTF-8 as
    Latin-1, and every accented name in a Kenyan menu arrives mangled. The
    accountant this file is for is opening it in Excel.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")
