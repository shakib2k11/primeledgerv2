from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from accounting.models import (
    Account,
    AccountTemplateLine,
    ChartOfAccountsTemplate,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    Voucher,
)
from core.models import Party


class AccountForm(forms.ModelForm):
    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business

    def clean_code(self):
        code = self.cleaned_data["code"].strip()
        if self.business and Account.objects.filter(
            business=self.business, code__iexact=code
        ).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("This account code is already in use.")
        return code

    class Meta:
        model = Account
        fields = ["code", "name", "account_type", "system_role", "is_active"]
        help_texts = {
            "system_role": "Optional stable posting role used by automated accounting workflows.",
        }


class ChartOfAccountsTemplateForm(forms.ModelForm):
    class Meta:
        model = ChartOfAccountsTemplate
        fields = ["name", "description", "is_default", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class AccountTemplateLineForm(forms.ModelForm):
    def __init__(self, *args, template=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.template = template
        self.fields["system_role"].help_text = (
            "Assign only when automated posting depends on this account."
        )

    class Meta:
        model = AccountTemplateLine
        fields = [
            "code", "name", "account_type", "system_role", "account_is_active", "is_active"
        ]
        labels = {
            "account_is_active": "Active when copied",
            "is_active": "Include in template",
        }


class FiscalPeriodForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.is_locked:
            for field_name in ("starts_on", "ends_on"):
                self.fields[field_name].disabled = True
                self.fields[field_name].help_text = "Reopen the period before changing its boundary."

    class Meta:
        model = FiscalPeriod
        fields = ["name", "starts_on", "ends_on"]
        widgets = {
            "starts_on": forms.DateInput(attrs={"type": "date"}),
            "ends_on": forms.DateInput(attrs={"type": "date"}),
        }


class JournalEntryForm(forms.ModelForm):
    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["period"].queryset = FiscalPeriod.objects.filter(
            business=business, is_locked=False
        ).order_by("starts_on")
        self.fields["entry_date"].initial = timezone.localdate()

    class Meta:
        model = JournalEntry
        fields = ["reference", "entry_date", "period", "description"]
        widgets = {"entry_date": forms.DateInput(attrs={"type": "date"})}


class JournalLineForm(forms.ModelForm):
    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(
            business=business, is_active=True
        ).order_by("code")
        self.fields["party"].queryset = Party.objects.filter(
            business=business, is_active=True
        ).order_by("name")

    class Meta:
        model = JournalLine
        fields = ["account", "party", "description", "debit", "credit"]


class BaseJournalLineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        active_forms = [
            form for form in self.forms if form.cleaned_data and not form.cleaned_data.get("DELETE")
        ]
        if len(active_forms) < 2:
            raise forms.ValidationError("A journal entry requires at least two lines.")
        for form in active_forms:
            debit = form.cleaned_data.get("debit") or Decimal("0")
            credit = form.cleaned_data.get("credit") or Decimal("0")
            if (debit > 0) == (credit > 0):
                raise forms.ValidationError("Each line must contain either a debit or a credit amount.")


JournalLineFormSet = inlineformset_factory(
    JournalEntry,
    JournalLine,
    form=JournalLineForm,
    formset=BaseJournalLineFormSet,
    extra=2,
    min_num=2,
    validate_min=True,
    can_delete=True,
)


class VoucherForm(forms.ModelForm):
    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.instance.business = business
        self.fields["party"].queryset = Party.objects.filter(
            business=business, is_active=True
        ).order_by("name")
        self.fields["journal_entry"].queryset = JournalEntry.objects.filter(
            business=business, posted=True, voucher__isnull=True
        ).order_by("-entry_date", "reference")

    def clean(self):
        cleaned = super().clean()
        journal = cleaned.get("journal_entry")
        if journal:
            self.instance.business = self.business
            self.instance.journal_entry = journal
            self.instance.total = journal.total_debit
            self.instance.voucher_date = journal.entry_date
        return cleaned

    class Meta:
        model = Voucher
        fields = ["voucher_type", "number", "party", "journal_entry", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
