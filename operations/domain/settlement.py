from dataclasses import dataclass


class SettlementMethod:
    DEFERRED = "deferred"
    CASH = "cash"
    BANK = "bank"
    MOBILE_MONEY = "mobile_money"

    LIQUID = {CASH, BANK, MOBILE_MONEY}


@dataclass(frozen=True)
class PostingAccountPlan:
    debit_role: str
    credit_role: str
    funds_side: str | None = None


def posting_account_plan(kind: str, settlement_method: str) -> PostingAccountPlan:
    """Return the ledger roles implied by an ordinary sale or purchase."""
    if settlement_method not in {
        SettlementMethod.DEFERRED,
        *SettlementMethod.LIQUID,
    }:
        raise ValueError("Unknown settlement method.")

    if kind == "sale":
        return PostingAccountPlan(
            debit_role=(
                "accounts_receivable"
                if settlement_method == SettlementMethod.DEFERRED
                else settlement_method
            ),
            credit_role="sales_revenue",
            funds_side=None if settlement_method == SettlementMethod.DEFERRED else "debit",
        )
    if kind == "purchase":
        return PostingAccountPlan(
            debit_role="inventory",
            credit_role=(
                "accounts_payable"
                if settlement_method == SettlementMethod.DEFERRED
                else settlement_method
            ),
            funds_side=None if settlement_method == SettlementMethod.DEFERRED else "credit",
        )
    raise ValueError("Settlement is supported only for sales and purchases.")
