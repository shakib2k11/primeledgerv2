"""Shared presentation primitives for Prime Ledger PDF delivery adapters."""

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from django.utils import timezone


INK = HexColor("#202927")
INK_SOFT = HexColor("#52605D")
MUTED = HexColor("#78827F")
BORDER = HexColor("#DFE3DF")
BORDER_STRONG = HexColor("#CBD2CD")
TEAL = HexColor("#176F67")
TEAL_SOFT = HexColor("#E8F2EF")
SURFACE_SUBTLE = HexColor("#F7F9F7")
WHITE = HexColor("#FFFFFF")
AMBER = HexColor("#8A641C")

PAGE_MARGIN = 40


def clean_text(value, limit=100):
    return " ".join(str(value or "").split())[:limit]


def draw_report_header(
    pdf,
    business,
    title,
    *,
    page_size=A4,
    page_number=1,
    metadata=(),
):
    width, height = page_size
    pdf.setFillColor(TEAL)
    pdf.rect(0, height - 7, width, 7, stroke=0, fill=1)

    mark_y = height - 51
    pdf.setFillColor(TEAL_SOFT)
    pdf.roundRect(PAGE_MARGIN, mark_y - 5, 31, 31, 5, stroke=0, fill=1)
    pdf.setFillColor(TEAL)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(PAGE_MARGIN + 15.5, mark_y + 6, "PL")

    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(PAGE_MARGIN + 42, mark_y + 12, clean_text(business.name, 62))
    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 7.5)
    identity = clean_text(getattr(business, "address", ""), 75) or "Business records"
    pdf.drawString(PAGE_MARGIN + 42, mark_y - 1, identity)

    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawRightString(width - PAGE_MARGIN, mark_y + 17, "PRIME LEDGER REPORT")
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawRightString(width - PAGE_MARGIN, mark_y - 1, clean_text(title, 48))

    rule_y = mark_y - 17
    pdf.setStrokeColor(BORDER_STRONG)
    pdf.setLineWidth(.7)
    pdf.line(PAGE_MARGIN, rule_y, width - PAGE_MARGIN, rule_y)

    metadata = list(metadata)
    metadata.append(("Generated", f"{timezone.localdate():%d %b %Y}"))
    if metadata:
        column_width = (width - (2 * PAGE_MARGIN)) / len(metadata)
        label_y = rule_y - 17
        value_y = label_y - 12
        for index, (label, value) in enumerate(metadata):
            x = PAGE_MARGIN + (index * column_width)
            if index:
                pdf.setStrokeColor(BORDER)
                pdf.line(x - 10, label_y + 4, x - 10, value_y - 3)
            pdf.setFillColor(MUTED)
            pdf.setFont("Helvetica-Bold", 6.5)
            pdf.drawString(x, label_y, clean_text(label, 24).upper())
            pdf.setFillColor(INK_SOFT)
            pdf.setFont("Helvetica", 8)
            pdf.drawString(x, value_y, clean_text(value, 42))
        return width, height, value_y - 23
    return width, height, rule_y - 22


def draw_document_header(
    pdf,
    business,
    document_type,
    document_number,
    document_date,
    *,
    page_size=A4,
    status=None,
):
    width, height = page_size
    pdf.setFillColor(TEAL)
    pdf.rect(0, height - 7, width, 7, stroke=0, fill=1)

    top_y = height - 48
    pdf.setFillColor(TEAL_SOFT)
    pdf.roundRect(PAGE_MARGIN, top_y - 6, 33, 33, 5, stroke=0, fill=1)
    pdf.setFillColor(TEAL)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(PAGE_MARGIN + 16.5, top_y + 6, "PL")
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(PAGE_MARGIN + 44, top_y + 13, clean_text(business.name, 55))
    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 7.5)
    pdf.drawString(
        PAGE_MARGIN + 44,
        top_y - 1,
        clean_text(getattr(business, "address", ""), 68) or "Generated business document",
    )

    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawRightString(width - PAGE_MARGIN, top_y + 19, clean_text(document_type, 35).upper())
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawRightString(width - PAGE_MARGIN, top_y + 2, clean_text(document_number, 30))
    pdf.setFillColor(INK_SOFT)
    pdf.setFont("Helvetica", 8)
    detail = f"{document_date:%d %b %Y}"
    if status:
        detail += f"  /  {status}"
    pdf.drawRightString(width - PAGE_MARGIN, top_y - 12, detail)

    y = top_y - 30
    pdf.setStrokeColor(BORDER_STRONG)
    pdf.line(PAGE_MARGIN, y, width - PAGE_MARGIN, y)
    return width, height, y - 24


def draw_table_header(pdf, y, columns, *, width, margin=PAGE_MARGIN):
    pdf.setFillColor(TEAL_SOFT)
    pdf.rect(margin, y - 7, width - (2 * margin), 23, stroke=0, fill=1)
    pdf.setStrokeColor(BORDER)
    pdf.line(margin, y - 7, width - margin, y - 7)
    pdf.setFillColor(TEAL)
    pdf.setFont("Helvetica-Bold", 6.8)
    for x, label, alignment in columns:
        if alignment == "right":
            pdf.drawRightString(x, y + 1, clean_text(label, 28).upper())
        else:
            pdf.drawString(x, y + 1, clean_text(label, 28).upper())
    return y - 22


def draw_table_row_background(pdf, y, *, width, row_index, margin=PAGE_MARGIN, height=20):
    if row_index % 2:
        pdf.setFillColor(SURFACE_SUBTLE)
        pdf.rect(margin, y - 6, width - (2 * margin), height, stroke=0, fill=1)
    pdf.setStrokeColor(BORDER)
    pdf.setLineWidth(.35)
    pdf.line(margin, y - 6, width - margin, y - 6)


def draw_empty_state(pdf, y, message, *, width, margin=PAGE_MARGIN):
    pdf.setFillColor(SURFACE_SUBTLE)
    pdf.setStrokeColor(BORDER)
    pdf.roundRect(margin, y - 52, width - (2 * margin), 62, 4, stroke=1, fill=1)
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawCentredString(width / 2, y - 17, clean_text(message, 90))
    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 7.5)
    pdf.drawCentredString(width / 2, y - 33, "No rows were available for the selected filters.")
    return y - 67


def draw_report_total(pdf, y, label, amount, *, width, currency, margin=PAGE_MARGIN):
    box_width = 215
    x = width - margin - box_width
    pdf.setFillColor(TEAL_SOFT)
    pdf.roundRect(x, y - 18, box_width, 38, 4, stroke=0, fill=1)
    pdf.setFillColor(TEAL)
    pdf.setFont("Helvetica-Bold", 7)
    pdf.drawString(x + 12, y + 5, clean_text(label, 32).upper())
    pdf.setFillColor(INK)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawRightString(x + box_width - 12, y + 3, f"{amount:.2f} {currency}")
    return y - 32


def draw_page_footer(pdf, *, width, page_number, note="Generated by Prime Ledger"):
    y = 27
    pdf.setStrokeColor(BORDER)
    pdf.setLineWidth(.5)
    pdf.line(PAGE_MARGIN, y + 10, width - PAGE_MARGIN, y + 10)
    pdf.setFillColor(MUTED)
    pdf.setFont("Helvetica", 6.8)
    pdf.drawString(PAGE_MARGIN, y, clean_text(note, 90))
    pdf.drawRightString(width - PAGE_MARGIN, y, f"Page {page_number}")
