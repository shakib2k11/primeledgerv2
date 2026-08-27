import csv
from datetime import date
from io import BytesIO

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

from accounting.models import Voucher
from core.application.services import ACCOUNTING_VIEW, CONTACTS_VIEW, SALES_VIEW
from core.models import Party
from core.views import authorize, is_authorized, request_business
from operations.models import TradeDocument


def _report_business(request, permission):
    business = request_business(request)
    if business is not None:
        authorize(request.user, business, permission)
    return business


def _parse_date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _date_filters(request):
    return _parse_date(request.GET.get("date_from", "")), _parse_date(
        request.GET.get("date_to", "")
    )


def _date_range_label(date_from, date_to):
    return f"{date_from.isoformat() if date_from else 'All'} to {date_to.isoformat() if date_to else 'All'}"


def _write_csv_heading(writer, business, report_name, *, date_range=None):
    writer.writerow(["Business", business.name])
    writer.writerow(["Report", report_name])
    if date_range:
        writer.writerow(["Date range", date_range])
    writer.writerow(["Currency", business.currency])
    writer.writerow(["Generated", timezone.localdate().strftime("%d-%b-%Y")])
    writer.writerow([])


def _pdf_header(pdf, business, report_name, *, date_range=None, page_size=A4):
    width, height = page_size
    y = height - 42
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(38, y, business.name[:70])
    y -= 20
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(38, y, report_name)
    pdf.setFont("Helvetica", 8)
    if date_range:
        y -= 14
        pdf.drawString(38, y, f"Date range: {date_range}")
    y -= 14
    pdf.drawString(38, y, f"Currency: {business.currency}")
    y -= 14
    pdf.drawString(38, y, f"Generated: {timezone.localdate():%d-%b-%Y}")
    return width, height, y - 18


def _contact_filters(request, business):
    query = request.GET.get("q", "").strip()
    kind = request.GET.get("kind", "")
    state = request.GET.get("state", "active")
    contacts = Party.objects.filter(business=business)
    if state == "inactive":
        contacts = contacts.filter(is_active=False)
    elif state != "all":
        state = "active"
        contacts = contacts.filter(is_active=True)
    if kind == Party.Kind.CUSTOMER:
        contacts = contacts.filter(kind__in=[Party.Kind.CUSTOMER, Party.Kind.BOTH])
    elif kind == Party.Kind.SUPPLIER:
        contacts = contacts.filter(kind__in=[Party.Kind.SUPPLIER, Party.Kind.BOTH])
    elif kind == Party.Kind.BOTH:
        contacts = contacts.filter(kind=Party.Kind.BOTH)
    else:
        kind = ""
    if query:
        contacts = contacts.filter(
            Q(name__icontains=query)
            | Q(phone__icontains=query)
            | Q(email__icontains=query)
            | Q(address__icontains=query)
        )
    return contacts.order_by("name", "pk"), {
        "query": query,
        "kind": kind,
        "state": state,
    }


def _invoice_filters(request, business):
    query = request.GET.get("q", "").strip()
    date_from, date_to = _date_filters(request)
    invoices = TradeDocument.objects.filter(
        business=business,
        kind=TradeDocument.Kind.SALE,
        status=TradeDocument.Status.POSTED,
    ).select_related("party", "period")
    if query:
        invoices = invoices.filter(
            Q(number__icontains=query) | Q(party__name__icontains=query)
        )
    if date_from:
        invoices = invoices.filter(document_date__gte=date_from)
    if date_to:
        invoices = invoices.filter(document_date__lte=date_to)
    return invoices.order_by("-document_date", "-id"), {
        "query": query,
        "date_from": date_from,
        "date_to": date_to,
    }


def _receipt_filters(request, business):
    query = request.GET.get("q", "").strip()
    date_from, date_to = _date_filters(request)
    receipts = Voucher.objects.filter(
        business=business,
        voucher_type=Voucher.Type.RECEIPT,
    ).select_related("party", "journal_entry")
    if query:
        receipts = receipts.filter(
            Q(number__icontains=query)
            | Q(party__name__icontains=query)
            | Q(journal_entry__reference__icontains=query)
        )
    if date_from:
        receipts = receipts.filter(voucher_date__gte=date_from)
    if date_to:
        receipts = receipts.filter(voucher_date__lte=date_to)
    return receipts.order_by("-voucher_date", "-id"), {
        "query": query,
        "date_from": date_from,
        "date_to": date_to,
    }


@login_required
def report_index(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    if not any(
        is_authorized(request.user, business, permission)
        for permission in (CONTACTS_VIEW, SALES_VIEW, ACCOUNTING_VIEW)
    ):
        raise PermissionDenied
    return render(request, "reports/index.html", {"business": business})


@login_required
def contact_report(request):
    business = _report_business(request, CONTACTS_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    contacts, filters = _contact_filters(request, business)
    page = Paginator(contacts, 40).get_page(request.GET.get("page"))
    return render(request, "reports/contact-report.html", {
        "business": business,
        "contacts": page,
        "page_obj": page,
        **filters,
    })


@login_required
def contact_report_csv(request):
    business = _report_business(request, CONTACTS_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    contacts, _ = _contact_filters(request, business)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="contact-directory.csv"'
    writer = csv.writer(response)
    _write_csv_heading(writer, business, "Contact directory")
    writer.writerow([
        "Name", "Type", "Phone", "Email", "Address", "Opening balance",
        "Balance position", "Status",
    ])
    wrote_row = False
    for party in contacts:
        wrote_row = True
        position = (
            "Settled" if party.opening_balance == 0
            else "Payable" if party.opening_balance_is_payable
            else "Receivable"
        )
        writer.writerow([
            party.name,
            party.get_kind_display(),
            party.phone,
            party.email,
            party.address,
            party.opening_balance,
            position,
            "Active" if party.is_active else "Inactive",
        ])
    if not wrote_row:
        writer.writerow(["No records for the selected filters."])
    return response


@login_required
def contact_report_pdf(request):
    business = _report_business(request, CONTACTS_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    contacts, _ = _contact_filters(request, business)
    rows = list(contacts)
    buffer = BytesIO()
    page_size = landscape(A4)
    pdf = canvas.Canvas(buffer, pagesize=page_size, pageCompression=1)
    pdf.setTitle(f"{business.name} contact directory")
    width, height, y = _pdf_header(
        pdf, business, "Contact directory", page_size=page_size
    )

    def columns(current_y):
        pdf.setFont("Helvetica-Bold", 8)
        for x, label in ((38, "Contact"), (205, "Type"), (325, "Phone"), (430, "Email"), (610, "Opening balance"), (735, "Position")):
            pdf.drawString(x, current_y, label)
        pdf.line(38, current_y - 5, width - 38, current_y - 5)
        return current_y - 17

    y = columns(y)
    pdf.setFont("Helvetica", 8)
    if not rows:
        pdf.drawString(38, y, "No records for the selected filters.")
    for party in rows:
        if y < 42:
            pdf.showPage()
            _, _, y = _pdf_header(
                pdf, business, "Contact directory", page_size=page_size
            )
            y = columns(y)
            pdf.setFont("Helvetica", 8)
        position = (
            "Settled" if party.opening_balance == 0
            else "Payable" if party.opening_balance_is_payable
            else "Receivable"
        )
        pdf.drawString(38, y, party.name[:30])
        pdf.drawString(205, y, party.get_kind_display()[:20])
        pdf.drawString(325, y, party.phone[:18] or "-")
        pdf.drawString(430, y, party.email[:30] or "-")
        pdf.drawRightString(710, y, f"{party.opening_balance:.2f} {business.currency}")
        pdf.drawString(735, y, position)
        y -= 16
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="contact-directory.pdf"'
    return response


@login_required
def invoice_report(request):
    business = _report_business(request, SALES_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    invoices, filters = _invoice_filters(request, business)
    total = invoices.aggregate(total=Sum("total"))["total"] or 0
    page = Paginator(invoices, 40).get_page(request.GET.get("page"))
    return render(request, "reports/invoice-report.html", {
        "business": business,
        "invoices": page,
        "page_obj": page,
        "report_total": total,
        **filters,
    })


@login_required
def invoice_report_csv(request):
    business = _report_business(request, SALES_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    invoices, filters = _invoice_filters(request, business)
    date_range = _date_range_label(filters["date_from"], filters["date_to"])
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="invoice-register.csv"'
    writer = csv.writer(response)
    _write_csv_heading(writer, business, "Invoice register", date_range=date_range)
    writer.writerow([
        "Invoice number", "Date", "Customer", "Fiscal period",
        f"Subtotal ({business.currency})", f"Discount ({business.currency})",
        f"Total ({business.currency})",
    ])
    wrote_row = False
    for invoice in invoices:
        wrote_row = True
        writer.writerow([
            invoice.number,
            invoice.document_date,
            invoice.party.name,
            invoice.period.name,
            invoice.subtotal,
            invoice.discount_amount,
            invoice.total,
        ])
    if not wrote_row:
        writer.writerow(["No records for the selected filters."])
    return response


@login_required
def invoice_report_pdf(request):
    business = _report_business(request, SALES_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    invoices, filters = _invoice_filters(request, business)
    rows = list(invoices)
    date_range = _date_range_label(filters["date_from"], filters["date_to"])
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"{business.name} invoice register")
    width, height, y = _pdf_header(
        pdf, business, "Invoice register", date_range=date_range
    )

    def columns(current_y):
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(38, current_y, "Invoice")
        pdf.drawString(110, current_y, "Date")
        pdf.drawString(180, current_y, "Customer")
        pdf.drawRightString(430, current_y, "Subtotal")
        pdf.drawRightString(495, current_y, "Discount")
        pdf.drawRightString(width - 38, current_y, f"Total ({business.currency})")
        pdf.line(38, current_y - 5, width - 38, current_y - 5)
        return current_y - 17

    y = columns(y)
    pdf.setFont("Helvetica", 8)
    if not rows:
        pdf.drawString(38, y, "No records for the selected filters.")
    total = 0
    for invoice in rows:
        if y < 55:
            pdf.showPage()
            _, _, y = _pdf_header(
                pdf, business, "Invoice register", date_range=date_range
            )
            y = columns(y)
            pdf.setFont("Helvetica", 8)
        pdf.drawString(38, y, invoice.number)
        pdf.drawString(110, y, invoice.document_date.strftime("%d-%b-%Y"))
        pdf.drawString(180, y, invoice.party.name[:27])
        pdf.drawRightString(430, y, f"{invoice.subtotal:.2f}")
        pdf.drawRightString(495, y, f"{invoice.discount_amount:.2f}")
        pdf.drawRightString(width - 38, y, f"{invoice.total:.2f}")
        total += invoice.total
        y -= 16
    if rows:
        y -= 4
        pdf.line(390, y, width - 38, y)
        y -= 15
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawRightString(width - 38, y, f"Report total: {total:.2f} {business.currency}")
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="invoice-register.pdf"'
    return response


@login_required
def money_receipt_report(request):
    business = _report_business(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    receipts, filters = _receipt_filters(request, business)
    total = receipts.aggregate(total=Sum("total"))["total"] or 0
    page = Paginator(receipts, 40).get_page(request.GET.get("page"))
    return render(request, "reports/money-receipt-report.html", {
        "business": business,
        "receipts": page,
        "page_obj": page,
        "report_total": total,
        **filters,
    })


@login_required
def money_receipt_report_csv(request):
    business = _report_business(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    receipts, filters = _receipt_filters(request, business)
    date_range = _date_range_label(filters["date_from"], filters["date_to"])
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="money-receipt-register.csv"'
    writer = csv.writer(response)
    _write_csv_heading(writer, business, "Money receipt register", date_range=date_range)
    writer.writerow(["Receipt number", "Date", "Received from", "Journal reference", f"Amount ({business.currency})", "Notes"])
    wrote_row = False
    for receipt in receipts:
        wrote_row = True
        writer.writerow([
            receipt.number,
            receipt.voucher_date,
            receipt.party.name if receipt.party else "",
            receipt.journal_entry.reference,
            receipt.total,
            receipt.notes,
        ])
    if not wrote_row:
        writer.writerow(["No records for the selected filters."])
    return response


@login_required
def money_receipt_report_pdf(request):
    business = _report_business(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    receipts, filters = _receipt_filters(request, business)
    rows = list(receipts)
    date_range = _date_range_label(filters["date_from"], filters["date_to"])
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"{business.name} money receipt register")
    width, height, y = _pdf_header(
        pdf, business, "Money receipt register", date_range=date_range
    )

    def columns(current_y):
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(38, current_y, "Receipt")
        pdf.drawString(135, current_y, "Date")
        pdf.drawString(215, current_y, "Received from")
        pdf.drawString(390, current_y, "Journal")
        pdf.drawRightString(width - 38, current_y, f"Amount ({business.currency})")
        pdf.line(38, current_y - 5, width - 38, current_y - 5)
        return current_y - 17

    y = columns(y)
    pdf.setFont("Helvetica", 8)
    if not rows:
        pdf.drawString(38, y, "No records for the selected filters.")
    total = 0
    for receipt in rows:
        if y < 55:
            pdf.showPage()
            _, _, y = _pdf_header(
                pdf, business, "Money receipt register", date_range=date_range
            )
            y = columns(y)
            pdf.setFont("Helvetica", 8)
        pdf.drawString(38, y, receipt.number[:18])
        pdf.drawString(135, y, receipt.voucher_date.strftime("%d-%b-%Y"))
        pdf.drawString(215, y, (receipt.party.name if receipt.party else "-")[:28])
        pdf.drawString(390, y, receipt.journal_entry.reference[:22])
        pdf.drawRightString(width - 38, y, f"{receipt.total:.2f}")
        total += receipt.total
        y -= 16
    if rows:
        y -= 4
        pdf.line(390, y, width - 38, y)
        y -= 15
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawRightString(width - 38, y, f"Report total: {total:.2f} {business.currency}")
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="money-receipt-register.pdf"'
    return response


@login_required
def money_receipt_document_pdf(request, pk):
    business = _report_business(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    receipt = get_object_or_404(
        Voucher.objects.select_related("party", "journal_entry"),
        pk=pk,
        business=business,
        voucher_type=Voucher.Type.RECEIPT,
    )
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, height = A4
    pdf.setTitle(f"Money receipt {receipt.number}")
    y = height - 58
    pdf.setFont("Helvetica-Bold", 17)
    pdf.drawString(46, y, business.name[:65])
    if business.address:
        y -= 15
        pdf.setFont("Helvetica", 8)
        pdf.drawString(46, y, business.address.replace("\n", " ")[:90])
    y -= 34
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawCentredString(width / 2, y, "MONEY RECEIPT")
    y -= 26
    pdf.setFont("Helvetica", 9)
    pdf.drawString(46, y, f"Receipt number: {receipt.number}")
    pdf.drawRightString(width - 46, y, f"Date: {receipt.voucher_date:%d-%b-%Y}")
    y -= 30
    pdf.setFont("Helvetica", 10)
    pdf.drawString(46, y, "Received from")
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(135, y, (receipt.party.name if receipt.party else "Not specified")[:60])
    y -= 28
    pdf.setFont("Helvetica", 10)
    pdf.drawString(46, y, "Amount received")
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(135, y - 2, f"{business.currency} {receipt.total:.2f}")
    y -= 30
    pdf.setFont("Helvetica", 9)
    pdf.drawString(46, y, f"Accounting reference: {receipt.journal_entry.reference}")
    if receipt.notes:
        y -= 24
        pdf.drawString(46, y, f"Notes: {receipt.notes.replace(chr(10), ' ')[:90]}")
    y -= 70
    pdf.line(46, y, 190, y)
    pdf.line(width - 190, y, width - 46, y)
    pdf.setFont("Helvetica", 8)
    pdf.drawString(46, y - 13, "Received by")
    pdf.drawString(width - 190, y - 13, "Authorised signature")
    pdf.setFont("Helvetica-Oblique", 7)
    pdf.drawCentredString(width / 2, 35, "Generated by Prime Ledger from an immutable posted voucher")
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="money-receipt-{receipt.number}.pdf"'
    return response
