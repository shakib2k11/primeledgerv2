from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounting.domain.policies import LIQUID_ACCOUNT_SYSTEM_ROLES
from accounting.models import (
    Account,
    AccountTemplateApplication,
    ChartOfAccountsTemplate,
    ExpensePayment,
    ExpenseRecord,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    MoneyReceipt,
    Voucher,
)
from core.models import Business
from core.models import Party
from core.infrastructure.numbering import allocate_reference_number


@dataclass(frozen=True)
class TemplateApplicationResult:
    created: int
    matched: int


class DjangoAccountTemplateRepository:
    @transaction.atomic
    def apply(self, *, template_id: int, business_id: int, user_id: int | None = None):
        business = Business.objects.select_for_update().get(pk=business_id)
        template = (
            ChartOfAccountsTemplate.objects.select_for_update()
            .prefetch_related("lines")
            .get(pk=template_id, is_active=True)
        )
        created = 0
        matched = 0
        conflicts = []

        for line in template.lines.filter(is_active=True).order_by("code"):
            existing = Account.objects.filter(
                business=business,
                code__iexact=line.code,
            ).first()
            role_account = None
            if line.system_role:
                role_account = Account.objects.filter(
                    business=business,
                    system_role=line.system_role,
                ).first()
            if existing:
                if existing.account_type != line.account_type:
                    conflicts.append(
                        f"{line.code} is {existing.get_account_type_display()}, expected {line.get_account_type_display()}"
                    )
                    continue
                if line.system_role and role_account and role_account.pk != existing.pk:
                    conflicts.append(
                        f"{line.get_system_role_display()} is already assigned to {role_account.code}"
                    )
                    continue
                changed = []
                if line.system_role and not existing.system_role:
                    existing.system_role = line.system_role
                    changed.append("system_role")
                if changed:
                    existing.full_clean()
                    existing.save(update_fields=changed)
                matched += 1
                continue
            if role_account:
                matched += 1
                continue
            account = Account(
                business=business,
                code=line.code,
                name=line.name,
                account_type=line.account_type,
                system_role=line.system_role,
                is_system=True,
                is_active=line.account_is_active,
            )
            account.full_clean()
            account.save()
            created += 1

        if conflicts:
            raise ValidationError(
                "Template conflicts must be resolved first: " + "; ".join(conflicts)
            )
        AccountTemplateApplication.objects.create(
            business=business,
            template=template,
            applied_by_id=user_id,
            created_count=created,
            matched_count=matched,
        )
        return TemplateApplicationResult(created=created, matched=matched)


@dataclass(frozen=True)
class MoneyReceiptResult:
    receipt_id: int
    number: str
    created: bool


class DjangoMoneyReceiptRepository:
    @transaction.atomic
    def create_for_voucher(
        self,
        *,
        voucher_id: int,
        preferred_number: str,
        payment_account_id: int | None = None,
    ):
        voucher = (
            Voucher.objects.select_for_update()
            .select_related("business", "journal_entry")
            .get(pk=voucher_id)
        )
        existing = MoneyReceipt.objects.filter(voucher=voucher).first()
        if existing:
            return MoneyReceiptResult(existing.pk, existing.number, False)

        payment_account = None
        if payment_account_id:
            payment_account = Account.objects.filter(
                pk=payment_account_id,
                business=voucher.business,
                system_role__in=LIQUID_ACCOUNT_SYSTEM_ROLES,
            ).first()
        if payment_account is None:
            payment_account = (
                Account.objects.filter(
                    journal_lines__entry=voucher.journal_entry,
                    journal_lines__debit__gt=0,
                    business=voucher.business,
                    system_role__in=LIQUID_ACCOUNT_SYSTEM_ROLES,
                )
                .order_by("journal_lines__id")
                .first()
            )
        if voucher.voucher_type == Voucher.Type.SALE and payment_account is None:
            return None
        if voucher.voucher_type not in {Voucher.Type.SALE, Voucher.Type.RECEIPT}:
            return None

        Business.objects.select_for_update().get(pk=voucher.business_id)
        base_number = preferred_number.strip()[:40]
        candidate = base_number
        suffix = 2
        while MoneyReceipt.objects.filter(
            business=voucher.business,
            number=candidate,
        ).exists():
            marker = f"-{suffix}"
            candidate = f"{base_number[:40 - len(marker)]}{marker}"
            suffix += 1
        receipt = MoneyReceipt(
            business=voucher.business,
            number=candidate,
            voucher=voucher,
            party=voucher.party,
            payment_account=payment_account,
            amount=voucher.total,
            receipt_date=voucher.voucher_date,
        )
        receipt.full_clean()
        receipt.save()
        return MoneyReceiptResult(receipt.pk, receipt.number, True)


def _voucher_number(business_id, prefix, number):
    base = f"{prefix}-{number}"
    candidate = base
    suffix = 2
    while Voucher.objects.filter(business_id=business_id, number=candidate).exists():
        marker = f"-{suffix}"
        candidate = f"{base[:40 - len(marker)]}{marker}"
        suffix += 1
    return candidate


class DjangoExpenseRepository:
    @transaction.atomic
    def create(
        self,
        *,
        business_id,
        expense_date,
        expense_account_id,
        settlement,
        amount,
        description,
        idempotency_key,
        payee_id=None,
        payment_account_id=None,
        external_reference="",
        user_id=None,
    ):
        amount = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        existing = ExpenseRecord.objects.filter(
            business_id=business_id, idempotency_key=idempotency_key
        ).first()
        if existing:
            if (
                existing.expense_date != expense_date
                or existing.expense_account_id != expense_account_id
                or existing.settlement != settlement
                or existing.amount != amount
                or existing.payee_id != payee_id
                or existing.payment_account_id != payment_account_id
            ):
                raise ValidationError(
                    "This expense request key was already used with different details."
                )
            return existing
        if expense_date > timezone.localdate():
            raise ValidationError("Expense date cannot be in the future.")
        business = Business.objects.select_for_update().get(pk=business_id)
        period = FiscalPeriod.objects.select_for_update().filter(
            business_id=business_id,
            starts_on__lte=expense_date,
            ends_on__gte=expense_date,
        ).first()
        if period is None:
            raise ValidationError("No fiscal period covers the expense date.")
        if period.is_locked:
            raise ValidationError("The fiscal period covering the expense date is locked.")
        expense_account = Account.objects.select_for_update().filter(
            pk=expense_account_id,
            business_id=business_id,
            is_active=True,
            account_type=Account.Type.EXPENSE,
        ).first()
        if expense_account is None:
            raise ValidationError("Select an active expense account for this business.")
        payee = None
        if payee_id:
            payee = Party.objects.filter(
                pk=payee_id, business_id=business_id, is_active=True
            ).first()
            if payee is None:
                raise ValidationError("Select an active payee for this business.")
        payment_account = None
        payable_account = None
        if settlement == ExpenseRecord.Settlement.PAID:
            payment_account = Account.objects.select_for_update().filter(
                pk=payment_account_id,
                business_id=business_id,
                is_active=True,
                system_role__in=LIQUID_ACCOUNT_SYSTEM_ROLES,
            ).first()
            if payment_account is None:
                raise ValidationError(
                    "Select an active Cash, Bank, or Mobile Financial Services account."
                )
        elif settlement == ExpenseRecord.Settlement.PAYABLE:
            if payee is None:
                raise ValidationError("A pay-later expense requires a payee.")
            payable_account = Account.objects.select_for_update().filter(
                business_id=business_id,
                is_active=True,
                system_role=Account.SystemRole.ACCOUNTS_PAYABLE,
            ).first()
            if payable_account is None:
                raise ValidationError("An active Accounts Payable account is required.")
        else:
            raise ValidationError("Select whether this expense is paid now or payable later.")
        if amount <= 0:
            raise ValidationError("Expense amount must be greater than zero.")
        description = description.strip()
        if not description:
            raise ValidationError("Expense description is required.")

        number = allocate_reference_number(
            business_id=business_id, occurred_on=expense_date
        )
        journal = JournalEntry.objects.create(
            business=business,
            period=period,
            reference=f"EXPENSE:{number}",
            description=description,
            entry_date=expense_date,
            created_by_id=user_id,
        )
        JournalLine.objects.create(
            entry=journal,
            account=expense_account,
            party=payee,
            description=description,
            debit=amount,
        )
        JournalLine.objects.create(
            entry=journal,
            account=payment_account or payable_account,
            party=payee,
            description=description,
            credit=amount,
        )
        journal.validate_for_posting()
        JournalEntry.objects.filter(pk=journal.pk).update(posted=True)
        journal.posted = True
        voucher = Voucher(
            business=business,
            voucher_type=Voucher.Type.EXPENSE,
            number=_voucher_number(business_id, "E", number),
            party=payee,
            journal_entry=journal,
            total=amount,
            notes=external_reference,
            voucher_date=expense_date,
        )
        voucher.full_clean()
        voucher.save()
        expense = ExpenseRecord(
            business=business,
            number=number,
            expense_date=expense_date,
            payee=payee,
            expense_account=expense_account,
            settlement=settlement,
            payment_account=payment_account,
            payable_account=payable_account,
            amount=amount,
            description=description,
            external_reference=external_reference.strip(),
            journal_entry=journal,
            voucher=voucher,
            idempotency_key=idempotency_key,
            created_by_id=user_id,
        )
        expense.full_clean()
        expense.save()
        return expense


class DjangoExpensePaymentRepository:
    @transaction.atomic
    def pay(
        self,
        *,
        expense_id,
        business_id,
        payment_account_id,
        amount,
        payment_date,
        idempotency_key,
        notes="",
        user_id=None,
    ):
        amount = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        existing = ExpensePayment.objects.filter(
            business_id=business_id, idempotency_key=idempotency_key
        ).first()
        if existing:
            if (
                existing.expense_id != expense_id
                or existing.payment_account_id != payment_account_id
                or existing.amount != amount
                or existing.payment_date != payment_date
            ):
                raise ValidationError(
                    "This expense payment key was already used with different details."
                )
            return existing
        if payment_date > timezone.localdate():
            raise ValidationError("Payment date cannot be in the future.")
        expense = (
            ExpenseRecord.objects.select_for_update()
            .select_related("business", "payee", "payable_account")
            .filter(pk=expense_id, business_id=business_id)
            .first()
        )
        if expense is None:
            raise ExpenseRecord.DoesNotExist
        if expense.settlement != ExpenseRecord.Settlement.PAYABLE:
            raise ValidationError("Only pay-later expenses can receive allocated payments.")
        if payment_date < expense.expense_date:
            raise ValidationError("Payment date cannot precede the expense date.")
        payment_account = Account.objects.select_for_update().filter(
            pk=payment_account_id,
            business_id=business_id,
            is_active=True,
            system_role__in=LIQUID_ACCOUNT_SYSTEM_ROLES,
        ).first()
        if payment_account is None:
            raise ValidationError(
                "Select an active Cash, Bank, or Mobile Financial Services account."
            )
        period = FiscalPeriod.objects.select_for_update().filter(
            business_id=business_id,
            starts_on__lte=payment_date,
            ends_on__gte=payment_date,
        ).first()
        if period is None:
            raise ValidationError("No fiscal period covers the payment date.")
        if period.is_locked:
            raise ValidationError("The fiscal period covering the payment date is locked.")
        paid = ExpensePayment.objects.filter(expense=expense).aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0.00")
        remaining = expense.amount - paid
        if amount <= 0:
            raise ValidationError("Payment amount must be greater than zero.")
        if amount > remaining:
            raise ValidationError(
                f"Payment cannot exceed the remaining balance of {remaining:.2f}."
            )
        number = allocate_reference_number(
            business_id=business_id, occurred_on=payment_date
        )
        journal = JournalEntry.objects.create(
            business=expense.business,
            period=period,
            reference=f"EXPENSE-PAYMENT:{number}",
            description=f"Payment for expense {expense.number} — {expense.description}",
            entry_date=payment_date,
            created_by_id=user_id,
        )
        JournalLine.objects.create(
            entry=journal,
            account=expense.payable_account,
            party=expense.payee,
            description=f"Payable settled for {expense.number}",
            debit=amount,
        )
        JournalLine.objects.create(
            entry=journal,
            account=payment_account,
            party=expense.payee,
            description=f"Expense payment for {expense.number}",
            credit=amount,
        )
        journal.validate_for_posting()
        JournalEntry.objects.filter(pk=journal.pk).update(posted=True)
        journal.posted = True
        voucher = Voucher(
            business=expense.business,
            voucher_type=Voucher.Type.PAYMENT,
            number=_voucher_number(business_id, "P", number),
            party=expense.payee,
            journal_entry=journal,
            total=amount,
            notes=notes,
            voucher_date=payment_date,
        )
        voucher.full_clean()
        voucher.save()
        payment = ExpensePayment(
            business=expense.business,
            expense=expense,
            number=number,
            payment_date=payment_date,
            payment_account=payment_account,
            amount=amount,
            journal_entry=journal,
            voucher=voucher,
            notes=notes,
            idempotency_key=idempotency_key,
            paid_by_id=user_id,
        )
        payment.full_clean()
        payment.save()
        return payment
