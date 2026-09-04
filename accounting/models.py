from decimal import Decimal
import uuid
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from core.models import Business, Party
from django.utils.translation import gettext_lazy as _


class Account(models.Model):
    class Type(models.TextChoices):
        ASSET = "asset", _("Asset")
        LIABILITY = "liability", _("Liability")
        EQUITY = "equity", _("Equity")
        INCOME = "income", _("Income")
        EXPENSE = "expense", _("Expense")

    class SystemRole(models.TextChoices):
        CASH = "cash", _("Cash")
        BANK = "bank", _("Bank")
        MOBILE_MONEY = "mobile_money", _("Mobile financial services")
        ACCOUNTS_RECEIVABLE = "accounts_receivable", _("Accounts receivable")
        INVENTORY = "inventory", _("Inventory")
        ACCOUNTS_PAYABLE = "accounts_payable", _("Accounts payable")
        OWNER_CAPITAL = "owner_capital", _("Owner capital")
        RETAINED_EARNINGS = "retained_earnings", _("Retained earnings")
        SALES_REVENUE = "sales_revenue", _("Sales revenue")
        SERVICE_REVENUE = "service_revenue", _("Service revenue")
        COST_OF_GOODS_SOLD = "cost_of_goods_sold", _("Cost of goods sold")

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="accounts")
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=120)
    account_type = models.CharField(max_length=10, choices=Type.choices)
    system_role = models.CharField(max_length=32, choices=SystemRole.choices, blank=True)
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["business", "code"], name="unique_business_account_code"),
            models.UniqueConstraint(
                fields=["business", "system_role"],
                condition=~models.Q(system_role=""),
                name="unique_business_account_system_role",
            ),
        ]
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"

    def clean(self):
        from accounting.domain.policies import SYSTEM_ROLE_ACCOUNT_TYPES

        expected_type = SYSTEM_ROLE_ACCOUNT_TYPES.get(self.system_role)
        if expected_type and self.account_type != expected_type:
            raise ValidationError({
                "system_role": _("This posting role requires an %(type)s account.")
                % {"type": expected_type}
            })


class ChartOfAccountsTemplate(models.Model):
    name = models.CharField(max_length=120, unique=True)
    description = models.TextField(blank=True)
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class AccountTemplateLine(models.Model):
    template = models.ForeignKey(
        ChartOfAccountsTemplate,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    code = models.CharField(max_length=20)
    name = models.CharField(max_length=120)
    account_type = models.CharField(max_length=10, choices=Account.Type.choices)
    system_role = models.CharField(max_length=32, choices=Account.SystemRole.choices, blank=True)
    account_is_active = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["template", "code"],
                name="unique_template_account_code",
            ),
            models.UniqueConstraint(
                fields=["template", "system_role"],
                condition=~models.Q(system_role=""),
                name="unique_template_account_system_role",
            ),
        ]
        ordering = ["code"]

    def clean(self):
        from accounting.domain.policies import SYSTEM_ROLE_ACCOUNT_TYPES

        expected_type = SYSTEM_ROLE_ACCOUNT_TYPES.get(self.system_role)
        if expected_type and self.account_type != expected_type:
            raise ValidationError({
                "system_role": _("This posting role requires an %(type)s account.")
                % {"type": expected_type}
            })

    def __str__(self):
        return f"{self.code} - {self.name}"


class AccountTemplateApplication(models.Model):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="account_template_applications",
    )
    template = models.ForeignKey(
        ChartOfAccountsTemplate,
        on_delete=models.PROTECT,
        related_name="applications",
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_count = models.PositiveIntegerField(default=0)
    matched_count = models.PositiveIntegerField(default=0)
    applied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-applied_at", "-id"]


class FiscalPeriod(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="fiscal_periods")
    name = models.CharField(max_length=80)
    starts_on = models.DateField()
    ends_on = models.DateField()
    is_locked = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "name"], name="unique_business_period")]

    def __str__(self):
        return f"{self.name} ({self.starts_on:%d %b %Y} – {self.ends_on:%d %b %Y})"

    def clean(self):
        if self.starts_on > self.ends_on:
            raise ValidationError(_("A fiscal period must end on or after it starts."))
        if self.business_id and FiscalPeriod.objects.filter(
            business_id=self.business_id,
            starts_on__lte=self.ends_on,
            ends_on__gte=self.starts_on,
        ).exclude(pk=self.pk).exists():
            raise ValidationError(_("Fiscal periods for a business cannot overlap."))
        if self.pk:
            previous = FiscalPeriod.objects.filter(pk=self.pk).first()
            if previous and previous.is_locked and (
                previous.starts_on != self.starts_on
                or previous.ends_on != self.ends_on
                or previous.business_id != self.business_id
            ):
                raise ValidationError(_("A locked fiscal period's boundaries cannot be changed."))
        if self.pk and self.journal_entries.exclude(
            entry_date__range=(self.starts_on, self.ends_on)
        ).exists():
            raise ValidationError(_("Fiscal period boundaries must include all existing entries."))


class JournalEntry(models.Model):
    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="journal_entries")
    period = models.ForeignKey(FiscalPeriod, on_delete=models.PROTECT, related_name="journal_entries")
    reference = models.CharField(max_length=80)
    description = models.CharField(max_length=255)
    entry_date = models.DateField()
    posted = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "reference"], name="unique_business_journal_reference"
            )
        ]

    @property
    def total_debit(self):
        return sum((line.debit for line in self.lines.all()), Decimal("0.00"))

    @property
    def total_credit(self):
        return sum((line.credit for line in self.lines.all()), Decimal("0.00"))

    def clean(self):
        if self.period_id and self.business_id != self.period.business_id:
            raise ValidationError(_("Journal entry and fiscal period must belong to the same business."))
        if self.period_id and not (
            self.period.starts_on <= self.entry_date <= self.period.ends_on
        ):
            raise ValidationError(_("Journal entry date must fall within its fiscal period."))

    def validate_for_posting(self):
        self.clean()
        if self.period.is_locked:
            raise ValidationError(_("This fiscal period is locked."))
        lines = list(self.lines.all())
        if len(lines) < 2:
            raise ValidationError(_("A journal entry requires at least two lines."))
        for line in lines:
            line.clean()
            if line.account.business_id != self.business_id:
                raise ValidationError(_("Every account must belong to the journal business."))
            if line.party_id and line.party.business_id != self.business_id:
                raise ValidationError(_("Every party must belong to the journal business."))
        if self.total_debit <= 0 or self.total_debit != self.total_credit:
            raise ValidationError(_("A journal entry must balance before posting."))

    def post(self):
        if self.posted:
            return
        self.validate_for_posting()
        self.posted = True
        self.save(update_fields=["posted"])

    def save(self, *args, **kwargs):
        if self.pk:
            previous = JournalEntry.objects.filter(pk=self.pk).values(
                "posted", "period_id", "business_id", "reference", "description", "entry_date"
            ).first()
            if previous and previous["posted"]:
                mutable_fields = ("period_id", "business_id", "reference", "description", "entry_date")
                if not self.posted or any(previous[field] != getattr(self, field) for field in mutable_fields):
                    raise ValidationError(_("Posted journal entries cannot be edited or returned to draft."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.posted or self.period.is_locked:
            raise ValidationError(_("Posted or locked journal entries cannot be deleted."))
        return super().delete(*args, **kwargs)


class JournalLine(models.Model):
    entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="journal_lines")
    party = models.ForeignKey(Party, null=True, blank=True, on_delete=models.PROTECT, related_name="journal_lines")
    description = models.CharField(max_length=255, blank=True)
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))])
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))])

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(debit__gt=0, credit=0)
                    | models.Q(credit__gt=0, debit=0)
                ),
                name="journal_line_exactly_one_amount",
            )
        ]

    def clean(self):
        if self.debit and self.credit:
            raise ValidationError(_("A journal line cannot have both debit and credit."))
        if not self.debit and not self.credit:
            raise ValidationError(_("A journal line requires a debit or credit amount."))
        if self.entry_id and self.account_id and self.entry.business_id != self.account.business_id:
            raise ValidationError(_("Journal line account must belong to the journal business."))
        if self.entry_id and self.party_id and self.entry.business_id != self.party.business_id:
            raise ValidationError(_("Journal line party must belong to the journal business."))

    def save(self, *args, **kwargs):
        if self.entry_id and (self.entry.posted or self.entry.period.is_locked):
            raise ValidationError(_("Lines in posted or locked journal entries cannot be edited."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.entry.posted or self.entry.period.is_locked:
            raise ValidationError(_("Lines in posted or locked journal entries cannot be deleted."))
        return super().delete(*args, **kwargs)


class Voucher(models.Model):
    class Type(models.TextChoices):
        SALE = "sale", _("Sale")
        PURCHASE = "purchase", _("Purchase")
        RECEIPT = "receipt", _("Receipt")
        PAYMENT = "payment", _("Payment")
        CONTRA = "contra", _("Contra")
        EXPENSE = "expense", _("Expense")
        RETURN = "return", _("Return")

    business = models.ForeignKey(Business, on_delete=models.CASCADE, related_name="vouchers")
    voucher_type = models.CharField(max_length=10, choices=Type.choices)
    number = models.CharField(max_length=40)
    party = models.ForeignKey(Party, null=True, blank=True, on_delete=models.PROTECT, related_name="vouchers")
    journal_entry = models.OneToOneField(JournalEntry, on_delete=models.PROTECT, related_name="voucher")
    total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))])
    notes = models.TextField(blank=True)
    voucher_date = models.DateField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["business", "number"], name="unique_business_voucher_number")]

    def __str__(self):
        return f"{self.number} ({self.get_voucher_type_display()})"

    def clean(self):
        if self.journal_entry_id and self.journal_entry.business_id != self.business_id:
            raise ValidationError(_("Voucher and journal entry must belong to the same business."))
        if self.party_id and self.party.business_id != self.business_id:
            raise ValidationError(_("Voucher and party must belong to the same business."))
        if self.journal_entry_id and self.voucher_date != self.journal_entry.entry_date:
            raise ValidationError(_("Voucher and journal entry dates must match."))
        if self.journal_entry_id and not self.journal_entry.posted:
            raise ValidationError(_("A voucher must reference a posted journal entry."))
        if self.journal_entry_id:
            transaction_lines = self.journal_entry.lines.all()
            if self.party_id:
                transaction_lines = transaction_lines.filter(party_id=self.party_id)
            transaction_debit = sum(
                (line.debit for line in transaction_lines), Decimal("0.00")
            )
            transaction_credit = sum(
                (line.credit for line in transaction_lines), Decimal("0.00")
            )
            if self.total not in {transaction_debit, transaction_credit}:
                raise ValidationError(
                    _("Voucher total must match the party-facing journal amount.")
                )

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError(_("Financial vouchers cannot be edited after creation."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Financial vouchers cannot be deleted."))


class MoneyReceipt(models.Model):
    business = models.ForeignKey(
        Business,
        on_delete=models.CASCADE,
        related_name="money_receipts",
    )
    number = models.CharField(max_length=40)
    voucher = models.OneToOneField(
        Voucher,
        on_delete=models.PROTECT,
        related_name="money_receipt",
    )
    party = models.ForeignKey(
        Party,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="money_receipts",
    )
    payment_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="money_receipts",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    receipt_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"],
                name="unique_business_money_receipt_number",
            ),
        ]
        ordering = ["-receipt_date", "-id"]

    def __str__(self):
        return f"{self.number} ({self.amount:.2f})"

    def clean(self):
        from accounting.domain.policies import LIQUID_ACCOUNT_SYSTEM_ROLES

        if self.voucher_id and self.voucher.business_id != self.business_id:
            raise ValidationError(_("Money receipt and voucher must belong to the same business."))
        if self.party_id and self.party.business_id != self.business_id:
            raise ValidationError(_("Money receipt and party must belong to the same business."))
        if self.payment_account_id and self.payment_account.business_id != self.business_id:
            raise ValidationError(_("Money receipt payment account must belong to the same business."))
        if self.voucher_id and not self.voucher.journal_entry.posted:
            raise ValidationError(_("A money receipt requires a posted voucher journal."))
        if self.voucher_id and self.amount != self.voucher.total:
            raise ValidationError(_("Money receipt amount must match its voucher."))
        if self.voucher_id and self.receipt_date != self.voucher.voucher_date:
            raise ValidationError(_("Money receipt and voucher dates must match."))
        if self.voucher_id and self.party_id != self.voucher.party_id:
            raise ValidationError(_("Money receipt and voucher parties must match."))
        if (
            self.voucher_id
            and self.voucher.voucher_type == Voucher.Type.SALE
            and (
                not self.payment_account_id
                or self.payment_account.system_role not in LIQUID_ACCOUNT_SYSTEM_ROLES
            )
        ):
            raise ValidationError(
                _("A sale money receipt requires a Cash, Bank, or Mobile Financial Services account.")
            )

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError(_("Money receipts cannot be edited after creation."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Money receipts cannot be deleted."))


class ExpenseRecord(models.Model):
    class Settlement(models.TextChoices):
        PAID = "paid", _("Paid now")
        PAYABLE = "payable", _("Pay later")

    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="expense_records"
    )
    number = models.CharField(max_length=8)
    expense_date = models.DateField()
    payee = models.ForeignKey(
        Party,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="expense_records",
    )
    expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="expense_records"
    )
    settlement = models.CharField(max_length=10, choices=Settlement.choices)
    payment_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="direct_expense_records",
    )
    payable_account = models.ForeignKey(
        Account,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="accrued_expense_records",
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    description = models.CharField(max_length=255)
    external_reference = models.CharField(max_length=80, blank=True)
    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.PROTECT, related_name="expense_record"
    )
    voucher = models.OneToOneField(
        Voucher, on_delete=models.PROTECT, related_name="expense_record"
    )
    idempotency_key = models.UUIDField(default=uuid.uuid4)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="expense_records_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"],
                name="unique_business_expense_number",
            ),
            models.UniqueConstraint(
                fields=["business", "idempotency_key"],
                name="unique_business_expense_idempotency",
            ),
            models.CheckConstraint(
                condition=models.Q(number__regex=r"^[0-9]{8}$"),
                name="expense_number_format",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="expense_positive_amount",
            ),
        ]
        ordering = ["-expense_date", "-id"]

    def __str__(self):
        return f"{self.number} — {self.description} ({self.amount:.2f})"

    @property
    def paid_amount(self):
        if self.settlement == self.Settlement.PAID:
            return self.amount
        return sum((payment.amount for payment in self.payments.all()), Decimal("0.00"))

    @property
    def balance_due(self):
        return max(self.amount - self.paid_amount, Decimal("0.00"))

    @property
    def payment_status(self):
        if self.balance_due <= 0:
            return "paid"
        if self.paid_amount > 0:
            return "partial"
        return "unpaid"

    @property
    def can_pay(self):
        return self.settlement == self.Settlement.PAYABLE and self.balance_due > 0

    def clean(self):
        if self.number and (len(self.number) != 8 or not self.number.isdigit()):
            raise ValidationError({"number": _("Expense number must contain eight digits.")})
        for field in (
            "payee", "expense_account", "payment_account", "payable_account",
            "journal_entry", "voucher",
        ):
            value = getattr(self, field, None)
            if value and value.business_id != self.business_id:
                raise ValidationError(_("Expense relationships must belong to the same business."))
        if self.expense_account_id and self.expense_account.account_type != Account.Type.EXPENSE:
            raise ValidationError(_("Expense category must be an expense account."))
        liquid_roles = {
            Account.SystemRole.CASH,
            Account.SystemRole.BANK,
            Account.SystemRole.MOBILE_MONEY,
        }
        if self.settlement == self.Settlement.PAID:
            if not self.payment_account_id or self.payment_account.system_role not in liquid_roles:
                raise ValidationError(_("Paid expenses require a Cash, Bank, or Mobile Financial Services account."))
            if self.payable_account_id:
                raise ValidationError(_("A paid expense cannot use a payable account."))
        elif self.settlement == self.Settlement.PAYABLE:
            if not self.payee_id:
                raise ValidationError(_("A pay-later expense requires a payee."))
            if (
                not self.payable_account_id
                or self.payable_account.system_role != Account.SystemRole.ACCOUNTS_PAYABLE
            ):
                raise ValidationError(_("Pay-later expenses require the Accounts Payable account."))
            if self.payment_account_id:
                raise ValidationError(_("A pay-later expense cannot have an immediate payment account."))
        if self.journal_entry_id and not self.journal_entry.posted:
            raise ValidationError(_("An expense requires a posted journal entry."))
        if self.voucher_id:
            if self.voucher.voucher_type != Voucher.Type.EXPENSE:
                raise ValidationError(_("An expense requires an expense voucher."))
            if self.voucher.total != self.amount:
                raise ValidationError(_("Expense and voucher amounts must match."))
            if self.voucher.journal_entry_id != self.journal_entry_id:
                raise ValidationError(_("Expense voucher must use the expense journal entry."))

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError(_("Posted expenses cannot be edited."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Posted expenses cannot be deleted."))


class ExpensePayment(models.Model):
    business = models.ForeignKey(
        Business, on_delete=models.CASCADE, related_name="expense_payments"
    )
    expense = models.ForeignKey(
        ExpenseRecord, on_delete=models.PROTECT, related_name="payments"
    )
    number = models.CharField(max_length=8)
    payment_date = models.DateField()
    payment_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="expense_payments"
    )
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    journal_entry = models.OneToOneField(
        JournalEntry, on_delete=models.PROTECT, related_name="expense_payment"
    )
    voucher = models.OneToOneField(
        Voucher, on_delete=models.PROTECT, related_name="expense_payment"
    )
    notes = models.TextField(blank=True)
    idempotency_key = models.UUIDField(default=uuid.uuid4)
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.SET_NULL,
        related_name="expense_payments_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["business", "number"],
                name="unique_business_expense_payment_number",
            ),
            models.UniqueConstraint(
                fields=["business", "idempotency_key"],
                name="unique_business_expense_payment_idempotency",
            ),
            models.CheckConstraint(
                condition=models.Q(number__regex=r"^[0-9]{8}$"),
                name="expense_payment_number_format",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="expense_payment_positive_amount",
            ),
        ]
        ordering = ["payment_date", "id"]

    def clean(self):
        for field in ("expense", "payment_account", "journal_entry", "voucher"):
            value = getattr(self, field, None)
            if value and value.business_id != self.business_id:
                raise ValidationError(_("Expense payment relationships must belong to the same business."))
        if self.expense_id and self.expense.settlement != ExpenseRecord.Settlement.PAYABLE:
            raise ValidationError(_("Payments can be allocated only to pay-later expenses."))
        if self.payment_account_id and self.payment_account.system_role not in {
            Account.SystemRole.CASH,
            Account.SystemRole.BANK,
            Account.SystemRole.MOBILE_MONEY,
        }:
            raise ValidationError(_("Payment account must be Cash, Bank, or Mobile Financial Services."))
        if self.journal_entry_id and not self.journal_entry.posted:
            raise ValidationError(_("An expense payment requires a posted journal entry."))
        if self.voucher_id:
            if self.voucher.voucher_type != Voucher.Type.PAYMENT:
                raise ValidationError(_("An expense payment requires a payment voucher."))
            if self.voucher.total != self.amount:
                raise ValidationError(_("Expense payment and voucher amounts must match."))
            if self.voucher.journal_entry_id != self.journal_entry_id:
                raise ValidationError(_("Payment voucher must use the payment journal entry."))

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError(_("Posted expense payments cannot be edited."))
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_("Posted expense payments cannot be deleted."))
