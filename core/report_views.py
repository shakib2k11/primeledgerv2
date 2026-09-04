import csv
from datetime import date
from decimal import Decimal
from io import BytesIO

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.formats import date_format
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

from accounting.application.reporting import (
    TransactionRegisterTotals,
    build_account_activity,
    build_transaction_register,
    calculate_contact_closing_balance,
)
from accounting.infrastructure.reporting import DjangoTransactionRegisterReader
from accounting.models import Account, JournalLine, MoneyReceipt, Voucher
from core.application.services import ACCOUNTING_VIEW, CONTACTS_VIEW, SALES_VIEW
from core.models import Party
from core.pdf import (
    BORDER,
    BORDER_STRONG,
    INK,
    INK_SOFT,
    MUTED,
    PAGE_MARGIN,
    SURFACE_SUBTLE,
    TEAL,
    TEAL_SOFT,
    clean_text,
    draw_document_header,
    draw_empty_state,
    draw_page_footer,
    draw_report_header,
    draw_report_total,
    draw_table_header,
    draw_table_row_background,
    pdf_font,
)
from core.views import authorize, is_authorized, request_business
from operations.models import SalePayment, SaleSetoffAllocation, TradeDocument
from django.utils.translation import gettext as _


TRANSACTION_TYPE_OPTIONS = (
    ("", "All transaction types"),
    (Voucher.Type.SALE, "Sale"),
    (Voucher.Type.PURCHASE, "Purchase"),
    (Voucher.Type.RECEIPT, "Receipt"),
    (Voucher.Type.PAYMENT, "Payment"),
    (Voucher.Type.CONTRA, "Contra"),
    (Voucher.Type.EXPENSE, "Expense"),
    (Voucher.Type.RETURN, "Return"),
    ("journal", "Journal entry"),
)


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


def _write_csv_heading(writer, business, report_name, *, date_range=None, as_of=None):
    writer.writerow([_("Business"), business.name])
    writer.writerow([_("Report"), report_name])
    if date_range:
        writer.writerow([_("Date range"), date_range])
    if as_of:
        writer.writerow([_("As of"), date_format(as_of, "SHORT_DATE_FORMAT")])
    writer.writerow([_("Currency"), business.currency])
    writer.writerow([_("Generated"), date_format(timezone.localdate(), "SHORT_DATE_FORMAT")])
    writer.writerow([])


def _pdf_header(
    pdf,
    business,
    report_name,
    *,
    date_range=None,
    page_size=A4,
    page_number=1,
):
    metadata = []
    if date_range:
        metadata.append(("Reporting period", date_range))
    metadata.append(("Currency", business.currency))
    return draw_report_header(
        pdf,
        business,
        report_name,
        page_size=page_size,
        page_number=page_number,
        metadata=metadata,
    )


def _contact_filters(request, business):
    query = request.GET.get("q", "").strip()
    kind = request.GET.get("kind", "")
    state = request.GET.get("state", "active")
    as_of = _parse_date(request.GET.get("as_of", "")) or timezone.localdate()
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
    elif kind == Party.Kind.EMPLOYEE:
        contacts = contacts.filter(kind=Party.Kind.EMPLOYEE)
    else:
        kind = ""
    if query:
        contacts = contacts.filter(
            Q(name__icontains=query)
            | Q(phone__icontains=query)
            | Q(email__icontains=query)
            | Q(address__icontains=query)
        )
    contacts = list(contacts.order_by("name", "pk"))
    party_totals = {
        item["party_id"]: (
            item["debit"] or Decimal("0.00"),
            item["credit"] or Decimal("0.00"),
        )
        for item in JournalLine.objects.filter(
            entry__business=business,
            entry__posted=True,
            entry__entry_date__lte=as_of,
            account__system_role__in=[
                Account.SystemRole.ACCOUNTS_RECEIVABLE,
                Account.SystemRole.ACCOUNTS_PAYABLE,
            ],
            party_id__in=[party.pk for party in contacts],
        ).values("party_id").annotate(
            debit=Sum("debit"),
            credit=Sum("credit"),
        )
    }
    for party in contacts:
        debit, credit = party_totals.get(
            party.pk,
            (Decimal("0.00"), Decimal("0.00")),
        )
        closing = calculate_contact_closing_balance(
            party.opening_balance,
            party.opening_balance_is_payable,
            debit,
            credit,
        )
        party.closing_balance = closing.amount
        party.closing_balance_position = closing.position
    return contacts, {
        "query": query,
        "kind": kind,
        "state": state,
        "as_of": as_of,
    }


def _invoice_filters(request, business):
    query = request.GET.get("q", "").strip()
    date_from, date_to = _date_filters(request)
    invoices = TradeDocument.objects.filter(
        business=business,
        kind=TradeDocument.Kind.SALE,
        status=TradeDocument.Status.POSTED,
    ).select_related("party", "period", "debit_account").prefetch_related(
        "payments", "sale_setoff_allocations"
    )
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
    receipts = MoneyReceipt.objects.filter(
        business=business,
    ).select_related(
        "party",
        "payment_account",
        "voucher__journal_entry__sale_payment__sale",
    )
    if query:
        receipts = receipts.filter(
            Q(number__icontains=query)
            | Q(party__name__icontains=query)
            | Q(voucher__journal_entry__reference__icontains=query)
        )
    if date_from:
        receipts = receipts.filter(receipt_date__gte=date_from)
    if date_to:
        receipts = receipts.filter(receipt_date__lte=date_to)
    return receipts.order_by("-receipt_date", "-id"), {
        "query": query,
        "date_from": date_from,
        "date_to": date_to,
    }


def _receipt_source(receipt):
    if receipt.voucher.voucher_type == Voucher.Type.SALE:
        return "Immediate sale"
    try:
        return f"Invoice payment / {receipt.voucher.journal_entry.sale_payment.sale.number}"
    except SalePayment.DoesNotExist:
        return "Receipt voucher"


def _account_activity_data(request, business):
    date_from, date_to = _date_filters(request)
    if date_from and date_to and date_from > date_to:
        return [], None, date_from, date_to, "From date cannot be after to date."

    accounts = list(Account.objects.filter(business=business).order_by("code", "pk"))
    posted_lines = JournalLine.objects.filter(
        entry__business=business,
        entry__posted=True,
    )

    def totals_by_account(queryset):
        return {
            item["account_id"]: (
                item["debit"] or Decimal("0.00"),
                item["credit"] or Decimal("0.00"),
            )
            for item in queryset.values("account_id").annotate(
                debit=Sum("debit"),
                credit=Sum("credit"),
            )
        }

    opening = (
        totals_by_account(posted_lines.filter(entry__entry_date__lt=date_from))
        if date_from
        else {}
    )
    activity = posted_lines
    if date_from:
        activity = activity.filter(entry__entry_date__gte=date_from)
    if date_to:
        activity = activity.filter(entry__entry_date__lte=date_to)
    rows, totals = build_account_activity(
        accounts,
        opening,
        totals_by_account(activity),
    )
    return rows, totals, date_from, date_to, ""


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


def _transaction_register_data(request, business):
    query = request.GET.get("q", "").strip()
    transaction_type = request.GET.get("type", "").strip()
    valid_types = {value for value, _ in TRANSACTION_TYPE_OPTIONS}
    if transaction_type not in valid_types:
        transaction_type = ""
    date_from, date_to = _date_filters(request)
    if date_from and date_to and date_from > date_to:
        return (
            [], TransactionRegisterTotals(), query, transaction_type,
            date_from, date_to, "From date cannot be after to date.",
        )
    rows, totals = build_transaction_register(
        DjangoTransactionRegisterReader(),
        business_id=business.pk,
        date_from=date_from,
        date_to=date_to,
        query=query,
        transaction_type=transaction_type,
    )
    return rows, totals, query, transaction_type, date_from, date_to, ""


@login_required
def transaction_register(request):
    business = _report_business(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    (
        rows, totals, query, transaction_type, date_from, date_to, date_error,
    ) = _transaction_register_data(request, business)
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    return render(request, "reports/transaction-register.html", {
        "business": business,
        "rows": page,
        "page_obj": page,
        "totals": totals,
        "query": query,
        "transaction_type": transaction_type,
        "transaction_types": TRANSACTION_TYPE_OPTIONS,
        "date_from": date_from,
        "date_to": date_to,
        "date_error": date_error,
        "date_range": _date_range_label(date_from, date_to),
    })


@login_required
def transaction_register_csv(request):
    business = _report_business(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    (
        rows, totals, query, transaction_type, date_from, date_to, date_error,
    ) = _transaction_register_data(request, business)
    if date_error:
        return HttpResponse(date_error, status=400, content_type="text/plain")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="transaction-register.csv"'
    writer = csv.writer(response)
    _write_csv_heading(
        writer,
        business,
        "Transaction register",
        date_range=_date_range_label(date_from, date_to),
    )
    writer.writerow([
        _("Date"), _("Transaction type"), _("Document number"), _("Journal reference"),
        _("Party"), _("Description"), f"Value ({business.currency})",
        f"Total debit ({business.currency})", f"Total credit ({business.currency})",
    ])
    for row in rows:
        writer.writerow([
            row.transaction_date,
            row.transaction_type,
            row.number,
            row.journal_reference,
            row.party_name if row.party_name != "—" else "",
            row.description,
            f"{row.amount:.2f}",
            f"{row.debit:.2f}",
            f"{row.credit:.2f}",
        ])
    if not rows:
        writer.writerow([_("No posted transactions match the selected filters.")])
    writer.writerow([])
    writer.writerow([
        _("TOTAL"), _(""), _(""), _(""), _(""), _(""),
        f"{totals.amount:.2f}", f"{totals.debit:.2f}", f"{totals.credit:.2f}",
    ])
    return response


@login_required
def transaction_register_pdf(request):
    business = _report_business(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    (
        rows, totals, query, transaction_type, date_from, date_to, date_error,
    ) = _transaction_register_data(request, business)
    if date_error:
        return HttpResponse(date_error, status=400, content_type="text/plain")
    date_range = _date_range_label(date_from, date_to)
    page_size = landscape(A4)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=page_size, pageCompression=1)
    pdf.setTitle(_("%(business)s transaction register") % {"business": business.name})
    pdf.setAuthor("Prime Ledger")
    page_number = 1
    width, _height, y = _pdf_header(
        pdf, business, "Transaction register", date_range=date_range,
        page_size=page_size, page_number=page_number,
    )

    def columns(current_y):
        return draw_table_header(
            pdf,
            current_y,
            (
                (PAGE_MARGIN, "Date", "left"),
                (105, "Type", "left"),
                (175, "Document / journal", "left"),
                (350, "Party / description", "left"),
                (635, "Value", "right"),
                (720, "Debit", "right"),
                (800, "Credit", "right"),
            ),
            width=width,
        )

    y = columns(y)
    if not rows:
        y = draw_empty_state(
            pdf, y, _("No posted transactions match the selected filters"), width=width
        )
    for row_index, row in enumerate(rows):
        if y < 68:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = _pdf_header(
                pdf, business, "Transaction register", date_range=date_range,
                page_size=page_size, page_number=page_number,
            )
            y = columns(y)
        draw_table_row_background(
            pdf, y, width=width, row_index=row_index, height=25
        )
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 7.2)
        pdf.drawString(PAGE_MARGIN, y + 2, date_format(row.transaction_date, "DATE_FORMAT"))
        pdf.setFillColor(TEAL)
        pdf.setFont(pdf_font(bold=True), 7)
        pdf.drawString(105, y + 2, clean_text(row.transaction_type, 13))
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.2)
        pdf.drawString(175, y + 4, clean_text(row.number, 26))
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(), 6.5)
        pdf.drawString(175, y - 6, clean_text(row.journal_reference, 30))
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.1)
        pdf.drawString(350, y + 4, clean_text(row.party_name, 37))
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(), 6.5)
        pdf.drawString(350, y - 6, clean_text(row.description, 50))
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.3)
        pdf.drawRightString(635, y + 1, f"{row.amount:.2f}")
        pdf.drawRightString(720, y + 1, f"{row.debit:.2f}")
        pdf.drawRightString(800, y + 1, f"{row.credit:.2f}")
        y -= 25
    if rows:
        if y < 74:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = _pdf_header(
                pdf, business, "Transaction register", date_range=date_range,
                page_size=page_size, page_number=page_number,
            )
            y = columns(y)
        pdf.setFillColor(TEAL_SOFT)
        pdf.rect(PAGE_MARGIN, y - 8, width - (2 * PAGE_MARGIN), 25, stroke=0, fill=1)
        pdf.setFillColor(TEAL)
        pdf.setFont(pdf_font(bold=True), 7.3)
        pdf.drawString(
            PAGE_MARGIN,
            y,
            _("TOTAL · %(count)s TRANSACTIONS") % {"count": totals.transaction_count},
        )
        pdf.drawRightString(635, y, f"{totals.amount:.2f}")
        pdf.drawRightString(720, y, f"{totals.debit:.2f}")
        pdf.drawRightString(800, y, f"{totals.credit:.2f}")
    draw_page_footer(pdf, width=width, page_number=page_number)
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="transaction-register.pdf"'
    return response


@login_required
def account_activity_report(request):
    business = _report_business(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    rows, totals, date_from, date_to, date_error = _account_activity_data(
        request,
        business,
    )
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    return render(request, "reports/account-activity-report.html", {
        "business": business,
        "rows": page,
        "page_obj": page,
        "totals": totals,
        "date_from": date_from,
        "date_to": date_to,
        "date_error": date_error,
        "date_range": _date_range_label(date_from, date_to),
    })


@login_required
def account_activity_report_csv(request):
    business = _report_business(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    rows, totals, date_from, date_to, date_error = _account_activity_data(
        request,
        business,
    )
    if date_error:
        return HttpResponse(date_error, status=400, content_type="text/plain")
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="account-activity.csv"'
    writer = csv.writer(response)
    _write_csv_heading(
        writer,
        business,
        "Account activity summary",
        date_range=_date_range_label(date_from, date_to),
    )
    writer.writerow([
        _("Account code"), _("Account name"), _("Account type"), _("Status"),
        f"Opening debit ({business.currency})",
        f"Opening credit ({business.currency})",
        f"Period debit ({business.currency})",
        f"Period credit ({business.currency})",
        f"Closing debit ({business.currency})",
        f"Closing credit ({business.currency})",
    ])
    for row in rows:
        writer.writerow([
            row.account.code,
            row.account.name,
            row.account.get_account_type_display(),
            "Active" if row.account.is_active else "Inactive",
            f"{row.opening_debit:.2f}",
            f"{row.opening_credit:.2f}",
            f"{row.period_debit:.2f}",
            f"{row.period_credit:.2f}",
            f"{row.closing_debit:.2f}",
            f"{row.closing_credit:.2f}",
        ])
    writer.writerow([])
    writer.writerow([
        _("TOTAL"), _(""), _(""), _(""),
        f"{totals.opening_debit:.2f}",
        f"{totals.opening_credit:.2f}",
        f"{totals.period_debit:.2f}",
        f"{totals.period_credit:.2f}",
        f"{totals.closing_debit:.2f}",
        f"{totals.closing_credit:.2f}",
    ])
    return response


@login_required
def account_activity_report_pdf(request):
    business = _report_business(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    rows, totals, date_from, date_to, date_error = _account_activity_data(
        request,
        business,
    )
    if date_error:
        return HttpResponse(date_error, status=400, content_type="text/plain")
    date_range = _date_range_label(date_from, date_to)
    page_size = landscape(A4)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=page_size, pageCompression=1)
    pdf.setTitle(_("%(business)s account activity summary") % {"business": business.name})
    pdf.setAuthor("Prime Ledger")
    page_number = 1
    width, _height, y = _pdf_header(
        pdf,
        business,
        "Account activity summary",
        date_range=date_range,
        page_size=page_size,
        page_number=page_number,
    )

    def columns(current_y):
        return draw_table_header(
            pdf,
            current_y,
            (
                (PAGE_MARGIN, "Account", "left"),
                (235, "Type", "left"),
                (385, "Opening Dr", "right"),
                (465, "Opening Cr", "right"),
                (545, "Debit", "right"),
                (625, "Credit", "right"),
                (705, "Closing Dr", "right"),
                (795, "Closing Cr", "right"),
            ),
            width=width,
        )

    y = columns(y)
    if not rows:
        y = draw_empty_state(pdf, y, _("No accounts are configured"), width=width)
    for row_index, row in enumerate(rows):
        if y < 68:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = _pdf_header(
                pdf,
                business,
                "Account activity summary",
                date_range=date_range,
                page_size=page_size,
                page_number=page_number,
            )
            y = columns(y)
        draw_table_row_background(pdf, y, width=width, row_index=row_index)
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.5)
        pdf.drawString(
            PAGE_MARGIN,
            y,
            clean_text(f"{row.account.code}  {row.account.name}", 39),
        )
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 7.3)
        pdf.drawString(235, y, str(row.account.get_account_type_display()))
        pdf.drawRightString(385, y, f"{row.opening_debit:.2f}")
        pdf.drawRightString(465, y, f"{row.opening_credit:.2f}")
        pdf.drawRightString(545, y, f"{row.period_debit:.2f}")
        pdf.drawRightString(625, y, f"{row.period_credit:.2f}")
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.3)
        pdf.drawRightString(705, y, f"{row.closing_debit:.2f}")
        pdf.drawRightString(795, y, f"{row.closing_credit:.2f}")
        y -= 20

    if rows:
        if y < 76:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = _pdf_header(
                pdf,
                business,
                "Account activity summary",
                date_range=date_range,
                page_size=page_size,
                page_number=page_number,
            )
            y = columns(y)
        pdf.setFillColor(TEAL_SOFT)
        pdf.rect(PAGE_MARGIN, y - 8, width - (2 * PAGE_MARGIN), 25, stroke=0, fill=1)
        pdf.setFillColor(TEAL)
        pdf.setFont(pdf_font(bold=True), 7.3)
        pdf.drawString(PAGE_MARGIN, y, _("TOTAL"))
        values = (
            (385, totals.opening_debit),
            (465, totals.opening_credit),
            (545, totals.period_debit),
            (625, totals.period_credit),
            (705, totals.closing_debit),
            (795, totals.closing_credit),
        )
        for x, value in values:
            pdf.drawRightString(x, y, f"{value:.2f}")
    draw_page_footer(pdf, width=width, page_number=page_number)
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="account-activity.pdf"'
    return response


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
    contacts, filters = _contact_filters(request, business)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="contact-directory.csv"'
    writer = csv.writer(response)
    _write_csv_heading(
        writer,
        business,
        "Contact directory",
        as_of=filters["as_of"],
    )
    writer.writerow([
        _("Name"), _("Type"), _("Phone"), _("Email"), _("Address"), _("Opening balance"),
        _("Opening position"), _("Closing balance"), _("Closing position"), _("Status"),
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
            party.closing_balance,
            party.closing_balance_position,
            "Active" if party.is_active else "Inactive",
        ])
    if not wrote_row:
        writer.writerow([_("No records for the selected filters.")])
    return response


@login_required
def contact_report_pdf(request):
    business = _report_business(request, CONTACTS_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    contacts, filters = _contact_filters(request, business)
    rows = list(contacts)
    buffer = BytesIO()
    page_size = landscape(A4)
    pdf = canvas.Canvas(buffer, pagesize=page_size, pageCompression=1)
    pdf.setTitle(_("%(business)s contact directory") % {"business": business.name})
    pdf.setAuthor("Prime Ledger")
    page_number = 1
    width, height, y = _pdf_header(
        pdf,
        business,
        "Contact directory",
        date_range=f"As of {filters['as_of']:%d %b %Y}",
        page_size=page_size,
        page_number=page_number,
    )

    def columns(current_y):
        return draw_table_header(
            pdf,
            current_y,
            (
                (PAGE_MARGIN, "Contact", "left"),
                (200, "Relationship", "left"),
                (300, "Phone", "left"),
                (500, _("Opening (%(currency)s)") % {"currency": business.currency}, "right"),
                (610, _("Closing (%(currency)s)") % {"currency": business.currency}, "right"),
                (650, "Position", "left"),
                (745, "Status", "left"),
            ),
            width=width,
        )

    y = columns(y)
    if not rows:
        y = draw_empty_state(
            pdf,
            y,
            _("No contacts match the selected filters"),
            width=width,
        )
    for row_index, party in enumerate(rows):
        if y < 55:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = _pdf_header(
                pdf,
                business,
                "Contact directory",
                date_range=f"As of {filters['as_of']:%d %b %Y}",
                page_size=page_size,
                page_number=page_number,
            )
            y = columns(y)
        draw_table_row_background(
            pdf,
            y,
            width=width,
            row_index=row_index,
            height=21,
        )
        position = (
            "Settled" if party.opening_balance == 0
            else "Payable" if party.opening_balance_is_payable
            else "Receivable"
        )
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.8)
        pdf.drawString(PAGE_MARGIN, y, clean_text(party.name, 31))
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 7.5)
        pdf.drawString(200, y, clean_text(party.get_kind_display(), 20))
        pdf.drawString(300, y, party.phone[:18] or "-")
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.5)
        pdf.drawRightString(500, y, f"{party.opening_balance:.2f}")
        pdf.drawRightString(610, y, f"{party.closing_balance:.2f}")
        pdf.setFillColor(
            TEAL if party.closing_balance_position == "Receivable" else INK_SOFT
        )
        pdf.setFont(pdf_font(bold=True), 7.3)
        pdf.drawString(650, y, party.closing_balance_position)
        pdf.setFillColor(INK_SOFT)
        pdf.drawString(745, y, "Active" if party.is_active else "Inactive")
        y -= 21
    draw_page_footer(pdf, width=width, page_number=page_number)
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
    immediate_paid = invoices.filter(
        debit_account__system_role__in=[
            Account.SystemRole.CASH,
            Account.SystemRole.BANK,
            Account.SystemRole.MOBILE_MONEY,
        ]
    ).aggregate(total=Sum("total"))["total"] or 0
    allocated_paid = SalePayment.objects.filter(
        business=business,
        sale__in=invoices,
    ).aggregate(total=Sum("amount"))["total"] or 0
    allocated_paid += SaleSetoffAllocation.objects.filter(
        setoff__business=business,
        sale__in=invoices,
    ).aggregate(total=Sum("amount"))["total"] or 0
    paid_total = immediate_paid + allocated_paid
    page = Paginator(invoices, 40).get_page(request.GET.get("page"))
    return render(request, "reports/invoice-report.html", {
        "business": business,
        "invoices": page,
        "page_obj": page,
        "report_total": total,
        "paid_total": paid_total,
        "balance_total": total - paid_total,
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
        _("Invoice number"), _("Date"), _("Customer"), _("Fiscal period"),
        f"Subtotal ({business.currency})", f"Discount ({business.currency})",
        f"Total ({business.currency})", f"Paid ({business.currency})",
        f"Balance ({business.currency})", _("Payment status"),
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
            invoice.paid_amount,
            invoice.balance_due,
            invoice.get_payment_status_display(),
        ])
    if not wrote_row:
        writer.writerow([_("No records for the selected filters.")])
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
    page_size = landscape(A4)
    pdf = canvas.Canvas(buffer, pagesize=page_size, pageCompression=1)
    pdf.setTitle(_("%(business)s invoice register") % {"business": business.name})
    pdf.setAuthor("Prime Ledger")
    page_number = 1
    width, height, y = _pdf_header(
        pdf,
        business,
        "Invoice register",
        date_range=date_range,
        page_size=page_size,
        page_number=page_number,
    )

    def columns(current_y):
        return draw_table_header(
            pdf,
            current_y,
            (
                (PAGE_MARGIN, "Invoice", "left"),
                (120, "Date", "left"),
                (205, "Customer", "left"),
                (500, _("Total (%(currency)s)") % {"currency": business.currency}, "right"),
                (590, "Paid", "right"),
                (680, "Balance", "right"),
                (710, "Status", "left"),
            ),
            width=width,
        )

    y = columns(y)
    if not rows:
        y = draw_empty_state(
            pdf,
            y,
            _("No posted invoices match the selected filters"),
            width=width,
        )
    total_balance = 0
    for row_index, invoice in enumerate(rows):
        if y < 72:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = _pdf_header(
                pdf,
                business,
                "Invoice register",
                date_range=date_range,
                page_size=page_size,
                page_number=page_number,
            )
            y = columns(y)
        draw_table_row_background(pdf, y, width=width, row_index=row_index)
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.7)
        pdf.drawString(PAGE_MARGIN, y, invoice.number)
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 7.5)
        pdf.drawString(120, y, date_format(invoice.document_date, "DATE_FORMAT"))
        pdf.drawString(205, y, clean_text(invoice.party.name, 32))
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.7)
        pdf.drawRightString(500, y, f"{invoice.total:.2f}")
        pdf.drawRightString(590, y, f"{invoice.paid_amount:.2f}")
        pdf.drawRightString(680, y, f"{invoice.balance_due:.2f}")
        pdf.setFillColor(TEAL if invoice.payment_status == TradeDocument.PaymentStatus.PAID else INK_SOFT)
        pdf.setFont(pdf_font(bold=True), 7.2)
        pdf.drawString(710, y, str(invoice.get_payment_status_display()))
        total_balance += invoice.balance_due
        y -= 20
    if rows:
        if y < 75:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = _pdf_header(
                pdf,
                business,
                "Invoice register",
                date_range=date_range,
                page_size=page_size,
                page_number=page_number,
            )
        y = draw_report_total(
            pdf,
            y - 3,
            "Outstanding balance",
            total_balance,
            width=width,
            currency=business.currency,
        )
    draw_page_footer(pdf, width=width, page_number=page_number)
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
    total = receipts.aggregate(total=Sum("amount"))["total"] or 0
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
    writer.writerow([_("Receipt number"), _("Date"), _("Received from"), _("Payment account"), _("Source"), _("Journal reference"), f"Amount ({business.currency})", _("Notes")])
    wrote_row = False
    for receipt in receipts:
        wrote_row = True
        writer.writerow([
            receipt.number,
            receipt.receipt_date,
            receipt.party.name if receipt.party else "",
            str(receipt.payment_account) if receipt.payment_account else "",
            _receipt_source(receipt),
            receipt.voucher.journal_entry.reference,
            receipt.amount,
            receipt.voucher.notes,
        ])
    if not wrote_row:
        writer.writerow([_("No records for the selected filters.")])
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
    pdf.setTitle(_("%(business)s money receipt register") % {"business": business.name})
    pdf.setAuthor("Prime Ledger")
    page_number = 1
    width, height, y = _pdf_header(
        pdf,
        business,
        "Money receipt register",
        date_range=date_range,
        page_number=page_number,
    )

    def columns(current_y):
        return draw_table_header(
            pdf,
            current_y,
            (
                (PAGE_MARGIN, "Receipt", "left"),
                (137, "Date", "left"),
                (218, "Received from", "left"),
                (390, _("Source / journal"), "left"),
                (width - PAGE_MARGIN, _("Amount (%(currency)s)") % {"currency": business.currency}, "right"),
            ),
            width=width,
        )

    y = columns(y)
    if not rows:
        y = draw_empty_state(
            pdf,
            y,
            _("No money receipts match the selected filters"),
            width=width,
        )
    total = 0
    for row_index, receipt in enumerate(rows):
        if y < 72:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = _pdf_header(
                pdf,
                business,
                "Money receipt register",
                date_range=date_range,
                page_number=page_number,
            )
            y = columns(y)
        draw_table_row_background(pdf, y, width=width, row_index=row_index)
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.7)
        pdf.drawString(PAGE_MARGIN, y, receipt.number[:18])
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 7.5)
        pdf.drawString(137, y, date_format(receipt.receipt_date, "DATE_FORMAT"))
        pdf.drawString(
            218,
            y,
            clean_text(receipt.party.name if receipt.party else "Not specified", 27),
        )
        source = _receipt_source(receipt)
        pdf.drawString(
            390,
            y,
            clean_text(f"{source} / {receipt.voucher.journal_entry.reference}", 29),
        )
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.7)
        pdf.drawRightString(width - 38, y, f"{receipt.amount:.2f}")
        total += receipt.amount
        y -= 20
    if rows:
        if y < 75:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = _pdf_header(
                pdf,
                business,
                "Money receipt register",
                date_range=date_range,
                page_number=page_number,
            )
        y = draw_report_total(
            pdf,
            y - 3,
            "Amount received",
            total,
            width=width,
            currency=business.currency,
        )
    draw_page_footer(pdf, width=width, page_number=page_number)
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
        MoneyReceipt.objects.select_related(
            "party",
            "payment_account",
            "voucher__journal_entry__sale_payment__sale",
        ),
        pk=pk,
        business=business,
    )
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, height = A4
    pdf.setTitle(_("Money receipt %(number)s") % {"number": receipt.number})
    pdf.setAuthor("Prime Ledger")
    width, height, y = draw_document_header(
        pdf,
        business,
        _("Money receipt"),
        receipt.number,
        receipt.receipt_date,
        status=_("Posted"),
    )

    pdf.setFillColor(SURFACE_SUBTLE)
    pdf.setStrokeColor(BORDER)
    pdf.roundRect(PAGE_MARGIN, y - 58, width - (2 * PAGE_MARGIN), 66, 5, stroke=1, fill=1)
    pdf.setFillColor(MUTED)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawString(PAGE_MARGIN + 14, y - 13, _("RECEIVED FROM"))
    pdf.setFillColor(INK)
    pdf.setFont(pdf_font(bold=True), 12)
    pdf.drawString(
        PAGE_MARGIN + 14,
        y - 34,
        clean_text(receipt.party.name if receipt.party else "Not specified", 62),
    )
    if receipt.party and receipt.party.address:
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(), 7.5)
        pdf.drawString(PAGE_MARGIN + 14, y - 48, clean_text(receipt.party.address, 78))
    y -= 82

    pdf.setFillColor(TEAL_SOFT)
    pdf.roundRect(PAGE_MARGIN, y - 58, width - (2 * PAGE_MARGIN), 64, 5, stroke=0, fill=1)
    pdf.setFillColor(TEAL)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawString(PAGE_MARGIN + 14, y - 14, _("AMOUNT RECEIVED"))
    pdf.setFillColor(INK)
    pdf.setFont(pdf_font(bold=True), 22)
    pdf.drawString(PAGE_MARGIN + 14, y - 42, f"{business.currency} {receipt.amount:,.2f}")
    pdf.setFillColor(TEAL)
    pdf.setFont(pdf_font(bold=True), 7.5)
    pdf.drawRightString(width - PAGE_MARGIN - 14, y - 34, _("RECEIVED"))
    y -= 86

    receipt_source = _receipt_source(receipt)
    detail_rows = [
        (_("Payment account"), str(receipt.payment_account) if receipt.payment_account else _("Not mapped")),
        (_("Accounting reference"), receipt.voucher.journal_entry.reference),
        (_("Receipt source"), receipt_source),
    ]
    pdf.setFillColor(MUTED)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawString(PAGE_MARGIN, y, _("RECEIPT DETAILS"))
    y -= 15
    for label, value in detail_rows:
        pdf.setStrokeColor(BORDER)
        pdf.line(PAGE_MARGIN, y - 7, width - PAGE_MARGIN, y - 7)
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(), 8)
        pdf.drawString(PAGE_MARGIN, y, _(str(label)))
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 8)
        pdf.drawRightString(width - PAGE_MARGIN, y, clean_text(value, 60))
        y -= 25

    if receipt.voucher.notes:
        y -= 10
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(bold=True), 7)
        pdf.drawString(PAGE_MARGIN, y, _("NOTES"))
        y -= 23
        pdf.setFillColor(SURFACE_SUBTLE)
        pdf.roundRect(PAGE_MARGIN, y - 23, width - (2 * PAGE_MARGIN), 40, 4, stroke=0, fill=1)
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 8)
        pdf.drawString(PAGE_MARGIN + 12, y - 5, clean_text(receipt.voucher.notes, 100))
        y -= 48

    signature_y = min(y - 72, 155)
    pdf.setStrokeColor(BORDER_STRONG)
    pdf.line(PAGE_MARGIN, signature_y, 195, signature_y)
    pdf.line(width - 195, signature_y, width - PAGE_MARGIN, signature_y)
    pdf.setFillColor(MUTED)
    pdf.setFont(pdf_font(), 7.5)
    pdf.drawString(PAGE_MARGIN, signature_y - 13, _("Received by"))
    pdf.drawString(width - 195, signature_y - 13, _("Authorised signature"))
    draw_page_footer(
        pdf,
        width=width,
        page_number=1,
        note=_("Generated from an immutable posted voucher / No manual alteration permitted"),
    )
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="money-receipt-{receipt.number}.pdf"'
    return response
