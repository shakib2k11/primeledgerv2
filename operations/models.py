import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from accounting.models import Account, FiscalPeriod, JournalEntry, MoneyReceipt, Voucher
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

    class PaymentStatus(models.TextChoices):
        NOT_APPLICABLE = "not_applicable", "Not applicable"
        UNPAID = "unpaid", "Unpaid"
        PARTIAL = "partial", "Partially paid"
        PAID = "paid", "Paid"

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

    @property
    def paid_amount(self):
        if self.status != self.Status.POSTED:
            return Decimal("0.00")
        liquid_roles = {
            Account.SystemRole.CASH,
            Account.SystemRole.BANK,
            Account.SystemRole.MOBILE_MONEY,
        }
        if self.kind == self.Kind.SALE and self.debit_account.system_role in liquid_roles:
            return self.total
        if self.kind == self.Kind.PURCHASE and self.credit_account.system_role in liquid_roles:
            return self.total
        related_payments = (
            self.payments.all()
            if self.kind == self.Kind.SALE
            else self.supplier_payments.all()
        )
        setoff_allocations = (
            self.sale_setoff_allocations.all()
            if self.kind == self.Kind.SALE
            else self.purchase_setoff_allocations.all()
        )
        return sum(
            (payment.amount for payment in related_payments),
            Decimal("0.00"),
        ) + sum(
            (allocation.amount for allocation in setoff_allocations),
            Decimal("0.00"),
        )

    @property
    def balance_due(self):
        return max(self.total - self.paid_amount, Decimal("0.00"))

    @property
    def payment_status(self):
        if self.status != self.Status.POSTED:
            return self.PaymentStatus.NOT_APPLICABLE
        paid = self.paid_amount
        if paid <= 0:
            return self.PaymentStatus.UNPAID
        if paid < self.total:
            return self.PaymentStatus.PARTIAL
        return self.PaymentStatus.PAID

    def get_payment_status_display(self):
        return self.PaymentStatus(self.payment_status).label

    @property
    def can_receive_payment(self):
        return (
            self.kind == self.Kind.SALE
            and self.status == self.Status.POSTED
            and self.debit_account.system_role
            == Account.SystemRole.ACCOUNTS_RECEIVABLE
            and self.balance_due > 0
        )

    @property
    def can_pay_supplier(self):
        return (
            self.kind == self.Kind.PURCHASE
            and self.status == self.Status.POSTED
            and self.credit_account.system_role
            == Account.SystemRole.ACCOUNTS_PAYABLE
            and self.balance_due > 0
        )

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


class SalePayment(models.Model):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="sale_payments",
    )
    sale = models.ForeignKey(
        TradeDocument,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    number = models.CharField(max_length=8)
    payment_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="sale_payments",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    payment_date = models.DateField()
    journal_entry = models.OneToOneField(
        JournalEntry,
        on_delete=models.PROTECT,
        related_name="sale_payment",
    )
    money_receipt = models.OneToOneField(
        MoneyReceipt,
        on_delete=models.PROTECT,
        related_name="sale_payment",
    )
    notes = models.TextField(blank=True)
    idempotency_key = models.UUIDField(default=uuid.uuid4)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="sale_payments_received",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"],
                name="unique_business_sale_payment_number",
            ),
            models.UniqueConstraint(
                fields=["business", "idempotency_key"],
                name="unique_business_sale_payment_idempotency",
            ),
            models.CheckConstraint(
                condition=models.Q(number__regex=r"^[0-9]{8}$"),
                name="sale_payment_number_format",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="sale_payment_positive_amount",
            ),
        ]
        ordering = ["payment_date", "id"]

    def __str__(self):
        return f"{self.number} — {self.sale.number} ({self.amount:.2f})"

    def clean(self):
        liquid_roles = {
            Account.SystemRole.CASH,
            Account.SystemRole.BANK,
            Account.SystemRole.MOBILE_MONEY,
        }
        if self.number and (len(self.number) != 8 or not self.number.isdigit()):
            raise ValidationError({
                "number": "The payment number must contain exactly eight digits."
            })
        for field in ("sale", "payment_account", "journal_entry", "money_receipt"):
            value = getattr(self, field, None)
            if value and value.business_id != self.business_id:
                raise ValidationError(
                    f"Payment {field.replace('_', ' ')} must belong to the same business."
                )
        if self.sale_id and (
            self.sale.kind != TradeDocument.Kind.SALE
            or self.sale.status != TradeDocument.Status.POSTED
        ):
            raise ValidationError("Payments require a posted sale.")
        if self.sale_id and (
            self.sale.debit_account.system_role
            != Account.SystemRole.ACCOUNTS_RECEIVABLE
        ):
            raise ValidationError(
                "Payments can be allocated only to a sale posted to Accounts Receivable."
            )
        if self.payment_account_id and self.payment_account.system_role not in liquid_roles:
            raise ValidationError(
                "Payment account must be mapped as Cash, Bank, or Mobile Financial Services."
            )
        if self.journal_entry_id and not self.journal_entry.posted:
            raise ValidationError("A sale payment requires a posted journal entry.")
        if self.money_receipt_id:
            if self.money_receipt.amount != self.amount:
                raise ValidationError("Payment and money receipt amounts must match.")
            if self.money_receipt.party_id != self.sale.party_id:
                raise ValidationError("Payment and money receipt customers must match.")
            if self.money_receipt.payment_account_id != self.payment_account_id:
                raise ValidationError("Payment and money receipt accounts must match.")
            if self.money_receipt.receipt_date != self.payment_date:
                raise ValidationError("Payment and money receipt dates must match.")
            if (
                self.money_receipt.voucher.journal_entry_id
                != self.journal_entry_id
            ):
                raise ValidationError("Payment receipt must use the payment journal entry.")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Posted sale payments cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Posted sale payments cannot be deleted.")


class PurchasePayment(models.Model):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="purchase_payments",
    )
    purchase = models.ForeignKey(
        TradeDocument,
        on_delete=models.PROTECT,
        related_name="supplier_payments",
    )
    number = models.CharField(max_length=8)
    payment_account = models.ForeignKey(
        Account,
        on_delete=models.PROTECT,
        related_name="purchase_payments",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    payment_date = models.DateField()
    journal_entry = models.OneToOneField(
        JournalEntry,
        on_delete=models.PROTECT,
        related_name="purchase_payment",
    )
    voucher = models.OneToOneField(
        Voucher,
        on_delete=models.PROTECT,
        related_name="purchase_payment",
    )
    notes = models.TextField(blank=True)
    idempotency_key = models.UUIDField(default=uuid.uuid4)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="supplier_payments_made",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"],
                name="unique_business_purchase_payment_number",
            ),
            models.UniqueConstraint(
                fields=["business", "idempotency_key"],
                name="unique_business_purchase_payment_idempotency",
            ),
            models.CheckConstraint(
                condition=models.Q(number__regex=r"^[0-9]{8}$"),
                name="purchase_payment_number_format",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="purchase_payment_positive_amount",
            ),
        ]
        ordering = ["payment_date", "id"]

    def __str__(self):
        return f"{self.number} — {self.purchase.number} ({self.amount:.2f})"

    def clean(self):
        liquid_roles = {
            Account.SystemRole.CASH,
            Account.SystemRole.BANK,
            Account.SystemRole.MOBILE_MONEY,
        }
        if self.number and (len(self.number) != 8 or not self.number.isdigit()):
            raise ValidationError({
                "number": "The payment number must contain exactly eight digits."
            })
        for field in ("purchase", "payment_account", "journal_entry", "voucher"):
            value = getattr(self, field, None)
            if value and value.business_id != self.business_id:
                raise ValidationError(
                    f"Payment {field.replace('_', ' ')} must belong to the same business."
                )
        if self.purchase_id and (
            self.purchase.kind != TradeDocument.Kind.PURCHASE
            or self.purchase.status != TradeDocument.Status.POSTED
        ):
            raise ValidationError("Supplier payments require a posted purchase.")
        if self.purchase_id and (
            self.purchase.credit_account.system_role
            != Account.SystemRole.ACCOUNTS_PAYABLE
        ):
            raise ValidationError(
                "Payments can be allocated only to a purchase posted to Accounts Payable."
            )
        if self.payment_account_id and self.payment_account.system_role not in liquid_roles:
            raise ValidationError(
                "Payment account must be mapped as Cash, Bank, or Mobile Financial Services."
            )
        if self.journal_entry_id and not self.journal_entry.posted:
            raise ValidationError("A supplier payment requires a posted journal entry.")
        if self.voucher_id:
            if self.voucher.voucher_type != Voucher.Type.PAYMENT:
                raise ValidationError("Supplier payment requires a payment voucher.")
            if self.voucher.total != self.amount:
                raise ValidationError("Payment and voucher amounts must match.")
            if self.voucher.party_id != self.purchase.party_id:
                raise ValidationError("Payment and voucher suppliers must match.")
            if self.voucher.voucher_date != self.payment_date:
                raise ValidationError("Payment and voucher dates must match.")
            if self.voucher.journal_entry_id != self.journal_entry_id:
                raise ValidationError("Payment voucher must use the payment journal entry.")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Posted supplier payments cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Posted supplier payments cannot be deleted.")


class BalanceSetoff(models.Model):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="balance_setoffs",
    )
    party = models.ForeignKey(
        Party,
        on_delete=models.PROTECT,
        related_name="balance_setoffs",
    )
    number = models.CharField(max_length=8)
    setoff_date = models.DateField()
    total_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    journal_entry = models.OneToOneField(
        JournalEntry,
        on_delete=models.PROTECT,
        related_name="balance_setoff",
    )
    voucher = models.OneToOneField(
        Voucher,
        on_delete=models.PROTECT,
        related_name="balance_setoff",
    )
    notes = models.TextField(blank=True)
    idempotency_key = models.UUIDField(default=uuid.uuid4)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="balance_setoffs_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"],
                name="unique_business_balance_setoff_number",
            ),
            models.UniqueConstraint(
                fields=["business", "idempotency_key"],
                name="unique_business_balance_setoff_idempotency",
            ),
            models.CheckConstraint(
                condition=models.Q(number__regex=r"^[0-9]{8}$"),
                name="balance_setoff_number_format",
            ),
            models.CheckConstraint(
                condition=models.Q(total_amount__gt=0),
                name="balance_setoff_positive_total",
            ),
        ]
        ordering = ["-setoff_date", "-id"]

    def __str__(self):
        return f"{self.number} — {self.party.name} ({self.total_amount:.2f})"

    def clean(self):
        if self.number and (len(self.number) != 8 or not self.number.isdigit()):
            raise ValidationError({
                "number": "The set-off number must contain exactly eight digits."
            })
        for field in ("party", "journal_entry", "voucher"):
            value = getattr(self, field, None)
            if value and value.business_id != self.business_id:
                raise ValidationError(
                    f"Set-off {field.replace('_', ' ')} must belong to the same business."
                )
        if self.party_id and self.party.kind != Party.Kind.BOTH:
            raise ValidationError(
                "Balance set-off requires a contact classified as Customer and Supplier."
            )
        if self.journal_entry_id and not self.journal_entry.posted:
            raise ValidationError("A balance set-off requires a posted journal entry.")
        if self.voucher_id:
            if self.voucher.voucher_type != Voucher.Type.CONTRA:
                raise ValidationError("Balance set-off requires a contra voucher.")
            if self.voucher.total != self.total_amount:
                raise ValidationError("Set-off and voucher totals must match.")
            if self.voucher.party_id != self.party_id:
                raise ValidationError("Set-off and voucher contacts must match.")
            if self.voucher.voucher_date != self.setoff_date:
                raise ValidationError("Set-off and voucher dates must match.")
            if self.voucher.journal_entry_id != self.journal_entry_id:
                raise ValidationError("Contra voucher must use the set-off journal entry.")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Posted balance set-offs cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Posted balance set-offs cannot be deleted.")


class SaleSetoffAllocation(models.Model):
    setoff = models.ForeignKey(
        BalanceSetoff,
        on_delete=models.PROTECT,
        related_name="sale_allocations",
    )
    sale = models.ForeignKey(
        TradeDocument,
        on_delete=models.PROTECT,
        related_name="sale_setoff_allocations",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["setoff", "sale"],
                name="unique_setoff_sale_allocation",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="sale_setoff_allocation_positive",
            ),
        ]

    def clean(self):
        if self.setoff_id and self.sale_id:
            if self.sale.business_id != self.setoff.business_id:
                raise ValidationError("Allocated sale must belong to the set-off business.")
            if self.sale.party_id != self.setoff.party_id:
                raise ValidationError("Allocated sale must belong to the set-off contact.")
            if (
                self.sale.kind != TradeDocument.Kind.SALE
                or self.sale.status != TradeDocument.Status.POSTED
                or self.sale.debit_account.system_role
                != Account.SystemRole.ACCOUNTS_RECEIVABLE
            ):
                raise ValidationError("Set-off requires a posted Accounts Receivable sale.")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Set-off allocations cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Set-off allocations cannot be deleted.")


class PurchaseSetoffAllocation(models.Model):
    setoff = models.ForeignKey(
        BalanceSetoff,
        on_delete=models.PROTECT,
        related_name="purchase_allocations",
    )
    purchase = models.ForeignKey(
        TradeDocument,
        on_delete=models.PROTECT,
        related_name="purchase_setoff_allocations",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["setoff", "purchase"],
                name="unique_setoff_purchase_allocation",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="purchase_setoff_allocation_positive",
            ),
        ]

    def clean(self):
        if self.setoff_id and self.purchase_id:
            if self.purchase.business_id != self.setoff.business_id:
                raise ValidationError("Allocated purchase must belong to the set-off business.")
            if self.purchase.party_id != self.setoff.party_id:
                raise ValidationError("Allocated purchase must belong to the set-off contact.")
            if (
                self.purchase.kind != TradeDocument.Kind.PURCHASE
                or self.purchase.status != TradeDocument.Status.POSTED
                or self.purchase.credit_account.system_role
                != Account.SystemRole.ACCOUNTS_PAYABLE
            ):
                raise ValidationError("Set-off requires a posted Accounts Payable purchase.")

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Set-off allocations cannot be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Set-off allocations cannot be deleted.")
