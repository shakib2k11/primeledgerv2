import csv
from decimal import Decimal
from io import BytesIO

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from accounting.models import Account, MoneyReceipt
from core.application.services import (
    ACCOUNTING_POST,
    ACCOUNTING_VIEW,
    PURCHASES_MANAGE,
    PURCHASES_POST,
    PURCHASES_VIEW,
    SALES_MANAGE,
    SALES_POST,
    SALES_VIEW,
)
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
from core.infrastructure.numbering import allocate_reference_number
from operations.application.services import (
    CreateBalanceSetoffCommand,
    PayPurchaseCommand,
    PostTradeDocumentCommand,
    ReceiveSalePaymentCommand,
    SetoffAllocationCommand,
    create_balance_setoff,
    pay_purchase,
    post_trade_document,
    receive_sale_payment,
)
from operations.forms import (
    BalanceSetoffForm,
    PayPurchaseForm,
    ReceiveSalePaymentForm,
    TradeDocumentForm,
    TradeLineFormSet,
)
from operations.infrastructure.repositories import (
    DjangoBalanceSetoffRepository,
    DjangoPurchasePaymentRepository,
    DjangoSalePaymentRepository,
    DjangoTradeDocumentRepository,
)
from operations.models import (
    BalanceSetoff,
    PurchasePayment,
    PurchaseSetoffAllocation,
    SalePayment,
    SaleSetoffAllocation,
    TradeDocument,
)
from django.utils.translation import gettext as _


def permissions_for(kind):
    if kind == TradeDocument.Kind.SALE:
        return SALES_VIEW, SALES_MANAGE, SALES_POST
    return PURCHASES_VIEW, PURCHASES_MANAGE, PURCHASES_POST


def route_name(kind, suffix):
    return f"{kind}-{suffix}"


def operational_business(request, kind, permission_index):
    business = request_business(request)
    if business:
        authorize(request.user, business, permissions_for(kind)[permission_index])
    return business


def filtered_documents(request, business, kind):
    documents = TradeDocument.objects.filter(business=business, kind=kind).select_related(
        "party", "period", "journal_entry"
    )
    query = request.GET.get("q", "").strip()
    state = request.GET.get("state", "")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    if query:
        documents = documents.filter(Q(number__icontains=query) | Q(party__name__icontains=query))
    if state in TradeDocument.Status.values:
        documents = documents.filter(status=state)
    if date_from:
        documents = documents.filter(document_date__gte=date_from)
    if date_to:
        documents = documents.filter(document_date__lte=date_to)
    return documents, {
        "query": query, "state": state, "date_from": date_from, "date_to": date_to
    }


@login_required
def document_list(request, kind):
    business = operational_business(request, kind, 0)
    if business is None:
        return render(request, "core/no-business.html")
    documents, filters = filtered_documents(request, business, kind)
    page = Paginator(documents, 25).get_page(request.GET.get("page"))
    totals = documents.filter(status=TradeDocument.Status.POSTED).aggregate(total=Sum("total"))
    return render(request, "operations/document-list.html", {
        "business": business,
        "documents": page,
        "page_obj": page,
        "kind": kind,
        "kind_label": TradeDocument.Kind(kind).label,
        "posted_total": totals["total"] or 0,
        **filters,
    })


def _document_form(request, business, kind, document, title):
    form = TradeDocumentForm(
        request.POST or None, instance=document, business=business, kind=kind
    )
    formset = TradeLineFormSet(
        request.POST or None,
        instance=document,
        prefix="lines",
        form_kwargs={"business": business, "kind": kind},
    )
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        document = form.save(commit=False)
        document.business = business
        document.kind = kind
        document.created_by = document.created_by or request.user
        proposed_subtotal = sum(
            (
                (
                    (line_form.cleaned_data.get("quantity") or Decimal("0"))
                    * (line_form.cleaned_data.get("unit_price") or Decimal("0"))
                ).quantize(Decimal("0.01"))
                for line_form in formset.forms
                if line_form.cleaned_data and not line_form.cleaned_data.get("DELETE")
            ),
            Decimal("0.00"),
        )
        try:
            document.set_totals(proposed_subtotal)
        except ValidationError as exc:
            form.add_error(
                "discount_value",
                exc.message_dict.get("discount_value", exc.messages),
            )
        else:
            with transaction.atomic():
                if not document.number:
                    document.number = allocate_reference_number(
                        business_id=business.pk,
                        occurred_on=document.document_date,
                    )
                document.full_clean()
                document.save()
                formset.instance = document
                formset.save()
                document.recalculate_total()
                document.save(update_fields=["subtotal", "discount_amount", "total"])
            messages.success(
                request,
                _("Draft %(kind)s saved.") % {
                    "kind": document.get_kind_display().lower()
                },
            )
            return redirect(route_name(kind, "detail"), pk=document.pk)
    return render(request, "operations/document-form.html", {
        "business": business,
        "kind": kind,
        "kind_label": TradeDocument.Kind(kind).label,
        "form": form,
        "formset": formset,
        "title": title,
    })


@login_required
def document_create(request, kind):
    business = operational_business(request, kind, 1)
    if business is None:
        return render(request, "core/no-business.html")
    document = TradeDocument(business=business, kind=kind, created_by=request.user)
    return _document_form(request, business, kind, document, f"New {TradeDocument.Kind(kind).label.lower()}")


@login_required
def document_edit(request, kind, pk):
    business = operational_business(request, kind, 1)
    if business is None:
        return render(request, "core/no-business.html")
    document = get_object_or_404(TradeDocument, business=business, kind=kind, pk=pk)
    if document.status == TradeDocument.Status.POSTED or document.period.is_locked:
        messages.error(request, _("Posted or locked documents cannot be edited."))
        return redirect(route_name(kind, "detail"), pk=pk)
    return _document_form(request, business, kind, document, f"Edit {document.number}")


@login_required
def document_detail(request, kind, pk):
    business = operational_business(request, kind, 0)
    if business is None:
        return render(request, "core/no-business.html")
    document = get_object_or_404(
        TradeDocument.objects.select_related(
            "party", "period", "debit_account", "credit_account", "journal_entry", "created_by"
        ).prefetch_related(
            "lines__product",
            Prefetch(
                "payments",
                queryset=SalePayment.objects.select_related(
                    "payment_account", "money_receipt", "journal_entry", "received_by"
                ),
            ),
            Prefetch(
                "supplier_payments",
                queryset=PurchasePayment.objects.select_related(
                    "payment_account", "voucher", "journal_entry", "paid_by"
                ),
            ),
            Prefetch(
                "sale_setoff_allocations",
                queryset=SaleSetoffAllocation.objects.select_related(
                    "setoff__voucher", "setoff__journal_entry"
                ),
            ),
            Prefetch(
                "purchase_setoff_allocations",
                queryset=PurchaseSetoffAllocation.objects.select_related(
                    "setoff__voucher", "setoff__journal_entry"
                ),
            ),
        ),
        business=business, kind=kind, pk=pk,
    )
    money_receipt = (
        MoneyReceipt.objects.filter(
            business=business,
            voucher__journal_entry_id=document.journal_entry_id,
        ).first()
        if document.journal_entry_id
        else None
    )
    return render(request, "operations/document-detail.html", {
        "business": business,
        "document": document,
        "kind": kind,
        "money_receipt": money_receipt,
        "payments": (
            document.payments.all()
            if kind == TradeDocument.Kind.SALE
            else document.supplier_payments.all()
        ),
        "setoff_allocations": (
            document.sale_setoff_allocations.all()
            if kind == TradeDocument.Kind.SALE
            else document.purchase_setoff_allocations.all()
        ),
        "paid_amount": document.paid_amount,
        "balance_due": document.balance_due,
        "payment_status": document.payment_status,
        "settlement_summary": (
            _("Receive later · Accounts Receivable")
            if kind == TradeDocument.Kind.SALE
            and document.debit_account.system_role == Account.SystemRole.ACCOUNTS_RECEIVABLE
            else _("Pay later · Accounts Payable")
            if kind == TradeDocument.Kind.PURCHASE
            and document.credit_account.system_role == Account.SystemRole.ACCOUNTS_PAYABLE
            else str(
                document.debit_account
                if kind == TradeDocument.Kind.SALE
                else document.credit_account
            )
        ),
        "is_credit_sale": (
            kind == TradeDocument.Kind.SALE
            and document.debit_account.system_role
            == "accounts_receivable"
        ),
        "is_credit_purchase": (
            kind == TradeDocument.Kind.PURCHASE
            and document.credit_account.system_role
            == "accounts_payable"
        ),
    })


@login_required
def sale_receive_payment(request, pk):
    business = operational_business(request, TradeDocument.Kind.SALE, 2)
    if business is None:
        return render(request, "core/no-business.html")
    sale = get_object_or_404(
        TradeDocument.objects.select_related(
            "party", "period", "debit_account", "credit_account"
        ).prefetch_related("payments"),
        business=business,
        kind=TradeDocument.Kind.SALE,
        pk=pk,
    )
    if sale.status != TradeDocument.Status.POSTED:
        messages.error(request, _("Payment can be received only against a posted sale."))
        return redirect("sale-detail", pk=sale.pk)
    if not sale.can_receive_payment:
        detail = (
            _("This invoice is already paid in full.")
            if sale.balance_due <= 0
            else _("This sale was not posted to Accounts Receivable.")
        )
        messages.error(request, detail)
        return redirect("sale-detail", pk=sale.pk)

    form = ReceiveSalePaymentForm(
        request.POST or None,
        business=business,
        sale=sale,
    )
    if request.method == "POST" and form.is_valid():
        try:
            payment = receive_sale_payment(
                ReceiveSalePaymentCommand(
                    sale_id=sale.pk,
                    business_id=business.pk,
                    payment_account_id=form.cleaned_data["payment_account"].pk,
                    amount=form.cleaned_data["amount"],
                    payment_date=form.cleaned_data["payment_date"],
                    idempotency_key=form.cleaned_data["idempotency_key"],
                    notes=form.cleaned_data["notes"],
                    user_id=request.user.pk,
                ),
                DjangoSalePaymentRepository(),
            )
        except (ValidationError, IntegrityError) as exc:
            detail = (
                " ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "The payment could not be posted because a financial reference already exists."
            )
            form.add_error(None, detail)
        else:
            messages.success(
                request,
                _("Payment %(payment)s posted. Money receipt %(receipt)s is ready.")
                % {
                    "payment": payment.number,
                    "receipt": payment.money_receipt.number,
                },
            )
            return redirect("sale-detail", pk=sale.pk)
    return render(request, "operations/receive-payment.html", {
        "business": business,
        "sale": sale,
        "form": form,
        "paid_amount": sale.paid_amount,
        "balance_due": sale.balance_due,
    })


@login_required
def purchase_pay_supplier(request, pk):
    business = operational_business(request, TradeDocument.Kind.PURCHASE, 2)
    if business is None:
        return render(request, "core/no-business.html")
    purchase = get_object_or_404(
        TradeDocument.objects.select_related(
            "party", "period", "debit_account", "credit_account"
        ).prefetch_related("supplier_payments"),
        business=business,
        kind=TradeDocument.Kind.PURCHASE,
        pk=pk,
    )
    if purchase.status != TradeDocument.Status.POSTED:
        messages.error(request, _("Payment can be made only against a posted purchase."))
        return redirect("purchase-detail", pk=purchase.pk)
    if not purchase.can_pay_supplier:
        detail = (
            _("This payable is already paid in full.")
            if purchase.balance_due <= 0
            else _("This purchase was not posted to Accounts Payable.")
        )
        messages.error(request, detail)
        return redirect("purchase-detail", pk=purchase.pk)

    form = PayPurchaseForm(
        request.POST or None,
        business=business,
        purchase=purchase,
    )
    if request.method == "POST" and form.is_valid():
        try:
            payment = pay_purchase(
                PayPurchaseCommand(
                    purchase_id=purchase.pk,
                    business_id=business.pk,
                    payment_account_id=form.cleaned_data["payment_account"].pk,
                    amount=form.cleaned_data["amount"],
                    payment_date=form.cleaned_data["payment_date"],
                    idempotency_key=form.cleaned_data["idempotency_key"],
                    notes=form.cleaned_data["notes"],
                    user_id=request.user.pk,
                ),
                DjangoPurchasePaymentRepository(),
            )
        except (ValidationError, IntegrityError) as exc:
            detail = (
                " ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "The payment could not be posted because a financial reference already exists."
            )
            form.add_error(None, detail)
        else:
            messages.success(
                request,
                _("Payment %(payment)s posted with voucher %(voucher)s.")
                % {"payment": payment.number, "voucher": payment.voucher.number},
            )
            return redirect("purchase-detail", pk=purchase.pk)
    return render(request, "operations/pay-purchase.html", {
        "business": business,
        "purchase": purchase,
        "form": form,
        "paid_amount": purchase.paid_amount,
        "balance_due": purchase.balance_due,
    })


@login_required
def payment_center(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    can_view_sales = is_authorized(request.user, business, SALES_VIEW)
    can_view_purchases = is_authorized(request.user, business, PURCHASES_VIEW)
    if not can_view_sales and not can_view_purchases:
        raise PermissionDenied

    query = request.GET.get("q", "").strip()
    receivable_query = TradeDocument.objects.none()
    payable_query = TradeDocument.objects.none()
    if can_view_sales:
        receivable_query = TradeDocument.objects.filter(
            business=business,
            kind=TradeDocument.Kind.SALE,
            status=TradeDocument.Status.POSTED,
            debit_account__system_role="accounts_receivable",
        ).select_related("party", "debit_account").prefetch_related(
            "payments", "sale_setoff_allocations"
        )
        if query:
            receivable_query = receivable_query.filter(
                Q(number__icontains=query) | Q(party__name__icontains=query)
            )
    if can_view_purchases:
        payable_query = TradeDocument.objects.filter(
            business=business,
            kind=TradeDocument.Kind.PURCHASE,
            status=TradeDocument.Status.POSTED,
            credit_account__system_role="accounts_payable",
        ).select_related("party", "credit_account").prefetch_related(
            "supplier_payments", "purchase_setoff_allocations"
        )
        if query:
            payable_query = payable_query.filter(
                Q(number__icontains=query) | Q(party__name__icontains=query)
            )

    receivables = [document for document in receivable_query if document.balance_due > 0]
    payables = [document for document in payable_query if document.balance_due > 0]
    receipts = (
        SalePayment.objects.filter(business=business)
        .select_related("sale__party", "payment_account", "money_receipt")
        .order_by("-payment_date", "-id")[:10]
        if can_view_sales
        else SalePayment.objects.none()
    )
    disbursements = (
        PurchasePayment.objects.filter(business=business)
        .select_related("purchase__party", "payment_account", "voucher")
        .order_by("-payment_date", "-id")[:10]
        if can_view_purchases
        else PurchasePayment.objects.none()
    )
    can_setoff = (
        can_view_sales
        and can_view_purchases
        and is_authorized(request.user, business, SALES_POST)
        and is_authorized(request.user, business, PURCHASES_POST)
        and is_authorized(request.user, business, ACCOUNTING_POST)
    )
    setoff_options = []
    if can_view_sales and can_view_purchases:
        receivable_by_party = {}
        payable_by_party = {}
        parties = {}
        for document in receivables:
            parties[document.party_id] = document.party
            receivable_by_party[document.party_id] = (
                receivable_by_party.get(document.party_id, Decimal("0.00"))
                + document.balance_due
            )
        for document in payables:
            parties[document.party_id] = document.party
            payable_by_party[document.party_id] = (
                payable_by_party.get(document.party_id, Decimal("0.00"))
                + document.balance_due
            )
        for party_id in sorted(
            set(receivable_by_party).intersection(payable_by_party),
            key=lambda item: parties[item].name.lower(),
        ):
            party = parties[party_id]
            receivable = receivable_by_party[party_id]
            payable = payable_by_party[party_id]
            setoff_options.append({
                "party": party,
                "receivable": receivable,
                "payable": payable,
                "available": min(receivable, payable),
            })
    recent_setoffs = (
        BalanceSetoff.objects.filter(business=business)
        .select_related("party", "voucher")[:10]
        if can_view_sales
        and can_view_purchases
        and is_authorized(request.user, business, ACCOUNTING_VIEW)
        else BalanceSetoff.objects.none()
    )
    return render(request, "operations/payment-center.html", {
        "business": business,
        "query": query,
        "receivables": receivables,
        "payables": payables,
        "receivable_total": sum(
            (document.balance_due for document in receivables), Decimal("0.00")
        ),
        "payable_total": sum(
            (document.balance_due for document in payables), Decimal("0.00")
        ),
        "receipts": receipts,
        "disbursements": disbursements,
        "show_receivables": can_view_sales,
        "show_payables": can_view_purchases,
        "can_setoff": can_setoff,
        "setoff_options": setoff_options,
        "recent_setoffs": recent_setoffs,
    })


def _open_setoff_documents(business, party):
    sales = list(
        TradeDocument.objects.filter(
            business=business,
            party=party,
            kind=TradeDocument.Kind.SALE,
            status=TradeDocument.Status.POSTED,
            debit_account__system_role="accounts_receivable",
        )
        .select_related("party", "debit_account")
        .prefetch_related("payments", "sale_setoff_allocations")
        .order_by("document_date", "id")
    )
    purchases = list(
        TradeDocument.objects.filter(
            business=business,
            party=party,
            kind=TradeDocument.Kind.PURCHASE,
            status=TradeDocument.Status.POSTED,
            credit_account__system_role="accounts_payable",
        )
        .select_related("party", "credit_account")
        .prefetch_related("supplier_payments", "purchase_setoff_allocations")
        .order_by("document_date", "id")
    )
    return (
        [document for document in sales if document.balance_due > 0],
        [document for document in purchases if document.balance_due > 0],
    )


@login_required
def balance_setoff_create(request, party_id):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    for permission in (SALES_POST, PURCHASES_POST, ACCOUNTING_POST):
        authorize(request.user, business, permission)
    party = get_object_or_404(
        business.parties.filter(is_active=True), pk=party_id
    )
    sales, purchases = _open_setoff_documents(business, party)
    if not sales or not purchases:
        messages.error(
            request,
            _("This party needs both an open receivable and an open payable before a set-off can be posted."),
        )
        return redirect("payment-center")
    form = BalanceSetoffForm(
        request.POST or None,
        sales=sales,
        purchases=purchases,
    )
    if request.method == "POST" and form.is_valid():
        try:
            setoff = create_balance_setoff(
                CreateBalanceSetoffCommand(
                    business_id=business.pk,
                    party_id=party.pk,
                    setoff_date=form.cleaned_data["setoff_date"],
                    sale_allocations=tuple(
                        SetoffAllocationCommand(document_id=pk, amount=amount)
                        for pk, amount in form.cleaned_data["sale_allocations"]
                    ),
                    purchase_allocations=tuple(
                        SetoffAllocationCommand(document_id=pk, amount=amount)
                        for pk, amount in form.cleaned_data["purchase_allocations"]
                    ),
                    idempotency_key=form.cleaned_data["idempotency_key"],
                    notes=form.cleaned_data["notes"],
                    user_id=request.user.pk,
                ),
                DjangoBalanceSetoffRepository(),
            )
        except (ValidationError, IntegrityError) as exc:
            detail = (
                " ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "The set-off could not be posted because a financial reference already exists."
            )
            form.add_error(None, detail)
        else:
            messages.success(
                request,
                _("Set-off %(setoff)s posted with contra voucher %(voucher)s.")
                % {"setoff": setoff.number, "voucher": setoff.voucher.number},
            )
            return redirect("balance-setoff-detail", pk=setoff.pk)
    return render(request, "operations/balance-setoff-form.html", {
        "business": business,
        "party": party,
        "sales": sales,
        "purchases": purchases,
        "form": form,
    })


@login_required
def balance_setoff_detail(request, pk):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, ACCOUNTING_VIEW)
    setoff = get_object_or_404(
        BalanceSetoff.objects.select_related(
            "party", "voucher", "journal_entry", "created_by"
        ).prefetch_related(
            "sale_allocations__sale", "purchase_allocations__purchase"
        ),
        business=business,
        pk=pk,
    )
    return render(request, "operations/balance-setoff-detail.html", {
        "business": business,
        "setoff": setoff,
    })


@login_required
def balance_setoff_pdf(request, pk):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, ACCOUNTING_VIEW)
    setoff = get_object_or_404(
        BalanceSetoff.objects.select_related(
            "party", "voucher", "journal_entry", "created_by"
        ).prefetch_related(
            "sale_allocations__sale", "purchase_allocations__purchase"
        ),
        business=business,
        pk=pk,
    )
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, _height = A4
    pdf.setTitle(_("Balance set-off %(number)s") % {"number": setoff.number})
    pdf.setAuthor("Prime Ledger")
    width, _height, y = draw_document_header(
        pdf,
        business,
        _("Balance set-off statement"),
        setoff.number,
        setoff.setoff_date,
        status=_("Posted"),
    )
    pdf.setFillColor(SURFACE_SUBTLE)
    pdf.setStrokeColor(BORDER)
    pdf.roundRect(PAGE_MARGIN, y - 64, width - (2 * PAGE_MARGIN), 70, 5, stroke=1, fill=1)
    pdf.setFillColor(MUTED)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawString(PAGE_MARGIN + 14, y - 14, _("CUSTOMER & SUPPLIER"))
    pdf.setFillColor(INK)
    pdf.setFont(pdf_font(bold=True), 12)
    pdf.drawString(PAGE_MARGIN + 14, y - 35, clean_text(setoff.party.name, 55))
    pdf.setFillColor(TEAL)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawRightString(width - PAGE_MARGIN - 14, y - 14, _("SET-OFF AMOUNT"))
    pdf.setFillColor(INK)
    pdf.setFont(pdf_font(bold=True), 18)
    pdf.drawRightString(
        width - PAGE_MARGIN - 14,
        y - 41,
        f"{business.currency} {setoff.total_amount:,.2f}",
    )
    y -= 86

    def allocation_table(title, allocations, document_attr):
        nonlocal y
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(bold=True), 7)
        pdf.drawString(PAGE_MARGIN, y, _(str(title)).upper())
        y -= 18
        columns = [
            (PAGE_MARGIN + 8, "Document", "left"),
            (PAGE_MARGIN + 220, "Date", "left"),
            (width - PAGE_MARGIN - 8, "Allocated", "right"),
        ]
        draw_table_header(pdf, y, columns, width=width)
        y -= 22
        for index, allocation in enumerate(allocations):
            document = getattr(allocation, document_attr)
            draw_table_row_background(pdf, y, width=width, row_index=index)
            pdf.setFillColor(INK)
            pdf.setFont(pdf_font(bold=True), 8)
            pdf.drawString(PAGE_MARGIN + 8, y, document.number)
            pdf.setFillColor(INK_SOFT)
            pdf.setFont(pdf_font(), 8)
            pdf.drawString(PAGE_MARGIN + 220, y, date_format(document.document_date, "DATE_FORMAT"))
            pdf.setFillColor(INK)
            pdf.setFont(pdf_font(bold=True), 8)
            pdf.drawRightString(width - PAGE_MARGIN - 8, y, f"{allocation.amount:,.2f}")
            y -= 22
        y -= 15

    allocation_table(_("Receivable invoices cleared"), setoff.sale_allocations.all(), "sale")
    allocation_table(_("Payable purchases cleared"), setoff.purchase_allocations.all(), "purchase")
    pdf.setStrokeColor(BORDER_STRONG)
    pdf.line(PAGE_MARGIN, y, width - PAGE_MARGIN, y)
    y -= 18
    pdf.setFillColor(MUTED)
    pdf.setFont(pdf_font(), 8)
    pdf.drawString(
        PAGE_MARGIN,
        y,
        _("Contra voucher: %(number)s") % {"number": setoff.voucher.number},
    )
    pdf.drawRightString(
        width - PAGE_MARGIN,
        y,
        _("Journal: %(reference)s") % {"reference": setoff.journal_entry.reference},
    )
    y -= 30
    pdf.setFillColor(INK_SOFT)
    pdf.setFont(pdf_font(), 8)
    pdf.drawString(PAGE_MARGIN, y, _("Accounting effect: Debit Accounts Payable / Credit Accounts Receivable"))
    draw_page_footer(pdf, width=width, page_number=1)
    pdf.save()
    return HttpResponse(
        buffer.getvalue(),
        content_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="balance-setoff-{setoff.number}.pdf"'
        },
    )


@login_required
def purchase_payment_document_pdf(request, pk):
    business = operational_business(request, TradeDocument.Kind.PURCHASE, 0)
    if business is None:
        return render(request, "core/no-business.html")
    payment = get_object_or_404(
        PurchasePayment.objects.select_related(
            "purchase__party", "payment_account", "journal_entry", "voucher", "paid_by"
        ),
        pk=pk,
        business=business,
    )
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, _height = A4
    pdf.setTitle(_("Payment voucher %(number)s") % {"number": payment.voucher.number})
    pdf.setAuthor("Prime Ledger")
    width, _height, y = draw_document_header(
        pdf,
        business,
        _("Payment voucher"),
        payment.voucher.number,
        payment.payment_date,
        status=_("Posted"),
    )

    pdf.setFillColor(SURFACE_SUBTLE)
    pdf.setStrokeColor(BORDER)
    pdf.roundRect(PAGE_MARGIN, y - 58, width - (2 * PAGE_MARGIN), 66, 5, stroke=1, fill=1)
    pdf.setFillColor(MUTED)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawString(PAGE_MARGIN + 14, y - 13, _("PAID TO"))
    pdf.setFillColor(INK)
    pdf.setFont(pdf_font(bold=True), 12)
    pdf.drawString(
        PAGE_MARGIN + 14,
        y - 34,
        clean_text(payment.purchase.party.name, 62),
    )
    if payment.purchase.party.address:
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(), 7.5)
        pdf.drawString(
            PAGE_MARGIN + 14,
            y - 48,
            clean_text(payment.purchase.party.address, 78),
        )
    y -= 82

    pdf.setFillColor(TEAL_SOFT)
    pdf.roundRect(PAGE_MARGIN, y - 58, width - (2 * PAGE_MARGIN), 64, 5, stroke=0, fill=1)
    pdf.setFillColor(TEAL)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawString(PAGE_MARGIN + 14, y - 14, _("AMOUNT PAID"))
    pdf.setFillColor(INK)
    pdf.setFont(pdf_font(bold=True), 22)
    pdf.drawString(PAGE_MARGIN + 14, y - 42, f"{business.currency} {payment.amount:,.2f}")
    pdf.setFillColor(TEAL)
    pdf.setFont(pdf_font(bold=True), 7.5)
    pdf.drawRightString(width - PAGE_MARGIN - 14, y - 34, _("PAID"))
    y -= 86

    detail_rows = [
        (_("Paid from"), str(payment.payment_account)),
        (_("Purchase invoice"), payment.purchase.number),
        (_("Accounting reference"), payment.journal_entry.reference),
        (_("Payment allocation"), payment.number),
    ]
    pdf.setFillColor(MUTED)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawString(PAGE_MARGIN, y, _("PAYMENT DETAILS"))
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

    if payment.notes:
        y -= 10
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(bold=True), 7)
        pdf.drawString(PAGE_MARGIN, y, _("NOTES"))
        y -= 23
        pdf.setFillColor(SURFACE_SUBTLE)
        pdf.roundRect(PAGE_MARGIN, y - 23, width - (2 * PAGE_MARGIN), 40, 4, stroke=0, fill=1)
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 8)
        pdf.drawString(PAGE_MARGIN + 12, y - 5, clean_text(payment.notes, 100))
        y -= 48

    signature_y = min(y - 72, 155)
    pdf.setStrokeColor(BORDER_STRONG)
    pdf.line(PAGE_MARGIN, signature_y, 195, signature_y)
    pdf.line(width - 195, signature_y, width - PAGE_MARGIN, signature_y)
    pdf.setFillColor(MUTED)
    pdf.setFont(pdf_font(), 7.5)
    pdf.drawString(PAGE_MARGIN, signature_y - 13, _("Prepared by"))
    pdf.drawRightString(width - PAGE_MARGIN, signature_y - 13, _("Authorized by"))
    draw_page_footer(pdf, width=width, page_number=1)
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="payment-voucher-{payment.voucher.number}.pdf"'
    )
    return response


@login_required
@transaction.atomic
def document_delete(request, kind, pk):
    business = operational_business(request, kind, 1)
    if business is None:
        return render(request, "core/no-business.html")
    document = get_object_or_404(
        TradeDocument.objects.select_for_update().select_related("period"),
        business=business,
        kind=kind,
        pk=pk,
    )
    if document.status == TradeDocument.Status.POSTED or document.period.is_locked:
        messages.error(request, _("Only drafts in an open fiscal period can be deleted."))
        return redirect(route_name(kind, "detail"), pk=pk)
    if request.method == "GET":
        return render(request, "core/confirmation.html", {
            "business": business,
            "eyebrow": _("Draft %(kind)s") % {"kind": document.get_kind_display().lower()},
            "title": _("Delete %(number)s?") % {"number": document.number},
            "description": _((
                "This permanently removes the draft and its lines. "
                "Its automatic number will not be reused."
            )),
            "confirmation_text": _("I understand this draft cannot be recovered."),
            "submit_label": _("Delete %(kind)s") % {"kind": document.get_kind_display().lower()},
            "submit_class": "danger",
            "cancel_href": reverse(route_name(kind, "detail"), kwargs={"pk": pk}),
        })
    if request.POST.get("confirm") != "yes":
        messages.error(request, _("Confirm deletion before continuing."))
        return redirect(route_name(kind, "detail"), pk=pk)
    document_number = document.number
    document_label = document.get_kind_display().lower()
    document.delete()
    messages.success(
        request,
        _("Draft %(kind)s %(number)s was deleted.")
        % {"kind": document_label, "number": document_number},
    )
    return redirect(route_name(kind, "list"))


@login_required
def document_post(request, kind, pk):
    business = operational_business(request, kind, 2)
    if business is None:
        return render(request, "core/no-business.html")
    document = get_object_or_404(TradeDocument, business=business, kind=kind, pk=pk)
    if request.method == "GET":
        return render(request, "core/confirmation.html", {
            "business": business,
            "eyebrow": _("%(kind)s posting") % {"kind": document.get_kind_display()},
            "title": _("Post %(number)s?") % {"number": document.number},
            "description": _("Posting creates a balanced journal, voucher, and stock movements in one transaction. It cannot be edited afterward."),
            "confirmation_text": _("I reviewed the party, accounts, products, quantities, prices, and date."),
            "submit_label": _("Post %(kind)s") % {"kind": document.get_kind_display().lower()},
            "cancel_href": reverse(route_name(kind, "detail"), kwargs={"pk": pk}),
        })
    if request.POST.get("confirm") != "yes":
        messages.error(request, _("Confirm posting before continuing."))
        return redirect(route_name(kind, "detail"), pk=pk)
    try:
        posted_document = post_trade_document(
            PostTradeDocumentCommand(document_id=pk, business_id=business.pk),
            DjangoTradeDocumentRepository(),
        )
    except (ValidationError, IntegrityError) as exc:
        detail = " ".join(exc.messages) if isinstance(exc, ValidationError) else "A financial reference already exists."
        messages.error(request, detail)
        return redirect(route_name(kind, "detail"), pk=pk)
    money_receipt = MoneyReceipt.objects.filter(
        business=business,
        voucher__journal_entry_id=posted_document.journal_entry_id,
    ).first()
    if money_receipt:
        message = _(
            "%(kind)s posted successfully. Money receipt %(receipt)s is ready."
        ) % {
            "kind": document.get_kind_display(),
            "receipt": money_receipt.number,
        }
    else:
        message = _("%(kind)s posted successfully.") % {
            "kind": document.get_kind_display()
        }
    messages.success(request, message)
    return redirect(route_name(kind, "detail"), pk=pk)


def _report_headers(response, business, kind, request):
    return [
        ["Business", business.name],
        ["Report", f"{TradeDocument.Kind(kind).label} register"],
        ["Date range", f"{request.GET.get('date_from') or 'All'} to {request.GET.get('date_to') or 'All'}"],
        ["Currency", business.currency],
        ["Tax", "Not included"],
        [_("Generated"), date_format(timezone.localdate(), "SHORT_DATE_FORMAT")],
    ]


@login_required
def document_csv(request, kind):
    business = operational_business(request, kind, 0)
    if business is None:
        return render(request, "core/no-business.html")
    documents, _filters = filtered_documents(request, business, kind)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{kind}-register.csv"'
    writer = csv.writer(response)
    for row in _report_headers(response, business, kind, request):
        writer.writerow(row)
    writer.writerow([])
    if kind == TradeDocument.Kind.SALE:
        writer.writerow([
            _("Number"), _("Date"), _("Party"), _("Status"),
            f"Subtotal ({business.currency})", f"Discount ({business.currency})",
            f"Total ({business.currency})",
        ])
    else:
        writer.writerow([_("Number"), _("Date"), _("Party"), _("Status"), f"Total ({business.currency})"])
    for document in documents:
        row = [document.number, document.document_date, document.party.name, document.status]
        if kind == TradeDocument.Kind.SALE:
            row.extend([document.subtotal, document.discount_amount])
        row.append(document.total)
        writer.writerow(row)
    if not documents.exists():
        writer.writerow([_("No records for the selected filters.")])
    return response


@login_required
def document_pdf(request, kind):
    business = operational_business(request, kind, 0)
    if business is None:
        return render(request, "core/no-business.html")
    documents, _filters = filtered_documents(request, business, kind)
    rows = list(documents)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, height = A4
    pdf.setTitle(
        _("%(business)s %(kind)s register")
        % {"business": business.name, "kind": TradeDocument.Kind(kind).label}
    )
    pdf.setAuthor("Prime Ledger")
    page_number = 1
    date_range = f"{request.GET.get('date_from') or 'All'} to {request.GET.get('date_to') or 'All'}"

    def header():
        return draw_report_header(
            pdf,
            business,
            _("%(kind)s register") % {"kind": TradeDocument.Kind(kind).label},
            page_number=page_number,
            metadata=(
                (_("Reporting period"), date_range),
                (_("Currency"), business.currency),
                (_("Tax basis"), _("Not included")),
            ),
        )

    def columns(current_y):
        if kind == TradeDocument.Kind.SALE:
            headings = (
                (PAGE_MARGIN, "Number", "left"),
                (119, "Date", "left"),
                (191, "Party", "left"),
                (388, "Status", "left"),
                (455, "Subtotal", "right"),
                (505, "Discount", "right"),
                (width - PAGE_MARGIN, f"Total ({business.currency})", "right"),
            )
        else:
            headings = (
                (PAGE_MARGIN, "Number", "left"),
                (125, "Date", "left"),
                (205, "Party", "left"),
                (420, "Status", "left"),
                (width - PAGE_MARGIN, f"Total ({business.currency})", "right"),
            )
        return draw_table_header(pdf, current_y, headings, width=width)

    width, height, y = header()
    y = columns(y)
    if not rows:
        y = draw_empty_state(
            pdf,
            y,
            _("No %(kind)s records match the selected filters")
            % {"kind": TradeDocument.Kind(kind).label.lower()},
            width=width,
        )
    report_total = Decimal("0.00")
    for row_index, document in enumerate(rows):
        if y < 72:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = header()
            y = columns(y)
        draw_table_row_background(pdf, y, width=width, row_index=row_index)
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.7)
        pdf.drawString(PAGE_MARGIN, y, document.number[:18])
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 7.5)
        if kind == TradeDocument.Kind.SALE:
            pdf.drawString(119, y, date_format(document.document_date, "DATE_FORMAT"))
            pdf.drawString(191, y, clean_text(document.party.name, 27))
            pdf.drawString(388, y, str(document.get_status_display()))
            pdf.drawRightString(455, y, f"{document.subtotal:.2f}")
            pdf.setFillColor(MUTED)
            pdf.drawRightString(505, y, f"{document.discount_amount:.2f}")
        else:
            pdf.drawString(125, y, date_format(document.document_date, "DATE_FORMAT"))
            pdf.drawString(205, y, clean_text(document.party.name, 30))
            pdf.drawString(420, y, str(document.get_status_display()))
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.7)
        pdf.drawRightString(width - PAGE_MARGIN, y, f"{document.total:.2f}")
        report_total += document.total
        y -= 20
    if rows:
        if y < 75:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = header()
        draw_report_total(
            pdf,
            y - 3,
            "Posted value" if request.GET.get("state") == "posted" else "Register value",
            report_total,
            width=width,
            currency=business.currency,
        )
    draw_page_footer(pdf, width=width, page_number=page_number)
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{kind}-register.pdf"'
    return response


@login_required
def document_print_pdf(request, kind, pk):
    business = operational_business(request, kind, 0)
    if business is None:
        return render(request, "core/no-business.html")
    document = get_object_or_404(
        TradeDocument.objects.select_related(
            "party", "period", "debit_account"
        ).prefetch_related("lines__product", "payments"),
        business=business, kind=kind, pk=pk,
    )
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, height = A4
    document_label = "Sales invoice" if kind == TradeDocument.Kind.SALE else "Purchase"
    pdf.setTitle(f"{document_label} {document.number}")
    pdf.setAuthor("Prime Ledger")
    page_number = 1

    def header():
        return draw_document_header(
            pdf,
            business,
            document_label,
            document.number,
            document.document_date,
            status=document.get_status_display(),
        )

    def columns(current_y):
        return draw_table_header(
            pdf,
            current_y,
            (
                (PAGE_MARGIN, _("Item / description"), "left"),
                (300, "Quantity", "right"),
                (425, _("Unit price (%(currency)s)") % {"currency": business.currency}, "right"),
                (width - PAGE_MARGIN, _("Line total (%(currency)s)") % {"currency": business.currency}, "right"),
            ),
            width=width,
        )

    width, height, y = header()
    pdf.setFillColor(SURFACE_SUBTLE)
    pdf.setStrokeColor(BORDER)
    pdf.roundRect(PAGE_MARGIN, y - 69, 315, 76, 5, stroke=1, fill=1)
    pdf.setFillColor(MUTED)
    pdf.setFont(pdf_font(bold=True), 7)
    pdf.drawString(PAGE_MARGIN + 13, y - 14, _("BILL TO"))
    pdf.setFillColor(INK)
    pdf.setFont(pdf_font(bold=True), 11)
    pdf.drawString(PAGE_MARGIN + 13, y - 34, clean_text(document.party.name, 42))
    if document.party.address:
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(), 7.5)
        pdf.drawString(PAGE_MARGIN + 13, y - 50, clean_text(document.party.address, 52))

    meta_x = 380
    meta_rows = [
        ("Fiscal period", document.period.name),
        ("Currency", business.currency),
        ("Tax", "Not included"),
    ]
    meta_y = y - 11
    for label, value in meta_rows:
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(), 7.5)
        pdf.drawString(meta_x, meta_y, label)
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.5)
        pdf.drawRightString(width - PAGE_MARGIN, meta_y, clean_text(value, 24))
        meta_y -= 20
    y -= 95
    y = columns(y)

    for row_index, line in enumerate(document.lines.all()):
        if y < 160:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = header()
            y = columns(y)
        draw_table_row_background(pdf, y, width=width, row_index=row_index, height=22)
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.7)
        item_name = line.product.name or line.description
        pdf.drawString(PAGE_MARGIN, y + 2, clean_text(item_name, 37))
        if line.description and line.description != item_name:
            pdf.setFillColor(MUTED)
            pdf.setFont(pdf_font(), 6.5)
            pdf.drawString(PAGE_MARGIN, y - 7, clean_text(line.description, 44))
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 7.7)
        pdf.drawRightString(300, y, f"{line.quantity:.3f}")
        pdf.drawRightString(425, y, f"{line.unit_price:.2f}")
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.7)
        pdf.drawRightString(width - PAGE_MARGIN, y, f"{line.line_total:.2f}")
        y -= 22

    required_total_space = 270 if kind == TradeDocument.Kind.SALE else 215
    if y < required_total_space:
        draw_page_footer(pdf, width=width, page_number=page_number)
        pdf.showPage()
        page_number += 1
        _width, _height, y = header()
    y -= 8
    totals_x = 350
    pdf.setStrokeColor(BORDER_STRONG)
    pdf.line(totals_x, y, width - PAGE_MARGIN, y)
    if document.discount_amount:
        y -= 17
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 8)
        pdf.drawString(totals_x, y, _("Subtotal"))
        pdf.drawRightString(width - PAGE_MARGIN, y, f"{document.subtotal:.2f} {business.currency}")
        y -= 16
        discount_label = "Discount"
        if document.discount_type == TradeDocument.DiscountType.PERCENTAGE:
            discount_label += f" ({document.discount_value:.2f}%)"
        pdf.setFillColor(MUTED)
        pdf.drawString(totals_x, y, discount_label)
        pdf.drawRightString(width - PAGE_MARGIN, y, f"-{document.discount_amount:.2f} {business.currency}")
    y -= 25
    pdf.setFillColor(TEAL_SOFT)
    pdf.roundRect(totals_x - 8, y - 8, width - PAGE_MARGIN - totals_x + 8, 32, 4, stroke=0, fill=1)
    pdf.setFillColor(TEAL)
    pdf.setFont(pdf_font(bold=True), 8)
    pdf.drawString(totals_x, y + 4, _("TOTAL"))
    pdf.setFillColor(INK)
    pdf.setFont(pdf_font(bold=True), 12)
    pdf.drawRightString(width - PAGE_MARGIN, y + 2, f"{document.total:,.2f} {business.currency}")
    if kind == TradeDocument.Kind.SALE:
        y -= 33
        settlement_rows = [
            (_("Amount paid"), document.paid_amount, False),
            (_("Balance due"), document.balance_due, True),
        ]
        for label, value, emphasize in settlement_rows:
            pdf.setFillColor(TEAL if emphasize else INK_SOFT)
            pdf.setFont(pdf_font(bold=emphasize), 8)
            pdf.drawString(totals_x, y, label)
            pdf.drawRightString(width - PAGE_MARGIN, y, f"{value:,.2f} {business.currency}")
            y -= 17
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(bold=True), 7)
        pdf.drawRightString(
            width - PAGE_MARGIN,
            y,
            str(document.get_payment_status_display()).upper(),
        )
    if document.notes:
        y -= 30
        pdf.setFillColor(MUTED)
        pdf.setFont(pdf_font(bold=True), 7)
        pdf.drawString(PAGE_MARGIN, y, _("NOTES"))
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 7.5)
        pdf.drawString(PAGE_MARGIN, y - 14, clean_text(document.notes, 100))
    draw_page_footer(
        pdf,
        width=width,
        page_number=page_number,
        note=_("Generated by Prime Ledger / Financial values shown in the stated currency"),
    )
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    file_prefix = "invoice" if kind == TradeDocument.Kind.SALE else "purchase"
    response["Content-Disposition"] = f'attachment; filename="{file_prefix}-{document.number}.pdf"'
    return response
