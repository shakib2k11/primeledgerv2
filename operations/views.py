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
from django.urls import reverse
from django.utils import timezone
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from accounting.models import MoneyReceipt
from core.application.services import (
    PURCHASES_MANAGE,
    PURCHASES_POST,
    PURCHASES_VIEW,
    SALES_MANAGE,
    SALES_POST,
    SALES_VIEW,
)
from core.views import authorize, request_business
from core.infrastructure.numbering import allocate_reference_number
from operations.application.services import PostTradeDocumentCommand, post_trade_document
from operations.forms import TradeDocumentForm, TradeLineFormSet
from operations.infrastructure.repositories import DjangoTradeDocumentRepository
from operations.models import TradeDocument


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
            messages.success(request, f"Draft {document.get_kind_display().lower()} saved.")
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
        messages.error(request, "Posted or locked documents cannot be edited.")
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
        ).prefetch_related("lines__product"),
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
    })


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
        messages.error(request, "Only drafts in an open fiscal period can be deleted.")
        return redirect(route_name(kind, "detail"), pk=pk)
    if request.method == "GET":
        return render(request, "core/confirmation.html", {
            "business": business,
            "eyebrow": f"Draft {document.get_kind_display().lower()}",
            "title": f"Delete {document.number}?",
            "description": (
                "This permanently removes the draft and its lines. "
                "Its automatic number will not be reused."
            ),
            "confirmation_text": "I understand this draft cannot be recovered.",
            "submit_label": f"Delete {document.get_kind_display().lower()}",
            "submit_class": "danger",
            "cancel_href": reverse(route_name(kind, "detail"), kwargs={"pk": pk}),
        })
    if request.POST.get("confirm") != "yes":
        messages.error(request, "Confirm deletion before continuing.")
        return redirect(route_name(kind, "detail"), pk=pk)
    document_number = document.number
    document_label = document.get_kind_display().lower()
    document.delete()
    messages.success(
        request,
        f"Draft {document_label} {document_number} was deleted.",
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
            "eyebrow": f"{document.get_kind_display()} posting",
            "title": f"Post {document.number}?",
            "description": "Posting creates a balanced journal, voucher, and stock movements in one transaction. It cannot be edited afterward.",
            "confirmation_text": "I reviewed the party, accounts, products, quantities, prices, and date.",
            "submit_label": f"Post {document.get_kind_display().lower()}",
            "cancel_href": reverse(route_name(kind, "detail"), kwargs={"pk": pk}),
        })
    if request.POST.get("confirm") != "yes":
        messages.error(request, "Confirm posting before continuing.")
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
    detail = f" Money receipt {money_receipt.number} is ready." if money_receipt else ""
    messages.success(
        request,
        f"{document.get_kind_display()} posted successfully.{detail}",
    )
    return redirect(route_name(kind, "detail"), pk=pk)


def _report_headers(response, business, kind, request):
    return [
        ["Business", business.name],
        ["Report", f"{TradeDocument.Kind(kind).label} register"],
        ["Date range", f"{request.GET.get('date_from') or 'All'} to {request.GET.get('date_to') or 'All'}"],
        ["Currency", business.currency],
        ["Tax", "Not included"],
        ["Generated", timezone.localdate().strftime("%d-%b-%Y")],
    ]


@login_required
def document_csv(request, kind):
    business = operational_business(request, kind, 0)
    if business is None:
        return render(request, "core/no-business.html")
    documents, _ = filtered_documents(request, business, kind)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{kind}-register.csv"'
    writer = csv.writer(response)
    for row in _report_headers(response, business, kind, request):
        writer.writerow(row)
    writer.writerow([])
    if kind == TradeDocument.Kind.SALE:
        writer.writerow([
            "Number", "Date", "Party", "Status",
            f"Subtotal ({business.currency})", f"Discount ({business.currency})",
            f"Total ({business.currency})",
        ])
    else:
        writer.writerow(["Number", "Date", "Party", "Status", f"Total ({business.currency})"])
    for document in documents:
        row = [document.number, document.document_date, document.party.name, document.status]
        if kind == TradeDocument.Kind.SALE:
            row.extend([document.subtotal, document.discount_amount])
        row.append(document.total)
        writer.writerow(row)
    if not documents.exists():
        writer.writerow(["No records for the selected filters."])
    return response


@login_required
def document_pdf(request, kind):
    business = operational_business(request, kind, 0)
    if business is None:
        return render(request, "core/no-business.html")
    documents, _ = filtered_documents(request, business, kind)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, height = A4
    pdf.setTitle(f"{business.name} {kind} register")
    y = height - 48
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(42, y, business.name)
    y -= 22
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(42, y, f"{TradeDocument.Kind(kind).label} register")
    pdf.setFont("Helvetica", 8)
    for label, value in _report_headers(None, business, kind, request)[2:]:
        y -= 14
        pdf.drawString(42, y, f"{label}: {value}")
    y -= 22
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(42, y, "Number")
    pdf.drawString(140, y, "Date")
    pdf.drawString(215, y, "Party")
    if kind == TradeDocument.Kind.SALE:
        pdf.drawString(350, y, "Status")
        pdf.drawRightString(445, y, "Subtotal")
        pdf.drawRightString(505, y, "Discount")
    else:
        pdf.drawString(390, y, "Status")
    pdf.drawRightString(width - 42, y, f"Total ({business.currency})")
    y -= 9
    pdf.line(42, y, width - 42, y)
    pdf.setFont("Helvetica", 8)
    rows = list(documents)
    if not rows:
        y -= 22
        pdf.drawString(42, y, "No records for the selected filters.")
    for document in rows:
        if y < 55:
            pdf.showPage()
            y = height - 48
            pdf.setFont("Helvetica", 8)
        y -= 18
        pdf.drawString(42, y, document.number[:18])
        pdf.drawString(140, y, document.document_date.strftime("%d-%b-%Y"))
        pdf.drawString(215, y, document.party.name[:30])
        if kind == TradeDocument.Kind.SALE:
            pdf.drawString(350, y, document.get_status_display())
            pdf.drawRightString(445, y, f"{document.subtotal:.2f}")
            pdf.drawRightString(505, y, f"{document.discount_amount:.2f}")
        else:
            pdf.drawString(390, y, document.get_status_display())
        pdf.drawRightString(width - 42, y, f"{document.total:.2f}")
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
        TradeDocument.objects.select_related("party", "period").prefetch_related("lines__product"),
        business=business, kind=kind, pk=pk,
    )
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, height = A4
    document_label = "Sales invoice" if kind == TradeDocument.Kind.SALE else "Purchase"
    pdf.setTitle(f"{document_label} {document.number}")
    y = height - 48
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(42, y, business.name)
    y -= 22
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(42, y, f"{document_label} {document.number}")
    pdf.setFont("Helvetica", 8)
    details = [
        ("Party", document.party.name),
        ("Date", document.document_date.strftime("%d-%b-%Y")),
        ("Period", document.period.name),
        ("Currency", business.currency),
        ("Tax", "Not included"),
        ("Status", document.get_status_display()),
        ("Generated", timezone.localdate().strftime("%d-%b-%Y")),
    ]
    for label, value in details:
        y -= 14
        pdf.drawString(42, y, f"{label}: {value}")
    y -= 22
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(42, y, "Item")
    pdf.drawString(240, y, "Quantity")
    pdf.drawRightString(420, y, "Unit price")
    pdf.drawRightString(width - 42, y, "Line total")
    y -= 9
    pdf.line(42, y, width - 42, y)
    pdf.setFont("Helvetica", 8)
    for line in document.lines.all():
        y -= 18
        pdf.drawString(42, y, line.description[:34])
        pdf.drawString(240, y, f"{line.quantity:.3f}")
        pdf.drawRightString(420, y, f"{line.unit_price:.2f}")
        pdf.drawRightString(width - 42, y, f"{line.line_total:.2f}")
    y -= 13
    pdf.line(350, y, width - 42, y)
    if document.discount_amount:
        y -= 17
        pdf.setFont("Helvetica", 9)
        pdf.drawRightString(470, y, f"Subtotal ({business.currency})")
        pdf.drawRightString(width - 42, y, f"{document.subtotal:.2f}")
        y -= 16
        discount_label = "Discount"
        if document.discount_type == TradeDocument.DiscountType.PERCENTAGE:
            discount_label += f" ({document.discount_value:.2f}%)"
        pdf.drawRightString(470, y, discount_label)
        pdf.drawRightString(width - 42, y, f"-{document.discount_amount:.2f}")
    y -= 18
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawRightString(470, y, f"Total ({business.currency})")
    pdf.drawRightString(width - 42, y, f"{document.total:.2f}")
    if document.notes:
        y -= 30
        pdf.setFont("Helvetica", 8)
        pdf.drawString(42, y, f"Notes: {document.notes[:90]}")
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    file_prefix = "invoice" if kind == TradeDocument.Kind.SALE else "purchase"
    response["Content-Disposition"] = f'attachment; filename="{file_prefix}-{document.number}.pdf"'
    return response
