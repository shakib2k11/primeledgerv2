from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from django.utils import timezone

from .models import Business, InventoryUnit, Party, Product, StockMovement
from django.utils.translation import gettext_lazy as _


class BusinessForm(forms.ModelForm):
    class Meta:
        model = Business
        fields = [
            "name", "slug", "legal_name", "phone", "address", "currency", "locale",
            "inherit_default_units", "is_active",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}
        help_texts = {
            "slug": _("A unique lowercase identifier, for example amina-traders."),
            "currency": _("Three-letter currency code. Use BDT for Bangladesh."),
            "locale": _("Use en-bd for English or bn-bd for Bangla-ready presentation."),
            "inherit_default_units": _("Make Super Admin-maintained inventory units available to this business."),
        }


class BusinessAdminCreationForm(forms.Form):
    username = forms.CharField(max_length=150)
    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=False)
    password1 = forms.CharField(label=_("Password"), widget=forms.PasswordInput)
    password2 = forms.CharField(label=_("Confirm password"), widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise forms.ValidationError(_("A user with this username already exists."))
        return username

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password1")
        if password and password != cleaned.get("password2"):
            self.add_error("password2", _("The passwords do not match."))
        if password:
            try:
                validate_password(password)
            except ValidationError as exc:
                self.add_error("password1", exc)
        return cleaned


class PartyForm(forms.ModelForm):
    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business

    def clean(self):
        cleaned = super().clean()
        if self.business and Party.objects.filter(
            business=self.business,
            name__iexact=cleaned.get("name", ""),
        ).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(_("A party with this name already exists."))
        return cleaned

    class Meta:
        model = Party
        fields = ["name", "phone", "email", "address", "opening_balance", "opening_balance_is_payable"]
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}


class ProductForm(forms.ModelForm):
    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.instance.business = business
        self.fields["unit"].queryset = InventoryUnit.objects.available_to(business).order_by(
            "business_id", "name"
        )
        self.fields["unit"].empty_label = _("Select a unit")

    def clean_sku(self):
        sku = self.cleaned_data["sku"].strip()
        if sku and self.business and Product.objects.filter(
            business=self.business, sku__iexact=sku
        ).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(_("This SKU is already in use in this business."))
        return sku

    class Meta:
        model = Product
        fields = ["name", "sku", "barcode", "unit", "is_service", "sale_price", "purchase_price", "reorder_level"]


class InventoryUnitForm(forms.ModelForm):
    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.business = business
        self.fields["code"].help_text = _("Stable lowercase identifier, for example carton or kilogram.")
        self.fields["symbol"].help_text = _("Short display label, for example ctn or kg.")
        if self.instance.pk and self.instance.products.exists():
            self.fields["code"].disabled = True
            self.fields["code"].help_text = _("This code is locked because products already use the unit.")

    class Meta:
        model = InventoryUnit
        fields = ["code", "name", "symbol", "is_active"]


class UnitInheritanceForm(forms.ModelForm):
    class Meta:
        model = Business
        fields = ["inherit_default_units"]
        labels = {"inherit_default_units": _("Use Prime Ledger default units")}

    def clean_inherit_default_units(self):
        inherit = self.cleaned_data["inherit_default_units"]
        if not inherit and Product.objects.filter(
            business=self.instance,
            unit__business__isnull=True,
        ).exists():
            raise forms.ValidationError(
                _("Reassign products using default units to business-owned units before disabling inheritance.")
            )
        return inherit


class StockMovementForm(forms.ModelForm):
    def __init__(self, *args, business=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.business = business
        self.instance.business = business
        self.fields["product"].queryset = Product.objects.filter(
            business=business, is_active=True, is_service=False
        ).order_by("name")
        self.fields["occurred_at"].initial = timezone.localtime().strftime("%Y-%m-%dT%H:%M")
        self.fields["reference"].label = _("Source reference")
        self.fields["reference"].help_text = _("Optional party, adjustment, or source document reference.")

    class Meta:
        model = StockMovement
        fields = ["product", "direction", "quantity", "unit_cost", "reference", "occurred_at"]
        widgets = {"occurred_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}
