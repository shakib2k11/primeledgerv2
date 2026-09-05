import uuid
from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from accounting.models import Account, FiscalPeriod
from accounting.form_fields import OperationalAccountChoiceField
from core.models import Party, Product
from operations.domain.settlement import SettlementMethod, posting_account_plan
from operations.models import TradeDocument, TradeLine
from django.utils.translation import gettext_lazy as _


class TradeDocumentForm(forms.ModelForm):
    settlement_method = forms.ChoiceField()
    funds_account = OperationalAccountChoiceField(
        queryset=Account.objects.none(), required=False
    )

    def __init__(self, *args, business=None, kind=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.kind = kind
        self.instance.business = business
        self.instance.kind = kind
        self.fields["party"].queryset = Party.objects.filter(
            business=business, is_active=True
        ).order_by("name")
        self.fields["period"].queryset = FiscalPeriod.objects.filter(
            business=business, is_locked=False
        ).order_by("starts_on")
        accounts = Account.objects.filter(business=business, is_active=True).order_by("code")
        self.fields["funds_account"].queryset = accounts.filter(
            system_role__in=SettlementMethod.LIQUID
        )
        deferred_label = _("Receive later") if kind == TradeDocument.Kind.SALE else _("Pay later")
        self.fields["settlement_method"].choices = [
            (SettlementMethod.DEFERRED, deferred_label),
            (SettlementMethod.CASH, _("Cash")),
            (SettlementMethod.BANK, _("Bank")),
            (SettlementMethod.MOBILE_MONEY, _("Mobile financial services")),
        ]
        self.fields["settlement_method"].label = _("Payment arrangement")
        self.fields["settlement_method"].help_text = (
            _("Choose Receive later for an Accounts Receivable sale.")
            if kind == TradeDocument.Kind.SALE
            else _("Choose Pay later for an Accounts Payable purchase.")
        )
        self.fields["funds_account"].label = (
            _("Deposit into") if kind == TradeDocument.Kind.SALE else _("Pay from")
        )
        self.fields["funds_account"].help_text = _(
            "Shown only for cash, bank, or mobile settlement."
        )
        self.fields["document_date"].initial = timezone.localdate()
        if self.instance.pk:
            operational_account = (
                self.instance.debit_account
                if kind == TradeDocument.Kind.SALE
                else self.instance.credit_account
            )
            method = (
                SettlementMethod.DEFERRED
                if operational_account.system_role
                in {Account.SystemRole.ACCOUNTS_RECEIVABLE, Account.SystemRole.ACCOUNTS_PAYABLE}
                else operational_account.system_role
            )
            self.fields["settlement_method"].initial = method
            if method in SettlementMethod.LIQUID:
                self.fields["funds_account"].initial = operational_account.pk
        else:
            self.fields["settlement_method"].initial = SettlementMethod.DEFERRED
        if kind == TradeDocument.Kind.SALE:
            self.fields["discount_type"].required = False
            self.fields["discount_type"].help_text = _("Apply one discount to the complete sale.")
            self.fields["discount_value"].required = False
            self.fields["discount_value"].help_text = _("Enter a currency amount or percentage according to the selected type.")
        else:
            self.fields.pop("discount_type")
            self.fields.pop("discount_value")

    def clean(self):
        cleaned = super().clean()
        method = cleaned.get("settlement_method")
        funds_account = cleaned.get("funds_account")
        if method:
            plan = posting_account_plan(self.kind, method)
            if plan.funds_side:
                if not funds_account:
                    self.add_error("funds_account", _("Select the cash, bank, or mobile account."))
                elif funds_account.system_role != method:
                    self.add_error(
                        "funds_account",
                        _("The selected account does not match the payment method."),
                    )
            else:
                cleaned["funds_account"] = None

            role_accounts = {
                account.system_role: account
                for account in Account.objects.filter(
                    business=self.business,
                    is_active=True,
                    system_role__in={plan.debit_role, plan.credit_role},
                )
            }
            missing_roles = [
                role for role in {plan.debit_role, plan.credit_role}
                if role not in role_accounts and role != method
            ]
            if missing_roles:
                self.add_error(
                    None,
                    _("The chart of accounts is missing a required system account. Apply the default chart template or ask an administrator."),
                )
            if not self.errors:
                self.instance.debit_account = (
                    funds_account if plan.funds_side == "debit" else role_accounts[plan.debit_role]
                )
                self.instance.credit_account = (
                    funds_account if plan.funds_side == "credit" else role_accounts[plan.credit_role]
                )
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
                self.add_error(
                    "discount_value",
                    _("Percentage discount must be less than 100."),
                )
        return cleaned

    class Meta:
        model = TradeDocument
        fields = [
            "document_date", "party", "period", "settlement_method",
            "funds_account", "discount_type", "discount_value", "notes",
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
            raise forms.ValidationError(_("Add at least one product or service line."))
        total = sum(
            (
                (form.cleaned_data.get("quantity") or Decimal("0"))
                * (form.cleaned_data.get("unit_price") or Decimal("0"))
                for form in active
            ),
            Decimal("0"),
        )
        if total <= 0:
            raise forms.ValidationError(_("The document total must be greater than zero."))


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


class ReceiveSalePaymentForm(forms.Form):
    payment_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=timezone.localdate,
    )
    payment_account = OperationalAccountChoiceField(queryset=Account.objects.none())
    amount = forms.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput())
    confirm = forms.BooleanField(
        label=_("I confirm that this payment has been received and should be posted."),
    )

    def __init__(self, *args, business=None, sale=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.sale = sale
        self.fields["payment_account"].queryset = Account.objects.filter(
            business=business,
            is_active=True,
            system_role__in=[
                Account.SystemRole.CASH,
                Account.SystemRole.BANK,
                Account.SystemRole.MOBILE_MONEY,
            ],
        ).order_by("code")
        if not self.is_bound:
            self.initial.setdefault("amount", sale.balance_due)
            self.initial.setdefault("idempotency_key", uuid.uuid4())
        self.fields["amount"].help_text = _(
            "Outstanding balance: %(balance).2f %(currency)s."
        ) % {"balance": sale.balance_due, "currency": business.currency}
        self.fields["payment_account"].help_text = _(
            "The selected Cash, Bank, or Mobile Financial Services account will be debited."
        )

    def clean_payment_date(self):
        payment_date = self.cleaned_data["payment_date"]
        if payment_date > timezone.localdate():
            raise forms.ValidationError(_("Payment date cannot be in the future."))
        return payment_date

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount > self.sale.balance_due:
            raise forms.ValidationError(
                _("Payment cannot exceed the remaining balance of %(balance).2f.")
                % {"balance": self.sale.balance_due}
            )
        return amount


class PayPurchaseForm(forms.Form):
    payment_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=timezone.localdate,
    )
    payment_account = OperationalAccountChoiceField(queryset=Account.objects.none())
    amount = forms.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput())
    confirm = forms.BooleanField(
        label=_("I confirm that this payment should be posted."),
    )

    def __init__(self, *args, business=None, purchase=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.purchase = purchase
        self.fields["payment_account"].queryset = Account.objects.filter(
            business=business,
            is_active=True,
            system_role__in=[
                Account.SystemRole.CASH,
                Account.SystemRole.BANK,
                Account.SystemRole.MOBILE_MONEY,
            ],
        ).order_by("code")
        if not self.is_bound:
            self.initial.setdefault("amount", purchase.balance_due)
            self.initial.setdefault("idempotency_key", uuid.uuid4())
        self.fields["amount"].help_text = _(
            "Outstanding payable: %(balance).2f %(currency)s."
        ) % {"balance": purchase.balance_due, "currency": business.currency}
        self.fields["payment_account"].help_text = _(
            "The selected Cash, Bank, or Mobile Financial Services account will be credited."
        )

    def clean_payment_date(self):
        payment_date = self.cleaned_data["payment_date"]
        if payment_date > timezone.localdate():
            raise forms.ValidationError(_("Payment date cannot be in the future."))
        return payment_date

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount > self.purchase.balance_due:
            raise forms.ValidationError(
                _("Payment cannot exceed the remaining balance of %(balance).2f.")
                % {"balance": self.purchase.balance_due}
            )
        return amount


class BalanceSetoffForm(forms.Form):
    setoff_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        initial=timezone.localdate,
    )
    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput())
    confirm = forms.BooleanField(
        label=_("I confirm these receivable and payable allocations should be posted."),
    )

    def __init__(self, *args, sales=(), purchases=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.sales = list(sales)
        self.purchases = list(purchases)
        if not self.is_bound:
            self.initial.setdefault("idempotency_key", uuid.uuid4())

        maximum = min(
            sum((sale.balance_due for sale in self.sales), Decimal("0.00")),
            sum((purchase.balance_due for purchase in self.purchases), Decimal("0.00")),
        )
        sale_remaining = maximum
        purchase_remaining = maximum
        self.sale_rows = []
        self.purchase_rows = []
        for sale in self.sales:
            initial = min(sale.balance_due, sale_remaining)
            self.fields[f"sale_{sale.pk}"] = forms.DecimalField(
                required=False,
                max_digits=14,
                decimal_places=2,
                min_value=Decimal("0.01"),
                max_value=sale.balance_due,
                initial=initial if initial > 0 else None,
            )
            self.sale_rows.append((sale, self[f"sale_{sale.pk}"]))
            sale_remaining -= initial
        for purchase in self.purchases:
            initial = min(purchase.balance_due, purchase_remaining)
            self.fields[f"purchase_{purchase.pk}"] = forms.DecimalField(
                required=False,
                max_digits=14,
                decimal_places=2,
                min_value=Decimal("0.01"),
                max_value=purchase.balance_due,
                initial=initial if initial > 0 else None,
            )
            self.purchase_rows.append((purchase, self[f"purchase_{purchase.pk}"]))
            purchase_remaining -= initial

    def clean_setoff_date(self):
        setoff_date = self.cleaned_data["setoff_date"]
        if setoff_date > timezone.localdate():
            raise forms.ValidationError(_("Set-off date cannot be in the future."))
        return setoff_date

    def clean(self):
        cleaned = super().clean()
        sale_allocations = tuple(
            (sale.pk, cleaned.get(f"sale_{sale.pk}"))
            for sale in self.sales
            if cleaned.get(f"sale_{sale.pk}")
        )
        purchase_allocations = tuple(
            (purchase.pk, cleaned.get(f"purchase_{purchase.pk}"))
            for purchase in self.purchases
            if cleaned.get(f"purchase_{purchase.pk}")
        )
        sale_total = sum((amount for _, amount in sale_allocations), Decimal("0.00"))
        purchase_total = sum(
            (amount for _, amount in purchase_allocations),
            Decimal("0.00"),
        )
        if not sale_allocations or not purchase_allocations:
            raise forms.ValidationError(
                _("Allocate at least one receivable invoice and one payable purchase.")
            )
        if sale_total != purchase_total:
            raise forms.ValidationError(
                _("Receivable and payable allocation totals must be equal.")
            )
        cleaned["sale_allocations"] = sale_allocations
        cleaned["purchase_allocations"] = purchase_allocations
        cleaned["setoff_total"] = sale_total
        return cleaned
