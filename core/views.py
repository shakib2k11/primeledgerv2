import csv
from io import BytesIO

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from decimal import Decimal

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.formats import date_format
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from .pdf import (
    INK,
    INK_SOFT,
    PAGE_MARGIN,
    clean_text,
    draw_empty_state,
    draw_page_footer,
    draw_report_header,
    draw_table_header,
    draw_table_row_background,
    pdf_font,
)
from .forms import InventoryUnitForm, PartyForm, ProductForm, StockMovementForm, UnitInheritanceForm
from .application.services import (
    ACCOUNTING_VIEW,
    CONTACTS_MANAGE,
    CONTACTS_VIEW,
    INVENTORY_MANAGE,
    INVENTORY_VIEW,
    PURCHASES_VIEW,
    SALES_VIEW,
    PermissionDenied,
    get_current_business,
    require_permission,
)
from .infrastructure.repositories import DjangoBusinessReader
from .infrastructure.numbering import allocate_reference_number
from .models import InventoryUnit, Party, Product, StockMovement
from django.utils.translation import gettext as _


business_reader = DjangoBusinessReader()


def current_business(user, business_id=None):
    return get_current_business(user, business_reader, business_id)


def request_business(request):
    raw_id = request.GET.get("business") or request.headers.get("X-Business-ID")
    if raw_id is None:
        raw_id = request.session.get("business_id")
    try:
        business_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        business_id = None
    business = current_business(request.user, business_id)
    if business:
        request.session["business_id"] = business.pk
    return business


def authorize(user, business, permission):
    try:
        require_permission(user, business, permission)
    except PermissionDenied as exc:
        raise DjangoPermissionDenied from exc


def is_authorized(user, business, permission):
    try:
        require_permission(user, business, permission)
    except PermissionDenied:
        return False
    return True


def health_check(request):
    return JsonResponse({"status": "ok", "service": "prime-ledger"})


@login_required
def dashboard(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    can_contacts = is_authorized(request.user, business, CONTACTS_VIEW)
    can_inventory = is_authorized(request.user, business, INVENTORY_VIEW)
    movements = (
        StockMovement.objects.filter(business=business).select_related("product")[:8]
        if can_inventory
        else StockMovement.objects.none()
    )
    inflow = (
        StockMovement.objects.filter(business=business, direction=StockMovement.Direction.IN).aggregate(total=Sum("quantity"))["total"] or 0
        if can_inventory
        else 0
    )
    outflow = (
        StockMovement.objects.filter(business=business, direction=StockMovement.Direction.OUT).aggregate(total=Sum("quantity"))["total"] or 0
        if can_inventory
        else 0
    )
    context = {
        "business": business,
        "party_count": Party.objects.filter(business=business, is_active=True).count() if can_contacts else 0,
        "product_count": Product.objects.filter(business=business, is_active=True).count() if can_inventory else 0,
        "stock_in": inflow,
        "stock_out": outflow,
        "movements": movements,
        "can_view_contacts": can_contacts,
        "can_view_inventory": can_inventory,
    }
    can_sales = is_authorized(request.user, business, SALES_VIEW)
    can_purchases = is_authorized(request.user, business, PURCHASES_VIEW)
    if can_sales or can_purchases:
        from operations.models import TradeDocument

        today_documents = TradeDocument.objects.filter(
            business=business,
            status=TradeDocument.Status.POSTED,
            document_date=timezone.localdate(),
        )
        context.update(
            can_view_sales=can_sales,
            can_view_purchases=can_purchases,
            today_sales=(
                today_documents.filter(kind=TradeDocument.Kind.SALE).aggregate(total=Sum("total"))["total"]
                or Decimal("0.00")
            ),
            today_purchases=(
                today_documents.filter(kind=TradeDocument.Kind.PURCHASE).aggregate(total=Sum("total"))["total"]
                or Decimal("0.00")
            ),
        )
    if is_authorized(request.user, business, ACCOUNTING_VIEW):
        from accounting.models import FiscalPeriod, JournalEntry, Voucher

        context.update(
            can_view_accounting=True,
            draft_journal_count=JournalEntry.objects.filter(
                business=business, posted=False
            ).count(),
            posted_value=Voucher.objects.filter(business=business).aggregate(
                total=Sum("total")
            )["total"] or Decimal("0.00"),
            current_period=FiscalPeriod.objects.filter(
                business=business,
                starts_on__lte=timezone.localdate(),
                ends_on__gte=timezone.localdate(),
            ).first(),
        )
    return render(request, "core/dashboard.html", context)


@login_required
def party_list(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, CONTACTS_VIEW)
    query = request.GET.get("q", "").strip()
    parties = Party.objects.filter(business=business, is_active=True)
    if query:
        parties = parties.filter(Q(name__icontains=query) | Q(phone__icontains=query) | Q(email__icontains=query))
    page = Paginator(parties.order_by("name", "pk"), 25).get_page(request.GET.get("page"))
    return render(request, "core/party-list.html", {"business": business, "parties": page, "page_obj": page, "query": query})


@login_required
def party_create(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, CONTACTS_MANAGE)
    form = PartyForm(request.POST or None, business=business)
    if request.method == "POST" and form.is_valid():
        party = form.save(commit=False)
        party.business = business
        party.save()
        messages.success(request, _("Party saved successfully."))
        return redirect("party-list")
    return render(request, "core/party-form.html", {"business": business, "form": form, "title": _("Add party")})


@login_required
def product_list(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_VIEW)
    query = request.GET.get("q", "").strip()
    products = Product.objects.filter(business=business, is_active=True).select_related("unit").annotate(
        stock_in_total=Sum(
            "stock_movements__quantity",
            filter=Q(stock_movements__direction=StockMovement.Direction.IN),
            default=Decimal("0"),
        ),
        stock_out_total=Sum(
            "stock_movements__quantity",
            filter=Q(stock_movements__direction=StockMovement.Direction.OUT),
            default=Decimal("0"),
        ),
    ).annotate(
        stock_balance=ExpressionWrapper(
            F("stock_in_total") - F("stock_out_total"),
            output_field=DecimalField(max_digits=14, decimal_places=3),
        )
    )
    if query:
        products = products.filter(Q(name__icontains=query) | Q(sku__icontains=query) | Q(barcode__icontains=query))
    page = Paginator(products.order_by("name", "pk"), 25).get_page(request.GET.get("page"))
    return render(request, "core/product-list.html", {"business": business, "products": page, "page_obj": page, "query": query})


@login_required
def product_create(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_MANAGE)
    form = ProductForm(request.POST or None, business=business)
    if request.method == "POST" and form.is_valid():
        product = form.save(commit=False)
        product.business = business
        product.full_clean()
        product.save()
        messages.success(request, _("Inventory item saved successfully."))
        return redirect("product-list")
    return render(request, "core/product-form.html", {"business": business, "form": form, "title": _("Add inventory item")})


@login_required
def product_edit(request, pk):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_MANAGE)
    product = get_object_or_404(Product, pk=pk, business=business)
    form = ProductForm(request.POST or None, instance=product, business=business)
    if request.method == "POST" and form.is_valid():
        product = form.save(commit=False)
        product.business = business
        product.full_clean()
        product.save()
        messages.success(request, _("Inventory item updated successfully."))
        return redirect("product-list")
    return render(request, "core/product-form.html", {
        "business": business,
        "form": form,
        "title": _("Edit %(product)s") % {"product": product.name},
    })


@login_required
def unit_list(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_VIEW)
    return render(request, "core/unit-list.html", {
        "business": business,
        "business_units": InventoryUnit.objects.filter(business=business).order_by("name"),
        "default_units": InventoryUnit.objects.filter(
            business__isnull=True, is_active=True
        ).order_by("name"),
        "inheritance_form": UnitInheritanceForm(instance=business),
    })


@login_required
def unit_inheritance_update(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_MANAGE)
    if request.method != "POST":
        return redirect("unit-list")
    form = UnitInheritanceForm(request.POST, instance=business)
    if form.is_valid():
        form.save()
        messages.success(request, _("Default unit availability updated."))
    else:
        business_units = InventoryUnit.objects.filter(business=business).order_by("name")
        default_units = InventoryUnit.objects.filter(
            business__isnull=True, is_active=True
        ).order_by("name")
        return render(request, "core/unit-list.html", {
            "business": business,
            "business_units": business_units,
            "default_units": default_units,
            "inheritance_form": form,
        }, status=400)
    return redirect("unit-list")


@login_required
def unit_create(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_MANAGE)
    form = InventoryUnitForm(request.POST or None, business=business)
    if request.method == "POST" and form.is_valid():
        unit = form.save(commit=False)
        unit.business = business
        unit.full_clean()
        unit.save()
        messages.success(request, _("Business inventory unit created."))
        return redirect("unit-list")
    return render(request, "core/unit-form.html", {
        "business": business,
        "form": form,
        "title": _("Add business unit"),
        "description": _("Create a unit available only inside this business."),
        "cancel_url": "unit-list",
    })


@login_required
def unit_edit(request, pk):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_MANAGE)
    unit = get_object_or_404(InventoryUnit, pk=pk, business=business)
    form = InventoryUnitForm(request.POST or None, instance=unit, business=business)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Business inventory unit updated."))
        return redirect("unit-list")
    return render(request, "core/unit-form.html", {
        "business": business,
        "form": form,
        "title": _("Edit %(unit)s") % {"unit": unit.name},
        "description": _("Update this business-owned unit or mark it inactive."),
        "cancel_url": "unit-list",
    })


@login_required
def stock_movement_list(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_VIEW)
    movements, filters = filtered_stock_movements(request, business)
    page = Paginator(movements, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "core/stock-movement-list.html",
        {"business": business, "movements": page, "page_obj": page, **filters},
    )


def filtered_stock_movements(request, business):
    query = request.GET.get("q", "").strip()
    direction = request.GET.get("direction", "")
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    movements = StockMovement.objects.filter(business=business).select_related("product", "product__unit", "created_by")
    if query:
        movements = movements.filter(
            Q(number__icontains=query)
            | Q(product__name__icontains=query)
            | Q(reference__icontains=query)
        )
    if direction in StockMovement.Direction.values:
        movements = movements.filter(direction=direction)
    if date_from:
        movements = movements.filter(occurred_at__date__gte=date_from)
    if date_to:
        movements = movements.filter(occurred_at__date__lte=date_to)
    return movements, {
        "query": query,
        "direction": direction,
        "date_from": date_from,
        "date_to": date_to,
    }


@login_required
def stock_movement_csv(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_VIEW)
    movements, _filters = filtered_stock_movements(request, business)
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="stock-movement-register.csv"'
    writer = csv.writer(response)
    writer.writerow([_("Business"), business.name])
    writer.writerow([_("Report"), _("Stock movement register")])
    writer.writerow([_("Currency"), business.currency])
    writer.writerow([_("Generated"), date_format(timezone.localdate(), "SHORT_DATE_FORMAT")])
    writer.writerow([])
    writer.writerow([
        _("Number"), _("Date and time"), _("Item"), _("Direction"), _("Quantity"), _("Unit"),
        f"Unit cost ({business.currency})", _("Source reference"),
    ])
    for movement in movements:
        writer.writerow([
            movement.number,
            date_format(timezone.localtime(movement.occurred_at), "SHORT_DATETIME_FORMAT"),
            movement.product.name,
            movement.get_direction_display(),
            movement.quantity,
            movement.product.unit.symbol,
            movement.unit_cost,
            movement.reference,
        ])
    if not movements.exists():
        writer.writerow([_("No records for the selected filters.")])
    return response


@login_required
def stock_movement_pdf(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_VIEW)
    movements, _filters = filtered_stock_movements(request, business)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    width, height = A4
    pdf.setTitle(_("%(business)s stock movement register") % {"business": business.name})
    pdf.setAuthor("Prime Ledger")
    rows = list(movements)
    page_number = 1

    def header():
        return draw_report_header(
            pdf,
            business,
            _("Stock movement register"),
            page_number=page_number,
            metadata=(
                (_("Currency"), business.currency),
                (_("Record basis"), _("Append-only movements")),
            ),
        )

    def columns(current_y):
        return draw_table_header(
            pdf,
            current_y,
            (
                (PAGE_MARGIN, "Number", "left"),
                (112, "Date", "left"),
                (185, "Item", "left"),
                (350, "Direction", "left"),
                (455, "Quantity", "right"),
                (width - PAGE_MARGIN, _("Unit cost (%(currency)s)") % {"currency": business.currency}, "right"),
            ),
            width=width,
        )

    width, height, y = header()
    y = columns(y)
    if not rows:
        y = draw_empty_state(
            pdf,
            y,
            _("No stock movements match the selected filters"),
            width=width,
        )
    for row_index, movement in enumerate(rows):
        if y < 70:
            draw_page_footer(pdf, width=width, page_number=page_number)
            pdf.showPage()
            page_number += 1
            _width, _height, y = header()
            y = columns(y)
        draw_table_row_background(pdf, y, width=width, row_index=row_index)
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.7)
        pdf.drawString(PAGE_MARGIN, y, movement.number)
        pdf.setFillColor(INK_SOFT)
        pdf.setFont(pdf_font(), 7.5)
        pdf.drawString(112, y, date_format(timezone.localtime(movement.occurred_at), "DATE_FORMAT"))
        pdf.drawString(185, y, clean_text(movement.product.name, 26))
        pdf.drawString(350, y, str(movement.get_direction_display()))
        pdf.drawRightString(455, y, f"{movement.quantity:.3f} {movement.product.unit.symbol}")
        pdf.setFillColor(INK)
        pdf.setFont(pdf_font(bold=True), 7.7)
        pdf.drawRightString(width - PAGE_MARGIN, y, f"{movement.unit_cost:.2f}")
        y -= 20
    draw_page_footer(pdf, width=width, page_number=page_number)
    pdf.save()
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = 'attachment; filename="stock-movement-register.pdf"'
    return response


@login_required
@transaction.atomic
def stock_movement_create(request):
    business = request_business(request)
    if business is None:
        return render(request, "core/no-business.html")
    authorize(request.user, business, INVENTORY_MANAGE)
    form = StockMovementForm(request.POST or None, business=business)
    if request.method == "POST" and form.is_valid():
        movement = form.save(commit=False)
        movement.business = business
        movement.created_by = request.user
        movement.number = allocate_reference_number(
            business_id=business.pk,
            occurred_on=movement.occurred_at,
        )
        movement.full_clean()
        movement.save()
        messages.success(request, _("Stock movement recorded successfully."))
        return redirect("stock-movement-list")
    return render(
        request,
        "core/record-form.html",
        {
            "business": business,
            "form": form,
            "title": _("Record stock movement"),
            "eyebrow": _("Inventory control"),
            "description": _("Record an append-only inflow or outflow. Its eight-digit number is assigned automatically."),
            "cancel_url": "stock-movement-list",
            "submit_label": _("Record movement"),
        },
    )
