from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from accounting.models import Account, FiscalPeriod, JournalEntry
from core.models import Business, Party, Product


class TradeDocument(models.Model):
    class Kind(models.TextChoices):
        SALE = "sale", "Sale"
        PURCHASE = "purchase", "Purchase"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        POSTED = "posted", "Posted"

    class DiscountType(models.TextChoices):
        NONE = "none", "No discount"
        FIXED = "fixed", "Fixed amount"
        PERCENTAGE = "percentage", "Percentage"

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="trade_documents")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    number = models.CharField(max_length=8)
    party = models.ForeignKey(Party, on_delete=models.PROTECT, related_name="trade_documents")
    period = models.ForeignKey(FiscalPeriod, on_delete=models.PROTECT, related_name="trade_documents")
    debit_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="debit_trade_documents")
    credit_account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="credit_trade_documents")
    document_date = models.DateField()
    notes = models.TextField(blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))])
    discount_type = models.CharField(max_length=10, choices=DiscountType.choices, default=DiscountType.NONE)
    discount_value = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), blank=True, validators=[MinValueValidator(Decimal("0"))])
    discount_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))])
    total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))])
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    journal_entry = models.OneToOneField(
        JournalEntry, null=True, blank=True, on_delete=models.PROTECT, related_name="trade_document"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "number"], name="unique_business_trade_number"),
            models.CheckConstraint(
                condition=models.Q(number__regex=r"^[0-9]{8}$"),
                name="trade_document_number_format",
            ),
            models.CheckConstraint(condition=models.Q(subtotal__gte=0), name="trade_document_nonnegative_subtotal"),
            models.CheckConstraint(condition=models.Q(discount_value__gte=0), name="trade_document_nonnegative_discount_value"),
            models.CheckConstraint(condition=models.Q(discount_amount__gte=0), name="trade_document_nonnegative_discount_amount"),
            models.CheckConstraint(condition=models.Q(discount_amount__lte=models.F("subtotal")), name="trade_document_discount_within_subtotal"),
            models.CheckConstraint(condition=models.Q(total__gte=0), name="trade_document_nonnegative_total"),
        ]
        ordering = ["-document_date", "-id"]

    def __str__(self):
        return f"{self.number} ({self.get_kind_display()})"

    def clean(self):
        if self.number and (len(self.number) != 8 or not self.number.isdigit()):
            raise ValidationError({"number": "The document number must contain exactly eight digits."})
        related = ("party", "period", "debit_account", "credit_account")
        for field in related:
            value = getattr(self, field, None)
            if value and value.business_id != self.business_id:
                raise ValidationError(f"Document {field.replace('_', ' ')} must belong to the same business.")
        if self.period_id and not self.period.starts_on <= self.document_date <= self.period.ends_on:
            raise ValidationError("Document date must fall within its fiscal period.")
        if self.period_id and self.period.is_locked:
            raise ValidationError("This fiscal period is locked.")
        if self.debit_account_id and self.debit_account_id == self.credit_account_id:
            raise ValidationError("Debit and credit accounts must be different.")
        if self.party_id:
            valid_kinds = {
                self.Kind.SALE: {Party.Kind.CUSTOMER, Party.Kind.BOTH},
                self.Kind.PURCHASE: {Party.Kind.SUPPLIER, Party.Kind.BOTH},
            }
            if self.party.kind not in valid_kinds.get(self.kind, set()):
                raise ValidationError(
                    "Sales require a customer and purchases require a supplier."
                )
        if self.kind == self.Kind.PURCHASE and (
            self.discount_type != self.DiscountType.NONE
            or self.discount_value != Decimal("0.00")
            or self.discount_amount != Decimal("0.00")
        ):
            raise ValidationError("Discounts are available on sales only.")
        if self.discount_type == self.DiscountType.NONE and self.discount_value:
            raise ValidationError({"discount_value": "Choose a discount type before entering a value."})
        if (
            self.discount_type == self.DiscountType.PERCENTAGE
            and self.discount_value >= Decimal("100")
        ):
            raise ValidationError({"discount_value": "Percentage discount must be less than 100."})

    def recalculate_total(self):
        subtotal = sum((line.line_total for line in self.lines.all()), Decimal("0.00"))
        self.set_totals(subtotal)
        return self.total

    def set_totals(self, subtotal):
        subtotal = Decimal(subtotal).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        discount = Decimal("0.00")
        if self.kind == self.Kind.SALE:
            if self.discount_type == self.DiscountType.FIXED:
                discount = self.discount_value.quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            elif self.discount_type == self.DiscountType.PERCENTAGE:
                discount = (
                    subtotal * self.discount_value / Decimal("100")
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if discount >= subtotal and discount > 0:
            raise ValidationError({
                "discount_value": "Discount must be less than the sale subtotal."
            })
        self.subtotal = subtotal
        self.discount_amount = discount
        self.total = subtotal - discount

    def save(self, *args, **kwargs):
        if self.pk:
            previous = TradeDocument.objects.filter(pk=self.pk).values("status", "number").first()
            if previous and previous["number"] != self.number:
                raise ValidationError("The automatic document number cannot be changed.")
            if previous and previous["status"] == self.Status.POSTED:
                raise ValidationError("Posted sales and purchases cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.POSTED:
            raise ValidationError("Posted sales and purchases cannot be deleted.")
        if self.period.is_locked:
            raise ValidationError("Documents in a locked fiscal period cannot be deleted.")
        return super().delete(*args, **kwargs)


class TradeLine(models.Model):
    document = models.ForeignKey(TradeDocument, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="trade_lines")
    description = models.CharField(max_length=255, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, validators=[MinValueValidator(Decimal("0.001"))])
    unit_price = models.DecimalField(max_digits=14, decimal_places=2, validators=[MinValueValidator(Decimal("0"))])
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))])

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="trade_line_positive_quantity"),
            models.CheckConstraint(condition=models.Q(unit_price__gte=0), name="trade_line_nonnegative_price"),
        ]

    def clean(self):
        if self.document_id and self.product_id and self.document.business_id != self.product.business_id:
            raise ValidationError("Line product must belong to the document business.")

    def save(self, *args, **kwargs):
        if self.document_id and self.document.status == TradeDocument.Status.POSTED:
            raise ValidationError("Lines in posted documents cannot be edited.")
        self.line_total = (self.quantity * self.unit_price).quantize(Decimal("0.01"))
        if not self.description and self.product_id:
            self.description = self.product.name
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.document.status == TradeDocument.Status.POSTED:
            raise ValidationError("Lines in posted documents cannot be deleted.")
        return super().delete(*args, **kwargs)
