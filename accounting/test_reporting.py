from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from accounting.application.reporting import (
    build_account_activity,
    calculate_contact_closing_balance,
)


class AccountActivityCalculationTests(SimpleTestCase):
    def test_builds_split_opening_activity_and_closing_balances(self):
        cash = SimpleNamespace(pk=1)
        revenue = SimpleNamespace(pk=2)
        rows, totals = build_account_activity(
            [cash, revenue],
            {
                1: (Decimal("100.00"), Decimal("0.00")),
                2: (Decimal("0.00"), Decimal("100.00")),
            },
            {
                1: (Decimal("250.00"), Decimal("20.00")),
                2: (Decimal("20.00"), Decimal("250.00")),
            },
        )

        self.assertEqual(rows[0].opening_debit, Decimal("100.00"))
        self.assertEqual(rows[0].closing_debit, Decimal("330.00"))
        self.assertEqual(rows[1].opening_credit, Decimal("100.00"))
        self.assertEqual(rows[1].closing_credit, Decimal("330.00"))
        self.assertEqual(totals.period_debit, Decimal("270.00"))
        self.assertEqual(totals.period_credit, Decimal("270.00"))
        self.assertEqual(totals.closing_debit, totals.closing_credit)

    def test_calculates_receivable_payable_and_settled_contact_positions(self):
        receivable = calculate_contact_closing_balance(
            Decimal("125.00"),
            False,
            Decimal("50.00"),
            Decimal("25.00"),
        )
        payable = calculate_contact_closing_balance(
            Decimal("100.00"),
            True,
            Decimal("20.00"),
            Decimal("50.00"),
        )
        settled = calculate_contact_closing_balance(
            Decimal("25.00"),
            False,
            Decimal("0.00"),
            Decimal("25.00"),
        )
        self.assertEqual((receivable.amount, receivable.position), (Decimal("150.00"), "Receivable"))
        self.assertEqual((payable.amount, payable.position), (Decimal("130.00"), "Payable"))
        self.assertEqual((settled.amount, settled.position), (Decimal("0.00"), "Settled"))
