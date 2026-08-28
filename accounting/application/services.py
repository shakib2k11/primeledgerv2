from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from accounting.domain.repositories import (
    AccountTemplateRepository,
    ExpensePaymentRepository,
    ExpenseRepository,
    MoneyReceiptRepository,
)


@dataclass(frozen=True)
class ApplyAccountTemplateCommand:
    template_id: int
    business_id: int
    user_id: int | None = None


def apply_account_template(command: ApplyAccountTemplateCommand, repository: AccountTemplateRepository):
    return repository.apply(
        template_id=command.template_id,
        business_id=command.business_id,
        user_id=command.user_id,
    )


@dataclass(frozen=True)
class CreateMoneyReceiptCommand:
    voucher_id: int
    preferred_number: str
    payment_account_id: int | None = None


def create_money_receipt(
    command: CreateMoneyReceiptCommand,
    repository: MoneyReceiptRepository,
):
    return repository.create_for_voucher(
        voucher_id=command.voucher_id,
        preferred_number=command.preferred_number,
        payment_account_id=command.payment_account_id,
    )


@dataclass(frozen=True)
class CreateExpenseCommand:
    business_id: int
    expense_date: date
    expense_account_id: int
    settlement: str
    amount: Decimal
    description: str
    idempotency_key: UUID
    payee_id: int | None = None
    payment_account_id: int | None = None
    external_reference: str = ""
    user_id: int | None = None


def create_expense(command: CreateExpenseCommand, repository: ExpenseRepository):
    return repository.create(**command.__dict__)


@dataclass(frozen=True)
class PayExpenseCommand:
    expense_id: int
    business_id: int
    payment_account_id: int
    amount: Decimal
    payment_date: date
    idempotency_key: UUID
    notes: str = ""
    user_id: int | None = None


def pay_expense(command: PayExpenseCommand, repository: ExpensePaymentRepository):
    return repository.pay(**command.__dict__)
