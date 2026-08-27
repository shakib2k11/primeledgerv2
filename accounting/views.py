from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render

from accounting.forms import (
    AccountForm,
    FiscalPeriodForm,
    JournalEntryForm,
    JournalLineFormSet,
    VoucherForm,
)
from accounting.models import Account, FiscalPeriod, JournalEntry, Voucher
from accounting.models import ChartOfAccountsTemplate
from accounting.application.services import ApplyAccountTemplateCommand, apply_account_template
from accounting.infrastructure.repositories import DjangoAccountTemplateRepository
from core.application.services import (
    ACCOUNTING_MANAGE,
    ACCOUNTING_POST,
    ACCOUNTING_VIEW,
    PostJournalCommand,
    post_journal,
)
from core.infrastructure.repositories import DjangoJournalRepository
from core.views import authorize, request_business


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
        messages.success(request, "Account added to the chart of accounts.")
        return redirect("account-list")
    return render(request, "core/record-form.html", {
        "business": business, "form": form, "title": "Add account",
        "eyebrow": "Chart of accounts", "description": "Use a stable code and the correct accounting classification.",
        "cancel_url": "account-list", "submit_label": "Save account",
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
                f"{template.name} applied: {result.created} created, {result.matched} matched.",
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
        messages.success(request, "Fiscal period created successfully.")
        return redirect("period-list")
    return render(request, "core/record-form.html", {
        "business": business, "form": form, "title": "Create fiscal period",
        "eyebrow": "Accounting controls", "description": "Periods cannot overlap and locked periods reject new postings.",
        "cancel_url": "period-list", "submit_label": "Create period",
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
        messages.success(request, "Fiscal period updated successfully.")
        return redirect("period-list")
    return render(request, "core/record-form.html", {
        "business": business,
        "form": form,
        "title": f"Edit {period.name}",
        "eyebrow": "Accounting controls",
        "description": (
            "This period is locked. Its name may be updated, but its boundaries remain protected."
            if period.is_locked
            else "Update the name or boundaries without overlapping another period or excluding existing entries."
        ),
        "cancel_url": "period-list",
        "submit_label": "Save changes",
    })


@login_required
@transaction.atomic
def period_toggle_lock(request, pk):
    business = accounting_context(request, ACCOUNTING_MANAGE)
    if business is None:
        return render(request, "core/no-business.html")
    period = get_object_or_404(FiscalPeriod, pk=pk, business=business)
    if request.method == "GET":
        action = "reopen" if period.is_locked else "lock"
        return render(request, "core/confirmation.html", {
            "business": business,
            "eyebrow": "Accounting control",
            "title": f"{action.title()} {period.name}?",
            "description": (
                "Reopening permits new postings in this period."
                if period.is_locked
                else "Locking rejects new postings and edits to its financial records."
            ),
            "confirmation_text": f"I understand the effect and want to {action} this period.",
            "submit_label": f"{action.title()} period",
            "cancel_url": "period-list",
        })
    if request.method != "POST" or request.POST.get("confirm") != "yes":
        messages.error(request, "Confirm the period status change before continuing.")
        return redirect("period-list")
    period.is_locked = not period.is_locked
    period.full_clean()
    period.save(update_fields=["is_locked"])
    messages.success(request, f"{period.name} is now {'locked' if period.is_locked else 'open'}.")
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
        messages.success(request, "Draft journal saved successfully.")
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
        messages.error(request, "Posted or locked journal entries cannot be edited.")
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
        messages.error(request, "Confirm posting before continuing.")
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
    messages.success(request, "Journal entry posted. Financial history is now locked.")
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
        messages.success(request, "Voucher created from the posted journal.")
        return redirect("voucher-list")
    return render(request, "core/record-form.html", {
        "business": business, "form": form, "title": "Create voucher",
        "eyebrow": "Financial documents", "description": "Create an immutable voucher from an unassigned posted journal entry.",
        "cancel_url": "voucher-list", "submit_label": "Create voucher",
    })
