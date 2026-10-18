"""A4 page furniture for Pingo's PDF documents, drawn with ReportLab.

Coordinates are in millimetres measured from the top-left corner, like the layout this replaces; they are
converted to ReportLab's bottom-left points only inside the drawing helpers. Nothing here knows about
loans or invoices: `pdf_documents` composes these pieces into each document type.
"""

import io
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

BRAND = (255, 108, 55)
INK = (24, 24, 27)
MUTED = (113, 113, 122)
LINE = (228, 228, 231)
SURFACE = (247, 247, 248)
STATUS = {"paid": (22, 128, 61), "overdue": (185, 28, 28), "unpaid": (180, 83, 9)}

WIDTH, HEIGHT = A4[0] / mm, A4[1] / mm
MARGIN = 18
FOOTER_SPACE = 24
RIGHT = WIDTH - MARGIN
CONTENT_WIDTH = WIDTH - 2 * MARGIN
LABEL_SPACING = 0.35 * mm  # letter spacing of the small upper-case labels
LOGO = Path(__file__).resolve().parent / "assets" / "pingo-logo.png"


def clean(value):
    """The built-in PDF fonts cover Western European text; anything else becomes '?' rather than a broken glyph."""
    return str(value).encode("cp1252", "replace").decode("cp1252")


def _rgb(color):
    return tuple(channel / 255 for channel in color)


def line_height(size):
    """Height of one line of text in mm."""
    return size * 0.3528 * 1.15


class _NumberedCanvas(canvas.Canvas):
    """Keeps every page until the end so the footer can say 'Page 2 of 3'."""

    def __init__(self, *args, footer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._footer = footer
        self._pages = []

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for index, state in enumerate(self._pages, start=1):
            self.__dict__.update(state)
            self._draw_footer(index, total)
            super().showPage()
        super().save()

    def _draw_footer(self, index, total):
        left, note, page_label = self._footer(index, total)
        top = lambda y: (HEIGHT - y) * mm  # noqa: E731
        self.setStrokeColorRGB(*_rgb(LINE))
        self.setLineWidth(0.2 * mm)
        self.line(MARGIN * mm, top(HEIGHT - 15), RIGHT * mm, top(HEIGHT - 15))
        self.setFillColorRGB(*_rgb(MUTED))
        self.setFont("Helvetica", 7.5)
        self.drawString(MARGIN * mm, top(HEIGHT - 10), clean(left))
        self.drawString(MARGIN * mm, top(HEIGHT - 6.5), clean(note))
        self.drawRightString(RIGHT * mm, top(HEIGHT - 10), clean(page_label))


class PdfPage:
    def __init__(self, *, title, number, meta, issuer, copy, money, day, footer_note, generated, compress=True):
        self.copy = copy
        self.money = money
        self.day = day
        self.y = MARGIN
        self.number = number
        self._buffer = io.BytesIO()
        issuer_name = issuer.get("name") or "Pingo"
        self._canvas = _NumberedCanvas(
            self._buffer, pagesize=A4, pageCompression=int(compress),
            footer=lambda index, total: (f"{issuer_name} · {number} · {copy['generated'](generated)}", footer_note, copy["page"](index, total)),
        )
        self._canvas.setTitle(clean(f"{title} {number}"))
        self._canvas.setCreator("Pingo")
        self._canvas.setAuthor(clean(issuer_name))
        self._header(title, number, meta, issuer)

    # --- drawing primitives -------------------------------------------------------------------------

    def _ty(self, y):
        return (HEIGHT - y) * mm

    def text(self, x, y, value, size=9, bold=False, color=INK, align="left", spacing=0):
        value = clean(value)
        font = "Helvetica-Bold" if bold else "Helvetica"
        c = self._canvas
        c.setFillColorRGB(*_rgb(color))
        if spacing:
            width = stringWidth(value, font, size) + spacing * max(len(value) - 1, 0)
            start = x * mm - width if align == "right" else x * mm
            obj = c.beginText()
            obj.setTextOrigin(start, self._ty(y))
            obj.setFont(font, size)
            obj.setCharSpace(spacing)
            obj.textOut(value)
            obj.setCharSpace(0)  # the setting would otherwise carry over to every later string on the page
            c.drawText(obj)
            return
        c.setFont(font, size)
        draw = c.drawRightString if align == "right" else c.drawString
        draw(x * mm, self._ty(y), value)

    def label(self, value, x, y, align="left"):
        self.text(x, y, value.upper(), 7.2, True, MUTED, align, spacing=LABEL_SPACING)

    def hline(self, x1, x2, y, color=LINE, width=0.2):
        c = self._canvas
        c.setStrokeColorRGB(*_rgb(color))
        c.setLineWidth(width * mm)
        c.line(x1 * mm, self._ty(y), x2 * mm, self._ty(y))

    def box(self, x, y, width, height):
        c = self._canvas
        c.setFillColorRGB(*_rgb(SURFACE))
        c.setStrokeColorRGB(*_rgb(LINE))
        c.setLineWidth(0.2 * mm)
        c.roundRect(x * mm, self._ty(y + height), width * mm, height * mm, 2 * mm, stroke=1, fill=1)

    def wrap(self, value, width, size=9, bold=False):
        return simpleSplit(clean(value), "Helvetica-Bold" if bold else "Helvetica", size, width * mm) or [""]

    def paragraph(self, value, x, y, width, size=9, color=INK):
        """Wrapped text starting at y; returns the y below it."""
        for index, line in enumerate(self.wrap(value, width, size)):
            self.text(x, y + index * line_height(size), line, size, False, color)
        return y + len(self.wrap(value, width, size)) * line_height(size)

    def new_page(self):
        self._canvas.showPage()
        self.y = MARGIN

    def ensure_room(self, needed):
        if self.y + needed > HEIGHT - FOOTER_SPACE:
            self.new_page()

    # --- page furniture -----------------------------------------------------------------------------

    def _header(self, title, number, meta, issuer):
        y = MARGIN
        logo = LOGO.exists()
        text_x = MARGIN + 17 if logo else MARGIN
        if logo:
            self._canvas.drawImage(ImageReader(str(LOGO)), MARGIN * mm, self._ty(y - 1 + 13), 13 * mm, 13 * mm, mask="auto")
        self.text(text_x, y + 4, issuer.get("name") or "Pingo", 13, True, INK)
        lines = [value for value in (f"NUIT {issuer['nuit']}" if issuer.get("nuit") else None, issuer.get("address"), issuer.get("email")) if value]
        for index, line in enumerate(lines):
            self.text(text_x, y + 9 + index * 4, line, 8.5, False, MUTED)

        self.text(RIGHT, y + 6, title.upper(), 18 if len(title) > 10 else 22, True, INK, "right")
        rows = [[self.copy["number"], number], *meta]
        for index, (name, value) in enumerate(rows):
            row_y = y + 13 + index * 4.6
            self.text(RIGHT - 58, row_y, name, 8.5, False, MUTED)
            self.text(RIGHT, row_y, value, 8.5, True, INK, "right")
        y = max(y + 9 + len(lines) * 4, y + 13 + len(rows) * 4.6) + 3
        self.hline(MARGIN, RIGHT, y, BRAND, 0.8)
        self.y = y + 9

    def section(self, value):
        """Section heading; starts a new page when too little room is left."""
        if self.y > HEIGHT - 50:
            self.new_page()
        self.text(MARGIN, self.y, value.upper(), 8.5, True, BRAND, spacing=LABEL_SPACING)
        self.y += 3

    def parties(self, party_label, party, facts_label, facts):
        """Two columns: who the document is for, and a list of facts."""
        column = (WIDTH - MARGIN * 2 - 10) / 2
        facts_x = MARGIN + column + 10
        y = self.y
        self.label(party_label, MARGIN, y)
        self.label(facts_label, facts_x, y)
        self.text(MARGIN, y + 6, party.get("name") or "-", 11, True, INK)
        left_y = y + 11
        for line in (party.get("phone"), party.get("email"), party.get("address")):
            if not line:
                continue
            for part in self.wrap(line, column):
                self.text(MARGIN, left_y, part, 9, False, MUTED)
                left_y += 4.2
        for index, fact in enumerate(facts):
            name, value = fact[0], fact[1]
            color = fact[2] if len(fact) > 2 else INK
            row_y = y + 6 + index * 4.8
            self.text(facts_x, row_y, name, 9, False, MUTED)
            self.text(RIGHT, row_y, value, 9, True, color, "right")
        self.y = max(left_y, y + 6 + len(facts) * 4.8) + 6

    def table(self, head, rows, widths, aligns=None, size=9, head_size=7.5, pad_y=2.6, pad_x=3):
        """Header row plus body rows. A cell is text, or (text, color) for emphasised text.

        Rows that do not fit move to a new page, and the header is repeated there."""
        aligns = aligns or ["left"] * len(widths)
        edges = [MARGIN]
        for width in widths:
            edges.append(edges[-1] + width)

        def draw_head():
            height = pad_y * 2 + line_height(head_size)
            c = self._canvas
            c.setFillColorRGB(*_rgb(SURFACE))
            c.rect(MARGIN * mm, self._ty(self.y + height), (edges[-1] - MARGIN) * mm, height * mm, stroke=0, fill=1)
            for index, cell in enumerate(head):
                baseline = self.y + pad_y + line_height(head_size) * 0.8
                if aligns[index] == "right":
                    self.text(edges[index + 1] - pad_x, baseline, cell, head_size, True, MUTED, "right")
                else:
                    self.text(edges[index] + pad_x, baseline, cell, head_size, True, MUTED)
            self.hline(MARGIN, edges[-1], self.y + height, LINE, 0.3)
            self.y += height

        draw_head()
        for row in rows:
            cells = [cell if isinstance(cell, tuple) else (cell, None) for cell in row]
            wrapped = [self.wrap(text, widths[i] - 2 * pad_x, size, bold=color is not None) for i, (text, color) in enumerate(cells)]
            height = pad_y * 2 + max(len(lines) for lines in wrapped) * line_height(size)
            if self.y + height > HEIGHT - FOOTER_SPACE:
                self.new_page()
                draw_head()
            for index, (lines, (_, color)) in enumerate(zip(wrapped, cells)):
                for offset, part in enumerate(lines):
                    baseline = self.y + pad_y + line_height(size) * 0.8 + offset * line_height(size)
                    if aligns[index] == "right":
                        self.text(edges[index + 1] - pad_x, baseline, part, size, color is not None, color or INK, "right")
                    else:
                        self.text(edges[index] + pad_x, baseline, part, size, color is not None, color or INK)
            self.hline(MARGIN, edges[-1], self.y + height, LINE, 0.2)
            self.y += height
        self.y += 9

    def totals(self, rows, final_label, final_value, currency, final_color=BRAND):
        """Label/value rows in the right-hand column, ending with an emphasised total."""
        self.ensure_room(34)
        box_x = RIGHT - 84
        for index, (name, value) in enumerate(rows):
            row_y = self.y + 5 + index * 6
            self.text(box_x, row_y, name, 9.5, False, MUTED)
            self.text(RIGHT, row_y, self.money(value, currency), 9.5, True, INK, "right")
        final_y = self.y + 5 + len(rows) * 6 + 2
        self.hline(box_x, RIGHT, final_y - 4, INK, 0.4)
        self.text(box_x, final_y + 2, final_label, 11.5, True, INK)
        self.text(RIGHT, final_y + 2, self.money(final_value, currency), 11.5, True, final_color, "right")
        self.y = final_y + 10

    def finish(self):
        self._canvas.showPage()  # keep the last page; the footers are stamped on all pages in save()
        self._canvas.save()
        return self._buffer.getvalue()
