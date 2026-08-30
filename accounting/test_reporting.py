from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from accounting.application.reporting import (
    TransactionRegisterRow,
    build_account_activity,
    build_transaction_register,
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

    def test_transaction_register_totals_reader_rows(self):
        rows = [
            TransactionRegisterRow(
                journal_id=1,
                transaction_date=None,
                transaction_type="Sale",
                transaction_type_code="sale",
                number="S-26000001",
                journal_reference="SALE:26000001",
                description="Sale",
                party_name="Customer",
                amount=Decimal("250.00"),
                debit=Decimal("410.00"),
                credit=Decimal("410.00"),
            ),
            TransactionRegisterRow(
                journal_id=2,
                transaction_date=None,
                transaction_type="Receipt",
                transaction_type_code="receipt",
                number="R-26000002",
                journal_reference="RECEIPT:26000002",
                description="Receipt",
                party_name="Customer",
                amount=Decimal("100.00"),
                debit=Decimal("100.00"),
                credit=Decimal("100.00"),
            ),
        ]

        class Reader:
            def read(self, **kwargs):
                self.kwargs = kwargs
                return rows

        reader = Reader()
        result, totals = build_transaction_register(
            reader,
            business_id=7,
            query="Customer",
            transaction_type="sale",
        )
        self.assertEqual(result, rows)
        self.assertEqual(totals.transaction_count, 2)
        self.assertEqual(totals.amount, Decimal("350.00"))
        self.assertEqual(totals.debit, Decimal("510.00"))
        self.assertEqual(totals.credit, Decimal("510.00"))
        self.assertEqual(reader.kwargs["business_id"], 7)
