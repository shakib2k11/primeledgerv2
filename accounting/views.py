import csv
from decimal import Decimal
from io import BytesIO

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.formats import date_format
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from accounting.forms import (
    AccountForm,
    ExpensePaymentForm,
    ExpenseRecordForm,
    FiscalPeriodForm,
    JournalEntryForm,
    JournalLineFormSet,
    VoucherForm,
)
from accounting.models import (
    Account, ExpensePayment, ExpenseRecord, FiscalPeriod, JournalEntry, Voucher,
)
from accounting.models import ChartOfAccountsTemplate
from accounting.application.services import (
    ApplyAccountTemplateCommand,
    CreateMoneyReceiptCommand,
    CreateExpenseCommand,
    PayExpenseCommand,
    apply_account_template,
    create_expense,
    create_money_receipt,
    pay_expense,
)
from accounting.infrastructure.repositories import (
    DjangoAccountTemplateRepository,
    DjangoExpensePaymentRepository,
    DjangoExpenseRepository,
    DjangoMoneyReceiptRepository,
)
from core.application.services import (
    ACCOUNTING_MANAGE,
    ACCOUNTING_POST,
    ACCOUNTING_VIEW,
    PostJournalCommand,
    post_journal,
)
from core.infrastructure.repositories import DjangoJournalRepository
from core.views import authorize, request_business
from core.pdf import (
    BORDER, INK, INK_SOFT, MUTED, PAGE_MARGIN, SURFACE_SUBTLE, TEAL,
    clean_text, draw_document_header, draw_empty_state, draw_page_footer,
    draw_report_header, draw_table_header, draw_table_row_background,
    pdf_font,
)
from django.utils.translation import gettext as _


def accounting_context(request, permission):
    business = request_business(request)
    if business is not None:
        authorize(request.user, business, permission)
    return business


@login_required
def accounting_overview(request):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    entries = JournalEntry.objects.filter(business=business)
    vouchers = Voucher.objects.filter(business=business)
    context = {
        "business": business,
        "account_count": Account.objects.filter(business=business, is_active=True).count(),
        "period_count": FiscalPeriod.objects.filter(business=business).count(),
        "draft_count": entries.filter(posted=False).count(),
        "posted_count": entries.filter(posted=True).count(),
        "voucher_total": vouchers.aggregate(total=Sum("total"))["total"] or 0,
        "recent_entries": entries.select_related("period").prefetch_related("lines").order_by("-entry_date", "-id")[:8],
    }
    return render(request, "accounting/overview.html", context)


@login_required
def account_list(request):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    query = request.GET.get("q", "").strip()
    accounts = Account.objects.filter(business=business)
    if query:
        accounts = accounts.filter(Q(code__icontains=query) | Q(name__icontains=query))
    page = Paginator(accounts.order_by("code"), 30).get_page(request.GET.get("page"))
    return render(request, "accounting/account-list.html", {
        "business": business, "accounts": page, "page_obj": page, "query": query
    })


@login_required
def account_create(request):
    business = accounting_context(request, ACCOUNTING_MANAGE)
    if business is None:
        return render(request, "core/no-business.html")
    form = AccountForm(request.POST or None, business=business)
    if request.method == "POST" and form.is_valid():
        account = form.save(commit=False)
        account.business = business
        account.full_clean()
        account.save()
        messages.success(request, _("Account added to the chart of accounts."))
        return redirect("account-list")
    return render(request, "core/record-form.html", {
        "business": business, "form": form, "title": _("Add account"),
        "eyebrow": _("Chart of accounts"), "description": _("Use a stable code and the correct accounting classification."),
        "cancel_url": "account-list", "submit_label": _("Save account"),
    })


@login_required
@transaction.atomic
def account_template_apply(request):
    business = accounting_context(request, ACCOUNTING_MANAGE)
    if business is None:
        return render(request, "core/no-business.html")
    templates = ChartOfAccountsTemplate.objects.filter(is_active=True).prefetch_related("lines")
    if request.method == "POST":
        template = get_object_or_404(templates, pk=request.POST.get("template"))
        try:
            result = apply_account_template(
                ApplyAccountTemplateCommand(
                    template_id=template.pk,
                    business_id=business.pk,
                    user_id=request.user.pk,
                ),
                DjangoAccountTemplateRepository(),
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(
                request,
                _("%(template)s applied: %(created)s created, %(matched)s matched.")
                % {
                    "template": template.name,
                    "created": result.created,
                    "matched": result.matched,
                },
            )
            return redirect("account-list")
    return render(request, "accounting/account-template-apply.html", {
        "business": business,
        "templates": templates,
    })


@login_required
def period_list(request):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    periods = FiscalPeriod.objects.filter(business=business).order_by("-starts_on")
    return render(request, "accounting/period-list.html", {"business": business, "periods": periods})


@login_required
def period_create(request):
    business = accounting_context(request, ACCOUNTING_MANAGE)
    if business is None:
        return render(request, "core/no-business.html")
    period = FiscalPeriod(business=business)
    form = FiscalPeriodForm(request.POST or None, instance=period)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Fiscal period created successfully."))
        return redirect("period-list")
    return render(request, "core/record-form.html", {
        "business": business, "form": form, "title": _("Create fiscal period"),
        "eyebrow": _("Accounting controls"), "description": _("Periods cannot overlap and locked periods reject new postings."),
        "cancel_url": "period-list", "submit_label": _("Create period"),
    })


@login_required
def period_edit(request, pk):
    business = accounting_context(request, ACCOUNTING_MANAGE)
    if business is None:
        return render(request, "core/no-business.html")
    period = get_object_or_404(FiscalPeriod, pk=pk, business=business)
    form = FiscalPeriodForm(request.POST or None, instance=period)
    if request.method == "POST" and form.is_valid():
        period = form.save(commit=False)
        period.business = business
        period.full_clean()
        period.save()
        messages.success(request, _("Fiscal period updated successfully."))
        return redirect("period-list")
    return render(request, "core/record-form.html", {
        "business": business,
        "form": form,
        "title": _("Edit %(period)s") % {"period": period.name},
        "eyebrow": _("Accounting controls"),
        "description": (
            _("This period is locked. Its name may be updated, but its boundaries remain protected.")
            if period.is_locked
            else _("Update the name or boundaries without overlapping another period or excluding existing entries.")
        ),
        "cancel_url": "period-list",
        "submit_label": _("Save changes"),
    })


@login_required
@transaction.atomic
def period_toggle_lock(request, pk):
    business = accounting_context(request, ACCOUNTING_MANAGE)
    if business is None:
        return render(request, "core/no-business.html")
    period = get_object_or_404(FiscalPeriod, pk=pk, business=business)
    if request.method == "GET":
        action = _("reopen") if period.is_locked else _("lock")
        return render(request, "core/confirmation.html", {
            "business": business,
            "eyebrow": _("Accounting control"),
            "title": _("%(action)s %(period)s?") % {
                "action": action.title(), "period": period.name,
            },
            "description": (
                _("Reopening permits new postings in this period.")
                if period.is_locked
                else _("Locking rejects new postings and edits to its financial records.")
            ),
            "confirmation_text": _("I understand the effect and want to %(action)s this period.") % {"action": action},
            "submit_label": _("%(action)s period") % {"action": action.title()},
            "cancel_url": "period-list",
        })
    if request.method != "POST" or request.POST.get("confirm") != "yes":
        messages.error(request, _("Confirm the period status change before continuing."))
        return redirect("period-list")
    period.is_locked = not period.is_locked
    period.full_clean()
    period.save(update_fields=["is_locked"])
    state = _("locked") if period.is_locked else _("open")
    messages.success(
        request,
        _("%(period)s is now %(state)s.") % {"period": period.name, "state": state},
    )
    return redirect("period-list")


@login_required
def journal_list(request):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    query = request.GET.get("q", "").strip()
    state = request.GET.get("state", "")
    entries = JournalEntry.objects.filter(business=business).select_related("period").prefetch_related("lines")
    if query:
        entries = entries.filter(Q(reference__icontains=query) | Q(description__icontains=query))
    if state == "posted":
        entries = entries.filter(posted=True)
    elif state == "draft":
        entries = entries.filter(posted=False)
    page = Paginator(entries.order_by("-entry_date", "-id"), 25).get_page(request.GET.get("page"))
    return render(request, "accounting/journal-list.html", {
        "business": business, "entries": page, "page_obj": page, "query": query, "state": state
    })


def _journal_form(request, business, entry, title):
    form = JournalEntryForm(request.POST or None, instance=entry, business=business)
    formset = JournalLineFormSet(
        request.POST or None,
        instance=entry,
        prefix="lines",
        form_kwargs={"business": business},
    )
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            entry = form.save(commit=False)
            entry.business = business
            entry.created_by = entry.created_by or request.user
            entry.full_clean()
            entry.save()
            formset.instance = entry
            formset.save()
        messages.success(request, _("Draft journal saved successfully."))
        return redirect("journal-detail", pk=entry.pk)
    return render(request, "accounting/journal-form.html", {
        "business": business, "form": form, "formset": formset, "title": title
    })


@login_required
def journal_create(request):
    business = accounting_context(request, ACCOUNTING_MANAGE)
    if business is None:
        return render(request, "core/no-business.html")
    return _journal_form(
        request, business, JournalEntry(business=business, created_by=request.user), "New journal entry"
    )


@login_required
def journal_edit(request, pk):
    business = accounting_context(request, ACCOUNTING_MANAGE)
    if business is None:
        return render(request, "core/no-business.html")
    entry = get_object_or_404(JournalEntry, pk=pk, business=business)
    if entry.posted or entry.period.is_locked:
        messages.error(request, _("Posted or locked journal entries cannot be edited."))
        return redirect("journal-detail", pk=entry.pk)
    return _journal_form(request, business, entry, "Edit draft journal")


@login_required
def journal_detail(request, pk):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    entry = get_object_or_404(
        JournalEntry.objects.select_related("period", "created_by").prefetch_related("lines__account", "lines__party"),
        pk=pk,
        business=business,
    )
    return render(request, "accounting/journal-detail.html", {"business": business, "entry": entry})


@login_required
@transaction.atomic
def journal_post(request, pk):
    business = accounting_context(request, ACCOUNTING_POST)
    if business is None:
        return render(request, "core/no-business.html")
    if request.method != "POST" or request.POST.get("confirm") != "yes":
        messages.error(request, _("Confirm posting before continuing."))
        return redirect("journal-detail", pk=pk)
    try:
        post_journal(
            PostJournalCommand(entry_id=pk, business_id=business.pk), DjangoJournalRepository()
        )
    except JournalEntry.DoesNotExist:
        get_object_or_404(JournalEntry, pk=pk, business=business)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("journal-detail", pk=pk)
    messages.success(request, _("Journal entry posted. Financial history is now locked."))
    return redirect("journal-detail", pk=pk)


@login_required
def voucher_list(request):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    vouchers = Voucher.objects.filter(business=business).select_related("party", "journal_entry")
    page = Paginator(vouchers.order_by("-voucher_date", "-id"), 25).get_page(request.GET.get("page"))
    return render(request, "accounting/voucher-list.html", {
        "business": business, "vouchers": page, "page_obj": page
    })


@login_required
@transaction.atomic
def voucher_create(request):
    business = accounting_context(request, ACCOUNTING_MANAGE)
    if business is None:
        return render(request, "core/no-business.html")
    form = VoucherForm(request.POST or None, business=business)
    if request.method == "POST" and form.is_valid():
        voucher = form.save(commit=False)
        voucher.business = business
        voucher.total = voucher.journal_entry.total_debit
        voucher.voucher_date = voucher.journal_entry.entry_date
        voucher.full_clean()
        voucher.save()
        receipt_result = None
        if voucher.voucher_type == Voucher.Type.RECEIPT:
            receipt_result = create_money_receipt(
                CreateMoneyReceiptCommand(
                    voucher_id=voucher.pk,
                    preferred_number=voucher.number,
                ),
                DjangoMoneyReceiptRepository(),
            )
        if receipt_result:
            message = _(
                "Voucher created from the posted journal. Money receipt %(number)s is ready."
            ) % {"number": receipt_result.number}
        else:
            message = _("Voucher created from the posted journal.")
        messages.success(request, message)
        return redirect("voucher-list")
    return render(request, "core/record-form.html", {
        "business": business, "form": form, "title": _("Create voucher"),
        "eyebrow": _("Financial documents"), "description": _("Create an immutable voucher from an unassigned posted journal entry."),
        "cancel_url": "voucher-list", "submit_label": _("Create voucher"),
    })


def _expense_queryset(business):
    return ExpenseRecord.objects.filter(business=business).select_related(
        "payee", "expense_account", "payment_account", "payable_account",
        "journal_entry", "voucher", "created_by",
    ).prefetch_related("payments__payment_account", "payments__voucher")


def _filtered_expenses(request, business):
    expenses = _expense_queryset(business)
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "")
    status_filter = request.GET.get("status", "")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    if query:
        expenses = expenses.filter(
            Q(number__icontains=query)
            | Q(description__icontains=query)
            | Q(payee__name__icontains=query)
            | Q(external_reference__icontains=query)
        )
    if category.isdigit():
        expenses = expenses.filter(expense_account_id=category)
    if date_from:
        expenses = expenses.filter(expense_date__gte=date_from)
    if date_to:
        expenses = expenses.filter(expense_date__lte=date_to)
    rows = list(expenses)
    if status_filter in {"paid", "partial", "unpaid"}:
        rows = [row for row in rows if row.payment_status == status_filter]
    return rows, {
        "query": query, "category": category, "status_filter": status_filter,
        "date_from": date_from, "date_to": date_to,
    }


@login_required
def expense_list(request):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    rows, filters = _filtered_expenses(request, business)
    page = Paginator(rows, 30).get_page(request.GET.get("page"))
    return render(request, "accounting/expense-list.html", {
        "business": business,
        "expenses": page,
        "page_obj": page,
        "categories": Account.objects.filter(
            business=business, is_active=True, account_type=Account.Type.EXPENSE
        ).order_by("code"),
        "expense_total": sum((row.amount for row in rows), Decimal("0.00")),
        "paid_total": sum((row.paid_amount for row in rows), Decimal("0.00")),
        "outstanding_total": sum((row.balance_due for row in rows), Decimal("0.00")),
        **filters,
    })


@login_required
def expense_create(request):
    business = accounting_context(request, ACCOUNTING_POST)
    if business is None:
        return render(request, "core/no-business.html")
    form = ExpenseRecordForm(request.POST or None, business=business)
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        try:
            expense = create_expense(
                CreateExpenseCommand(
                    business_id=business.pk,
                    expense_date=values["expense_date"],
                    expense_account_id=values["expense_account"].pk,
                    settlement=values["settlement"],
                    amount=values["amount"],
                    description=values["description"],
                    idempotency_key=values["idempotency_key"],
                    payee_id=values["payee"].pk if values.get("payee") else None,
                    payment_account_id=(
                        values["payment_account"].pk
                        if values.get("payment_account") else None
                    ),
                    external_reference=values["external_reference"],
                    user_id=request.user.pk,
                ),
                DjangoExpenseRepository(),
            )
        except (ValidationError, IntegrityError) as exc:
            detail = (
                " ".join(exc.messages) if isinstance(exc, ValidationError)
                else "The expense could not be posted because a financial reference already exists."
            )
            form.add_error(None, detail)
        else:
            messages.success(
                request,
                _("Expense %(expense)s posted with voucher %(voucher)s.")
                % {"expense": expense.number, "voucher": expense.voucher.number},
            )
            return redirect("expense-detail", pk=expense.pk)
    return render(request, "accounting/expense-form.html", {
        "business": business, "form": form,
    })


@login_required
def expense_detail(request, pk):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    expense = get_object_or_404(_expense_queryset(business), pk=pk)
    return render(request, "accounting/expense-detail.html", {
        "business": business, "expense": expense,
    })


@login_required
def expense_pay(request, pk):
    business = accounting_context(request, ACCOUNTING_POST)
    if business is None:
        return render(request, "core/no-business.html")
    expense = get_object_or_404(_expense_queryset(business), pk=pk)
    if not expense.can_pay:
        messages.error(request, _("This expense has no outstanding payable balance."))
        return redirect("expense-detail", pk=pk)
    form = ExpensePaymentForm(
        request.POST or None, business=business, expense=expense
    )
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        try:
            payment = pay_expense(
                PayExpenseCommand(
                    expense_id=expense.pk,
                    business_id=business.pk,
                    payment_account_id=values["payment_account"].pk,
                    amount=values["amount"],
                    payment_date=values["payment_date"],
                    idempotency_key=values["idempotency_key"],
                    notes=values["notes"],
                    user_id=request.user.pk,
                ),
                DjangoExpensePaymentRepository(),
            )
        except (ValidationError, IntegrityError) as exc:
            detail = (
                " ".join(exc.messages) if isinstance(exc, ValidationError)
                else "The payment could not be posted because a financial reference already exists."
            )
            form.add_error(None, detail)
        else:
            messages.success(
                request,
                _("Payment %(payment)s posted with voucher %(voucher)s.")
                % {"payment": payment.number, "voucher": payment.voucher.number},
            )
            return redirect("expense-detail", pk=expense.pk)
    return render(request, "accounting/expense-payment-form.html", {
        "business": business, "expense": expense, "form": form,
    })


@login_required
def expense_csv(request):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    rows, _filters = _filtered_expenses(request, business)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="expense-register.csv"'
    writer = csv.writer(response)
    writer.writerow([business.name, _("Expense register"), business.currency])
    writer.writerow([
        _("Number"), _("Date"), _("Category"), _("Description"), _("Payee"), _("Reference"),
        _("Settlement"), _("Amount"), _("Paid"), _("Outstanding"), _("Status"),
    ])
    for expense in rows:
        writer.writerow([
            expense.number, expense.expense_date, expense.expense_account.name,
            expense.description, expense.payee.name if expense.payee else "",
            expense.external_reference, expense.get_settlement_display(), expense.amount,
            expense.paid_amount, expense.balance_due, expense.payment_status,
        ])
    return response


@login_required
def expense_report_pdf(request):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    rows, filters = _filtered_expenses(request, business)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, height = A4
    page_number = 1

    def page_header():
        _width, _height, header_y = draw_report_header(
            pdf, business, "Expense register",
            page_number=page_number,
            metadata=[
                ("From", filters["date_from"] or "Beginning"),
                ("To", filters["date_to"] or "Today"),
                ("Currency", business.currency),
            ],
        )
        return header_y

    y = page_header()
    columns = [
        (PAGE_MARGIN + 7, "Expense", "left"),
        (PAGE_MARGIN + 82, "Date", "left"),
        (PAGE_MARGIN + 150, _("Category / payee"), "left"),
        (width - PAGE_MARGIN - 110, "Amount", "right"),
        (width - PAGE_MARGIN - 7, "Outstanding", "right"),
    ]
    y = draw_table_header(pdf, y, columns, width=width)
    if not rows:
        y = draw_empty_state(pdf, y, _("No expenses matched the selected filters."), width=width)
    for index, expense in enumerate(rows):
        if y < 70:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            y = page_header()
            y = draw_table_header(pdf, y, columns, width=width)
        draw_table_row_background(pdf, y, width=width, row_index=index, height=26)
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.5)
        pdf.drawString(PAGE_MARGIN + 7, y + 2, expense.number)
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 7)
        pdf.drawString(PAGE_MARGIN + 82, y + 2, date_format(expense.expense_date, "DATE_FORMAT"))
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.2)
        pdf.drawString(PAGE_MARGIN + 150, y + 4, clean_text(expense.expense_account.name, 34))
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(), 6.6)
        pdf.drawString(
            PAGE_MARGIN + 150, y - 6,
            clean_text(expense.payee.name if expense.payee else expense.description, 42),
        )
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.5)
        pdf.drawRightString(width - PAGE_MARGIN - 110, y + 2, f"{expense.amount:,.2f}")
        pdf.drawRightString(width - PAGE_MARGIN - 7, y + 2, f"{expense.balance_due:,.2f}")
        y -= 27
    draw_page_footer(pdf, width=width, page_number=page_number)
    pdf.save()
    return HttpResponse(
        buffer.getvalue(), content_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="expense-register.pdf"'},
    )


def _expense_document_response(business, title, number, document_date, payee, amount, rows):
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, _height = A4
    pdf.setTitle(f"{title} {number}")
    width, _height, y = draw_document_header(
        pdf, business, title, number, document_date, status=_("Posted")
    )
    pdf.setFillColor(SURFACE_SUBTLE)
    pdf.setStrokeColor(BORDER)
    pdf.roundRect(PAGE_MARGIN, y - 64, width - (2 * PAGE_MARGIN), 70, 5, stroke=1, fill=1)
    pdf.setFillColor(MUTED)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawString(PAGE_MARGIN + 14, y - 14, _("PAID / PAYABLE TO"))
    pdf.setFillColor(INK)
    pdf.setFont(pdf_font(bold=True), 12)
    pdf.drawString(PAGE_MARGIN + 14, y - 36, clean_text(payee or _("Not specified"), 58))
    pdf.setFillColor(TEAL)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawRightString(width - PAGE_MARGIN - 14, y - 14, _("AMOUNT"))
    pdf.setFillColor(INK)
    pdf.setFont(pdf_font(bold=True), 19)
    pdf.drawRightString(width - PAGE_MARGIN - 14, y - 41, f"{business.currency} {amount:,.2f}")
    y -= 94
    for label, value in rows:
        pdf.setStrokeColor(BORDER)
        pdf.line(PAGE_MARGIN, y - 8, width - PAGE_MARGIN, y - 8)
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(), 8)
        pdf.drawString(PAGE_MARGIN, y, _(str(label)))
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(bold=True), 8)
        pdf.drawRightString(width - PAGE_MARGIN, y, clean_text(value, 70))
        y -= 26
    draw_page_footer(pdf, width=width, page_number=1)
    pdf.save()
    return HttpResponse(
        buffer.getvalue(), content_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{number}.pdf"'},
    )


@login_required
def expense_pdf(request, pk):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    expense = get_object_or_404(_expense_queryset(business), pk=pk)
    return _expense_document_response(
        business, _("Expense voucher"), expense.voucher.number, expense.expense_date,
        expense.payee.name if expense.payee else None, expense.amount,
        [
            (_("Expense category"), str(expense.expense_account)),
            (_("Description"), expense.description),
            (_("Settlement"), expense.get_settlement_display()),
            (_("Paid from / liability"), str(expense.payment_account or expense.payable_account)),
            (_("External reference"), expense.external_reference or "—"),
            (_("Journal reference"), expense.journal_entry.reference),
        ],
    )


@login_required
def expense_payment_pdf(request, pk):
    business = accounting_context(request, ACCOUNTING_VIEW)
    if business is None:
        return render(request, "core/no-business.html")
    payment = get_object_or_404(
        ExpensePayment.objects.select_related(
            "expense__payee", "payment_account", "voucher", "journal_entry"
        ), business=business, pk=pk,
    )
    return _expense_document_response(
        business, _("Expense payment voucher"), payment.voucher.number,
        payment.payment_date, payment.expense.payee.name, payment.amount,
        [
            (_("Expense"), f"{payment.expense.number} — {payment.expense.description}"),
            (_("Paid from"), str(payment.payment_account)),
            (_("Allocation"), payment.number),
            (_("Journal reference"), payment.journal_entry.reference),
            (_("Notes"), payment.notes or "—"),
        ],
    )
import csv
from decimal import Decimal
from io import BytesIO
