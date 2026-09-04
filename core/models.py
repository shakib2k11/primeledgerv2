from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


class Business(models.Model):
    name = models.CharField(max_length=160)
    slug = models.SlugField(unique=True)
    legal_name = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    currency = models.CharField(max_length=3, default="BDT")
    locale = models.CharField(max_length=10, default="en-bd")
    inherit_default_units = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class AnnualReferenceSequence(models.Model):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="annual_reference_sequences",
    )
    year = models.PositiveSmallIntegerField()
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "year"],
                name="unique_business_annual_reference_sequence",
            ),
            models.CheckConstraint(
                condition=models.Q(year__gte=2000, year__lte=2099),
                name="reference_sequence_supported_year",
            ),
            models.CheckConstraint(
                condition=models.Q(last_value__gte=0, last_value__lte=999999),
                name="reference_sequence_value_range",
            ),
        ]

    def __str__(self):
        return f"{self.business} / {self.year}: {self.last_value}"


class Role(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="roles")
    name = models.CharField(max_length=80)
    permissions = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "name"], name="unique_business_role")]

    def __str__(self):
        return f"{self.business}: {self.name}"


class Membership(models.Model):
    class Level(models.TextChoices):
        BUSINESS_ADMIN = "business_admin", _("Business Admin")
        EMPLOYEE = "employee", _("Employee")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships")
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="memberships")
    level = models.CharField(max_length=20, choices=Level.choices, default=Level.EMPLOYEE)
    role = models.ForeignKey(Role, null=True, blank=True, on_delete=models.SET_NULL, related_name="memberships")
    can_view_all_transactions = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "business"], name="unique_business_membership")]

    def clean(self):
        if self.role_id and self.role.business_id != self.business_id:
            raise ValidationError(_("Membership and role must belong to the same business."))


class UserLanguagePreference(models.Model):
    class Language(models.TextChoices):
        ENGLISH = "en", _("English")
        BANGLA = "bn", _("বাংলা")

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="language_preference",
    )
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.ENGLISH)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user}: {self.language}"


class Party(models.Model):
    class Kind(models.TextChoices):
        CUSTOMER = "customer", _("Customer")
        SUPPLIER = "supplier", _("Supplier")
        BOTH = "both", _("Customer and Supplier")
        EMPLOYEE = "employee", _("Employee")

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="parties")
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    opening_balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    opening_balance_is_payable = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "name", "kind"], name="unique_business_party")]

    def __str__(self):
        return self.name

    def clean(self):
        if self.pk and not self.inherit_default_units and self.products.filter(
            unit__business__isnull=True
        ).exists():
            raise ValidationError({
                "inherit_default_units": _(
                    "Reassign products using default units to business-owned units before disabling inheritance."
                )
            })


class InventoryUnitQuerySet(models.QuerySet):
    def available_to(self, business, include_inactive=False):
        queryset = self
        if not include_inactive:
            queryset = queryset.filter(is_active=True)
        ownership = models.Q(business=business)
        if business.inherit_default_units:
            ownership |= models.Q(business__isnull=True)
        return queryset.filter(ownership)


class InventoryUnit(models.Model):
    business = models.ForeignKey(
        Business,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="inventory_units",
    )
    code = models.SlugField(max_length=30)
    name = models.CharField(max_length=80)
    symbol = models.CharField(max_length=16)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = InventoryUnitQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(business__isnull=True),
                name="unique_global_inventory_unit_code",
            ),
            models.UniqueConstraint(
                fields=["business", "code"],
                condition=models.Q(business__isnull=False),
                name="unique_business_inventory_unit_code",
            ),
        ]
        ordering = ["name", "code"]

    @property
    def is_default(self):
        return self.business_id is None

    def clean(self):
        self.code = self.code.strip().lower()
        self.name = self.name.strip()
        self.symbol = self.symbol.strip()
        duplicate = InventoryUnit.objects.filter(code__iexact=self.code)
        if self.business_id is None:
            duplicate = duplicate.filter(business__isnull=True)
        else:
            if InventoryUnit.objects.filter(
                business__isnull=True, code__iexact=self.code
            ).exclude(pk=self.pk).exists():
                raise ValidationError({"code": _("This code is reserved by a default unit.")})
            duplicate = duplicate.filter(business_id=self.business_id)
        if duplicate.exclude(pk=self.pk).exists():
            raise ValidationError({"code": _("This unit code is already in use.")})
        if self.pk:
            previous = InventoryUnit.objects.filter(pk=self.pk).only("code").first()
            if previous and previous.code != self.code and self.products.exists():
                raise ValidationError({
                    "code": _("The code cannot be changed after products use this unit.")
                })

    def __str__(self):
        return f"{self.name} ({self.symbol})"


class Product(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="products")
    name = models.CharField(max_length=160)
    sku = models.CharField(max_length=80, blank=True)
    barcode = models.CharField(max_length=80, blank=True)
    unit = models.ForeignKey(
        InventoryUnit,
        on_delete=models.PROTECT,
        related_name="products",
    )
    is_service = models.BooleanField(default=False)
    sale_price = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))])
    purchase_price = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))])
    reorder_level = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0"), validators=[MinValueValidator(Decimal("0"))])
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "sku"], condition=~models.Q(sku=""), name="unique_business_sku")]

    def __str__(self):
        return self.name

    def clean(self):
        from core.application.services import inventory_unit_is_available

        if self.unit_id and not inventory_unit_is_available(self.unit, self.business):
            raise ValidationError({"unit": _("Select an active unit available to this business.")})


class StockMovement(models.Model):
    class Direction(models.TextChoices):
        IN = "in", _("Inflow")
        OUT = "out", _("Outflow")

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="stock_movements")
    number = models.CharField(max_length=8)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_movements")
    direction = models.CharField(max_length=3, choices=Direction.choices)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))])
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))])
    reference = models.CharField(max_length=80, blank=True)
    occurred_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"],
                name="unique_business_stock_movement_number",
            ),
            models.CheckConstraint(
                condition=models.Q(number__regex=r"^[0-9]{8}$"),
                name="stock_movement_number_format",
            ),
        ]

    def clean(self):
        if self.number and (len(self.number) != 8 or not self.number.isdigit()):
            raise ValidationError({"number": _("The movement number must contain exactly eight digits.")})
        if self.product_id and self.product.business_id != self.business_id:
            raise ValidationError(_("Stock movement and product must belong to the same business."))
        if self.product_id and self.product.is_service:
            raise ValidationError(_("Stock movement cannot be recorded for a service."))

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError(_("Stock movements are append-only and cannot be edited."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Stock movements are append-only and cannot be deleted."))
