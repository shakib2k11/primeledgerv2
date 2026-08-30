from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping

from accounting.domain.repositories import TransactionRegisterReader


ZERO = Decimal("0.00")


@dataclass(frozen=True)
class AccountActivityRow:
    account: Any
    opening_debit: Decimal
    opening_credit: Decimal
    period_debit: Decimal
    period_credit: Decimal
    closing_debit: Decimal
    closing_credit: Decimal


@dataclass(frozen=True)
class AccountActivityTotals:
    opening_debit: Decimal = ZERO
    opening_credit: Decimal = ZERO
    period_debit: Decimal = ZERO
    period_credit: Decimal = ZERO
    closing_debit: Decimal = ZERO
    closing_credit: Decimal = ZERO


@dataclass(frozen=True)
class ContactClosingBalance:
    amount: Decimal
    position: str


@dataclass(frozen=True)
class TransactionRegisterRow:
    journal_id: int
    transaction_date: Any
    transaction_type: str
    transaction_type_code: str
    number: str
    journal_reference: str
    description: str
    party_name: str
    amount: Decimal
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True)
class TransactionRegisterTotals:
    transaction_count: int = 0
    amount: Decimal = ZERO
    debit: Decimal = ZERO
    credit: Decimal = ZERO


def build_transaction_register(
    reader: TransactionRegisterReader,
    *,
    business_id: int,
    date_from=None,
    date_to=None,
    query: str = "",
    transaction_type: str = "",
) -> tuple[list[TransactionRegisterRow], TransactionRegisterTotals]:
    rows = list(reader.read(
        business_id=business_id,
        date_from=date_from,
        date_to=date_to,
        query=query,
        transaction_type=transaction_type,
    ))
    totals = TransactionRegisterTotals(
        transaction_count=len(rows),
        amount=sum((row.amount for row in rows), ZERO),
        debit=sum((row.debit for row in rows), ZERO),
        credit=sum((row.credit for row in rows), ZERO),
    )
    return rows, totals


def _split_balance(value: Decimal) -> tuple[Decimal, Decimal]:
    if value >= ZERO:
        return value, ZERO
    return ZERO, -value


def calculate_contact_closing_balance(
    opening_amount: Decimal,
    opening_is_payable: bool,
    posted_debit: Decimal,
    posted_credit: Decimal,
) -> ContactClosingBalance:
    opening_signed = -opening_amount if opening_is_payable else opening_amount
    closing_signed = opening_signed + posted_debit - posted_credit
    if closing_signed > ZERO:
        return ContactClosingBalance(closing_signed, "Receivable")
    if closing_signed < ZERO:
        return ContactClosingBalance(-closing_signed, "Payable")
    return ContactClosingBalance(ZERO, "Settled")


def build_account_activity(
    accounts: Iterable[Any],
    opening_totals: Mapping[int, tuple[Decimal, Decimal]],
    period_totals: Mapping[int, tuple[Decimal, Decimal]],
) -> tuple[list[AccountActivityRow], AccountActivityTotals]:
    rows = []
    totals = AccountActivityTotals()
    for account in accounts:
        opening_debit_total, opening_credit_total = opening_totals.get(
            account.pk,
            (ZERO, ZERO),
        )
        period_debit, period_credit = period_totals.get(
            account.pk,
            (ZERO, ZERO),
        )
        opening_debit, opening_credit = _split_balance(
            opening_debit_total - opening_credit_total
        )
        closing_debit, closing_credit = _split_balance(
            opening_debit_total
            - opening_credit_total
            + period_debit
            - period_credit
        )
        row = AccountActivityRow(
            account=account,
            opening_debit=opening_debit,
            opening_credit=opening_credit,
            period_debit=period_debit,
            period_credit=period_credit,
            closing_debit=closing_debit,
            closing_credit=closing_credit,
        )
        rows.append(row)
        totals = AccountActivityTotals(
            opening_debit=totals.opening_debit + row.opening_debit,
            opening_credit=totals.opening_credit + row.opening_credit,
            period_debit=totals.period_debit + row.period_debit,
            period_credit=totals.period_credit + row.period_credit,
            closing_debit=totals.closing_debit + row.closing_debit,
            closing_credit=totals.closing_credit + row.closing_credit,
        )
    return rows, totals
