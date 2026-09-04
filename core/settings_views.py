from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from accounting.application.services import ApplyAccountTemplateCommand, apply_account_template
from accounting.forms import AccountTemplateLineForm, ChartOfAccountsTemplateForm
from accounting.infrastructure.repositories import DjangoAccountTemplateRepository
from accounting.models import (
    Account,
    AccountTemplateLine,
    ChartOfAccountsTemplate,
    FiscalPeriod,
)
from core.forms import BusinessAdminCreationForm, BusinessForm, InventoryUnitForm
from core.models import Business, InventoryUnit, Membership, Party, Product
from django.utils.translation import gettext_lazy as _


def superuser_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


@superuser_required
def tenant_list(request):
    query = request.GET.get("q", "").strip()
    businesses = Business.objects.annotate(
        member_count=Count("memberships", distinct=True),
        product_count=Count("products", distinct=True),
    ).order_by("name")
    if query:
        businesses = businesses.filter(name__icontains=query)
    return render(request, "core/settings/tenant-list.html", {
        "businesses": businesses, "query": query, "business": None
    })


@superuser_required
@transaction.atomic
def tenant_create(request):
    form = BusinessForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        business = form.save()
        default_template = ChartOfAccountsTemplate.objects.filter(
            is_default=True,
            is_active=True,
        ).first()
        applied_count = 0
        if default_template:
            result = apply_account_template(
                ApplyAccountTemplateCommand(
                    template_id=default_template.pk,
                    business_id=business.pk,
                    user_id=request.user.pk,
                ),
                DjangoAccountTemplateRepository(),
            )
            applied_count = result.created
        request.session["business_id"] = business.pk
        if applied_count:
            message = _(
                "%(business)s was created. %(count)s default accounts were installed. "
                "Assign its first Business Admin next."
            ) % {"business": business.name, "count": applied_count}
        else:
            message = _(
                "%(business)s was created. Assign its first Business Admin next."
            ) % {"business": business.name}
        messages.success(request, message)
        return redirect("tenant-detail", pk=business.pk)
    return render(request, "core/settings/tenant-form.html", {
        "form": form,
        "business": None,
        "title": _("Create business"),
        "description": _("Create an isolated tenant workspace with its own members and records."),
    })


@superuser_required
def tenant_edit(request, pk):
    tenant = get_object_or_404(Business, pk=pk)
    form = BusinessForm(request.POST or None, instance=tenant)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Business settings updated."))
        return redirect("tenant-detail", pk=tenant.pk)
    return render(request, "core/settings/tenant-form.html", {
        "form": form,
        "business": tenant,
        "title": _("Edit %(business)s") % {"business": tenant.name},
        "description": _("Update identity, locale, currency, or operational status."),
    })


@superuser_required
def tenant_detail(request, pk):
    tenant = get_object_or_404(Business, pk=pk)
    memberships = Membership.objects.filter(business=tenant).select_related("user", "role").order_by(
        "level", "user__username"
    )
    context = {
        "business": tenant,
        "tenant": tenant,
        "memberships": memberships,
        "admin_count": memberships.filter(
            level=Membership.Level.BUSINESS_ADMIN, is_active=True
        ).count(),
        "account_count": Account.objects.filter(business=tenant, is_active=True).count(),
        "period_count": FiscalPeriod.objects.filter(business=tenant).count(),
        "party_count": Party.objects.filter(business=tenant, is_active=True).count(),
        "product_count": Product.objects.filter(business=tenant, is_active=True).count(),
        "unit_count": InventoryUnit.objects.available_to(tenant).count(),
    }
    return render(request, "core/settings/tenant-detail.html", context)


@superuser_required
@transaction.atomic
def tenant_admin_create(request, pk):
    tenant = get_object_or_404(Business, pk=pk)
    form = BusinessAdminCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        user = get_user_model().objects.create_user(
            username=values["username"],
            password=values["password1"],
            first_name=values["first_name"],
            last_name=values["last_name"],
            email=values["email"],
        )
        membership = Membership(
            user=user,
            business=tenant,
            level=Membership.Level.BUSINESS_ADMIN,
            is_active=True,
        )
        membership.full_clean()
        membership.save()
        messages.success(
            request,
            _("%(username)s can now administer %(business)s.")
            % {"username": user.username, "business": tenant.name},
        )
        return redirect("tenant-detail", pk=tenant.pk)
    return render(request, "core/settings/admin-form.html", {
        "business": tenant, "tenant": tenant, "form": form
    })


@superuser_required
def default_unit_list(request):
    units = InventoryUnit.objects.filter(business__isnull=True).order_by("name")
    return render(request, "core/settings/default-unit-list.html", {
        "business": None,
        "units": units,
    })


@superuser_required
def default_unit_create(request):
    form = InventoryUnitForm(request.POST or None, business=None)
    if request.method == "POST" and form.is_valid():
        unit = form.save(commit=False)
        unit.business = None
        unit.full_clean()
        unit.save()
        messages.success(request, _("Default inventory unit created."))
        return redirect("default-unit-list")
    return render(request, "core/unit-form.html", {
        "business": None,
        "form": form,
        "title": _("Add default unit"),
        "description": _("Make this unit available to businesses that inherit Prime Ledger defaults."),
        "cancel_url": "default-unit-list",
    })


@superuser_required
def default_unit_edit(request, pk):
    unit = get_object_or_404(InventoryUnit, pk=pk, business__isnull=True)
    form = InventoryUnitForm(request.POST or None, instance=unit, business=None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Default inventory unit updated."))
        return redirect("default-unit-list")
    return render(request, "core/unit-form.html", {
        "business": None,
        "form": form,
        "title": _("Edit %(unit)s") % {"unit": unit.name},
        "description": _("Changes affect every business that inherits default units."),
        "cancel_url": "default-unit-list",
    })


@superuser_required
def account_template_list(request):
    templates = ChartOfAccountsTemplate.objects.annotate(
        line_count=Count("lines", distinct=True),
        business_count=Count("applications__business", distinct=True),
    ).order_by("name")
    return render(request, "core/settings/account-template-list.html", {
        "business": None,
        "templates": templates,
    })


@superuser_required
def account_template_detail(request, pk):
    template = get_object_or_404(ChartOfAccountsTemplate, pk=pk)
    return render(request, "core/settings/account-template-detail.html", {
        "business": None,
        "template": template,
        "lines": template.lines.order_by("code"),
        "business_count": template.applications.values("business_id").distinct().count(),
    })


def _account_template_form(request, template=None):
    form = ChartOfAccountsTemplateForm(request.POST or None, instance=template)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            template = form.save(commit=False)
            if template.is_default:
                ChartOfAccountsTemplate.objects.filter(is_default=True).exclude(
                    pk=template.pk
                ).update(is_default=False)
            template.save()
        messages.success(request, _("Account template saved."))
        return redirect("account-template-detail", pk=template.pk)
    return render(request, "core/settings/account-template-form.html", {
        "business": None,
        "form": form,
        "template": template,
        "title": f"Edit {template.name}" if template else "Create account template",
    })


@superuser_required
def account_template_create(request):
    return _account_template_form(request)


@superuser_required
def account_template_edit(request, pk):
    return _account_template_form(
        request,
        get_object_or_404(ChartOfAccountsTemplate, pk=pk),
    )


def _account_template_line_form(request, template, line=None):
    form = AccountTemplateLineForm(
        request.POST or None,
        instance=line,
        template=template,
    )
    if request.method == "POST" and form.is_valid():
        line = form.save(commit=False)
        line.template = template
        line.full_clean()
        line.save()
        messages.success(request, _("Template account saved."))
        return redirect("account-template-detail", pk=template.pk)
    return render(request, "core/settings/account-template-line-form.html", {
        "business": None,
        "template": template,
        "line": line,
        "form": form,
        "title": f"Edit {line.code}" if line else "Add template account",
    })


@superuser_required
def account_template_line_create(request, pk):
    return _account_template_line_form(
        request,
        get_object_or_404(ChartOfAccountsTemplate, pk=pk),
    )


@superuser_required
def account_template_line_edit(request, pk, line_pk):
    template = get_object_or_404(ChartOfAccountsTemplate, pk=pk)
    line = get_object_or_404(AccountTemplateLine, pk=line_pk, template=template)
    return _account_template_line_form(request, template, line)
