from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from accounting.models import Account, FiscalPeriod
from core.models import Party, Product
from operations.models import TradeDocument, TradeLine


class TradeDocumentForm(forms.ModelForm):
    def __init__(self, *args, business=None, kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.kind = kind
        self.instance.business = business
        self.instance.kind = kind
        party_kinds = (
            [Party.Kind.CUSTOMER, Party.Kind.BOTH]
            if kind == TradeDocument.Kind.SALE
            else [Party.Kind.SUPPLIER, Party.Kind.BOTH]
        )
        self.fields["party"].queryset = Party.objects.filter(
            business=business, is_active=True, kind__in=party_kinds
        ).order_by("name")
        self.fields["period"].queryset = FiscalPeriod.objects.filter(
            business=business, is_locked=False
        ).order_by("starts_on")
        accounts = Account.objects.filter(business=business, is_active=True).order_by("code")
        self.fields["debit_account"].queryset = accounts
        self.fields["credit_account"].queryset = accounts
        self.fields["document_date"].initial = timezone.localdate()
        if not self.instance.pk:
            initial_roles = (
                (Account.SystemRole.ACCOUNTS_RECEIVABLE, Account.SystemRole.SALES_REVENUE)
                if kind == TradeDocument.Kind.SALE
                else (Account.SystemRole.INVENTORY, Account.SystemRole.ACCOUNTS_PAYABLE)
            )
            role_accounts = {
                account.system_role: account.pk
                for account in accounts.filter(system_role__in=initial_roles)
            }
            self.fields["debit_account"].initial = role_accounts.get(initial_roles[0])
            self.fields["credit_account"].initial = role_accounts.get(initial_roles[1])
        if kind == TradeDocument.Kind.SALE:
            self.fields["debit_account"].help_text = "Usually Accounts Receivable or Cash."
            self.fields["credit_account"].help_text = "Usually Sales Revenue."
            self.fields["discount_type"].required = False
            self.fields["discount_type"].help_text = "Apply one discount to the complete sale."
            self.fields["discount_value"].required = False
            self.fields["discount_value"].help_text = "Enter a currency amount or percentage according to the selected type."
        else:
            self.fields["debit_account"].help_text = "Usually Inventory or Purchases."
            self.fields["credit_account"].help_text = "Usually Accounts Payable or Cash."
            self.fields.pop("discount_type")
            self.fields.pop("discount_value")

    def clean(self):
        cleaned = super().clean()
        if self.kind == TradeDocument.Kind.SALE:
            discount_type = cleaned.get("discount_type") or TradeDocument.DiscountType.NONE
            discount_value = cleaned.get("discount_value") or Decimal("0.00")
            cleaned["discount_type"] = discount_type
            self.instance.discount_type = discount_type
            if discount_type == TradeDocument.DiscountType.NONE:
                discount_value = Decimal("0.00")
                cleaned["discount_value"] = discount_value
                self.instance.discount_value = discount_value
            if (
                discount_type == TradeDocument.DiscountType.PERCENTAGE
                and discount_value >= Decimal("100")
            ):
                self.add_error("discount_value", "Percentage discount must be less than 100.")
        return cleaned

    class Meta:
        model = TradeDocument
        fields = [
            "document_date", "party", "period", "debit_account",
            "credit_account", "discount_type", "discount_value", "notes",
        ]
        widgets = {
            "document_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


class TradeLineForm(forms.ModelForm):
    def __init__(self, *args, business=None, kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = Product.objects.filter(
            business=business, is_active=True
        ).order_by("name")

    class Meta:
        model = TradeLine
        fields = ["product", "description", "quantity", "unit_price"]


class BaseTradeLineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        active = [form for form in self.forms if form.cleaned_data and not form.cleaned_data.get("DELETE")]
        if not active:
            raise forms.ValidationError("Add at least one product or service line.")
        total = sum(
            (
                (form.cleaned_data.get("quantity") or Decimal("0"))
                * (form.cleaned_data.get("unit_price") or Decimal("0"))
                for form in active
            ),
            Decimal("0"),
        )
        if total <= 0:
            raise forms.ValidationError("The document total must be greater than zero.")


TradeLineFormSet = inlineformset_factory(
    TradeDocument,
    TradeLine,
    form=TradeLineForm,
    formset=BaseTradeLineFormSet,
    extra=3,
    min_num=1,
    validate_min=True,
    can_delete=True,
)
