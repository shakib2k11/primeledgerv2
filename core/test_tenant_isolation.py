import json
import uuid
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounting.models import (
    Account,
    ExpenseRecord,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    MoneyReceipt,
    Voucher,
)
from core.models import Business, InventoryUnit, Membership, Party, Product, StockMovement
from operations.models import BalanceSetoff, TradeDocument


class TenantIsolationBusinessCases(TestCase):
    """Exercise tenant boundaries across delivery surfaces and nested relationships."""

    maxDiff = None

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username="isolation-owner", password="test-password"
        )
        cls.business = Business.objects.create(name="Visible Ledger", slug="visible-ledger")
        cls.other = Business.objects.create(name="Hidden Ledger", slug="hidden-ledger")
        Membership.objects.create(
            user=cls.user,
            business=cls.business,
            level=Membership.Level.BUSINESS_ADMIN,
        )

        cls.unit = InventoryUnit.objects.get(business__isnull=True, code="piece")
        cls.party = Party.objects.create(
            business=cls.business,
            name="VISIBLE-PARTY-MARKER",
            kind=Party.Kind.CUSTOMER,
        )
        cls.other_party = Party.objects.create(
            business=cls.other,
            name="HIDDEN-PARTY-MARKER",
            kind=Party.Kind.SUPPLIER,
        )
        cls.product = Product.objects.create(
            business=cls.business,
            name="VISIBLE-PRODUCT-MARKER",
            sku="VISIBLE-SKU",
            unit=cls.unit,
        )
        cls.other_product = Product.objects.create(
            business=cls.other,
            name="HIDDEN-PRODUCT-MARKER",
            sku="HIDDEN-SKU",
            unit=cls.unit,
        )
        cls.period = FiscalPeriod.objects.create(
            business=cls.business,
            name="VISIBLE-PERIOD-MARKER",
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        cls.other_period = FiscalPeriod.objects.create(
            business=cls.other,
            name="HIDDEN-PERIOD-MARKER",
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        cls.accounts = cls._make_accounts(cls.business, "VISIBLE")
        cls.other_accounts = cls._make_accounts(cls.other, "HIDDEN")

        cls.journal, cls.voucher = cls._make_posted_artifact(
            cls.business,
            cls.period,
            cls.party,
            cls.accounts,
            "VISIBLE-JOURNAL-MARKER",
            "VISIBLE-VOUCHER-MARKER",
            Voucher.Type.RECEIPT,
        )
        cls.other_journal, cls.other_voucher = cls._make_posted_artifact(
            cls.other,
            cls.other_period,
            cls.other_party,
            cls.other_accounts,
            "HIDDEN-JOURNAL-MARKER",
            "HIDDEN-VOUCHER-MARKER",
            Voucher.Type.RECEIPT,
        )

        cls.sale = cls._make_document(
            cls.business,
            cls.party,
            cls.period,
            cls.accounts,
            TradeDocument.Kind.SALE,
            "26000101",
        )
        cls.other_sale = cls._make_document(
            cls.other,
            cls.other_party,
            cls.other_period,
            cls.other_accounts,
            TradeDocument.Kind.SALE,
            "26000901",
        )
        cls.purchase = cls._make_document(
            cls.business,
            cls.party,
            cls.period,
            cls.accounts,
            TradeDocument.Kind.PURCHASE,
            "26000102",
        )
        cls.other_purchase = cls._make_document(
            cls.other,
            cls.other_party,
            cls.other_period,
            cls.other_accounts,
            TradeDocument.Kind.PURCHASE,
            "26000902",
        )

        cls.expense = cls._make_expense(
            cls.business,
            cls.period,
            cls.party,
            cls.accounts,
            "26000103",
            "VISIBLE-EXPENSE-MARKER",
        )
        cls.other_expense = cls._make_expense(
            cls.other,
            cls.other_period,
            cls.other_party,
            cls.other_accounts,
            "26000903",
            "HIDDEN-EXPENSE-MARKER",
        )
        cls.setoff = cls._make_setoff(
            cls.business,
            cls.period,
            cls.party,
            cls.accounts,
            "26000104",
            "VISIBLE-SETOFF-JOURNAL",
        )
        cls.other_setoff = cls._make_setoff(
            cls.other,
            cls.other_period,
            cls.other_party,
            cls.other_accounts,
            "26000904",
            "HIDDEN-SETOFF-JOURNAL",
        )
        cls.receipt = cls._make_receipt(
            cls.business,
            cls.period,
            cls.party,
            cls.accounts,
            "VISIBLE-RECEIPT-MARKER",
        )
        cls.other_receipt = cls._make_receipt(
            cls.other,
            cls.other_period,
            cls.other_party,
            cls.other_accounts,
            "HIDDEN-RECEIPT-MARKER",
        )
        cls.movement = StockMovement.objects.create(
            business=cls.business,
            number="26000105",
            product=cls.product,
            direction=StockMovement.Direction.IN,
            quantity=Decimal("2.000"),
            unit_cost=Decimal("3.00"),
            reference="VISIBLE-STOCK-MARKER",
            occurred_at=timezone.make_aware(timezone.datetime(2026, 8, 15, 9, 0)),
        )
        cls.other_movement = StockMovement.objects.create(
            business=cls.other,
            number="26000905",
            product=cls.other_product,
            direction=StockMovement.Direction.IN,
            quantity=Decimal("2.000"),
            unit_cost=Decimal("3.00"),
            reference="HIDDEN-STOCK-MARKER",
            occurred_at=timezone.make_aware(timezone.datetime(2026, 8, 15, 9, 0)),
        )

    @classmethod
    def _make_accounts(cls, business, prefix):
        definitions = {
            "cash": ("1000", Account.Type.ASSET, Account.SystemRole.CASH),
            "receivable": (
                "1100",
                Account.Type.ASSET,
                Account.SystemRole.ACCOUNTS_RECEIVABLE,
            ),
            "inventory": ("1200", Account.Type.ASSET, Account.SystemRole.INVENTORY),
            "payable": (
                "2000",
                Account.Type.LIABILITY,
                Account.SystemRole.ACCOUNTS_PAYABLE,
            ),
            "sales": ("4000", Account.Type.INCOME, Account.SystemRole.SALES_REVENUE),
            "expense": ("5000", Account.Type.EXPENSE, ""),
        }
        return {
            key: Account.objects.create(
                business=business,
                code=code,
                name=f"{prefix}-{key.upper()}-MARKER",
                account_type=account_type,
                system_role=role,
            )
            for key, (code, account_type, role) in definitions.items()
        }

    @classmethod
    def _make_posted_artifact(
        cls, business, period, party, accounts, reference, voucher_number, voucher_type
    ):
        journal = JournalEntry.objects.create(
            business=business,
            period=period,
            reference=reference,
            description=reference,
            entry_date=date(2026, 8, 15),
        )
        JournalLine.objects.create(
            entry=journal,
            account=accounts["cash"],
            party=party,
            debit=Decimal("1.00"),
        )
        JournalLine.objects.create(
            entry=journal,
            account=accounts["sales"],
            party=party,
            credit=Decimal("1.00"),
        )
        JournalEntry.objects.filter(pk=journal.pk).update(posted=True)
        journal.refresh_from_db()
        voucher = Voucher.objects.create(
            business=business,
            voucher_type=voucher_type,
            number=voucher_number,
            party=party,
            journal_entry=journal,
            total=Decimal("1.00"),
            voucher_date=date(2026, 8, 15),
        )
        return journal, voucher

    @classmethod
    def _make_document(cls, business, party, period, accounts, kind, number):
        return TradeDocument.objects.create(
            business=business,
            kind=kind,
            number=number,
            party=party,
            period=period,
            debit_account=(
                accounts["receivable"] if kind == TradeDocument.Kind.SALE else accounts["inventory"]
            ),
            credit_account=(
                accounts["sales"] if kind == TradeDocument.Kind.SALE else accounts["payable"]
            ),
            document_date=date(2026, 8, 15),
            subtotal=Decimal("10.00"),
            total=Decimal("10.00"),
            status=TradeDocument.Status.POSTED,
        )

    @classmethod
    def _make_expense(cls, business, period, party, accounts, number, marker):
        journal, voucher = cls._make_posted_artifact(
            business,
            period,
            party,
            accounts,
            f"{marker}-JOURNAL",
            f"{marker}-VOUCHER",
            Voucher.Type.EXPENSE,
        )
        return ExpenseRecord.objects.create(
            business=business,
            number=number,
            expense_date=date(2026, 8, 15),
            payee=party,
            expense_account=accounts["expense"],
            settlement=ExpenseRecord.Settlement.PAID,
            payment_account=accounts["cash"],
            amount=Decimal("1.00"),
            description=marker,
            journal_entry=journal,
            voucher=voucher,
        )

    @classmethod
    def _make_setoff(cls, business, period, party, accounts, number, reference):
        journal, voucher = cls._make_posted_artifact(
            business,
            period,
            party,
            accounts,
            reference,
            f"{reference}-VOUCHER",
            Voucher.Type.CONTRA,
        )
        return BalanceSetoff.objects.create(
            business=business,
            party=party,
            number=number,
            setoff_date=date(2026, 8, 15),
            total_amount=Decimal("1.00"),
            journal_entry=journal,
            voucher=voucher,
        )

    @classmethod
    def _make_receipt(cls, business, period, party, accounts, marker):
        journal, voucher = cls._make_posted_artifact(
            business,
            period,
            party,
            accounts,
            f"{marker}-JOURNAL",
            f"{marker}-VOUCHER",
            Voucher.Type.RECEIPT,
        )
        return MoneyReceipt.objects.create(
            business=business,
            number=marker,
            voucher=voucher,
            party=party,
            payment_account=accounts["cash"],
            amount=Decimal("1.00"),
            receipt_date=date(2026, 8, 15),
        )

    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client.force_login(self.user)
        self.header = {"HTTP_X_BUSINESS_ID": str(self.business.pk)}

    def _api_text(self, response):
        return json.dumps(response.data, default=str, ensure_ascii=False)

    def test_foreign_and_unknown_tenant_contexts_are_indistinguishable(self):
        for endpoint in (
            "/api/v1/parties/",
            "/api/v1/products/",
            "/api/v1/accounts/",
            "/api/v1/fiscal-periods/",
            "/api/v1/journal-entries/",
            "/api/v1/vouchers/",
            "/api/v1/expenses/",
            "/api/v1/sales/",
            "/api/v1/purchases/",
            "/api/v1/balance-setoffs/",
        ):
            with self.subTest(endpoint=endpoint):
                foreign = self.api.get(endpoint, HTTP_X_BUSINESS_ID=str(self.other.pk))
                unknown = self.api.get(endpoint, HTTP_X_BUSINESS_ID="999999999")
                self.assertEqual(foreign.status_code, 404)
                self.assertEqual(foreign.data, unknown.data)

    def test_api_lists_never_mix_tenant_records(self):
        cases = (
            ("/api/v1/parties/", "VISIBLE-PARTY-MARKER", "HIDDEN-PARTY-MARKER"),
            ("/api/v1/products/", "VISIBLE-PRODUCT-MARKER", "HIDDEN-PRODUCT-MARKER"),
            ("/api/v1/accounts/", "VISIBLE-CASH-MARKER", "HIDDEN-CASH-MARKER"),
            ("/api/v1/fiscal-periods/", "VISIBLE-PERIOD-MARKER", "HIDDEN-PERIOD-MARKER"),
            ("/api/v1/journal-entries/", "VISIBLE-JOURNAL-MARKER", "HIDDEN-JOURNAL-MARKER"),
            ("/api/v1/vouchers/", "VISIBLE-VOUCHER-MARKER", "HIDDEN-VOUCHER-MARKER"),
            ("/api/v1/expenses/", "VISIBLE-EXPENSE-MARKER", "HIDDEN-EXPENSE-MARKER"),
            ("/api/v1/sales/", "26000101", "26000901"),
            ("/api/v1/purchases/", "26000102", "26000902"),
            ("/api/v1/balance-setoffs/", "26000104", "26000904"),
        )
        for endpoint, visible, hidden in cases:
            with self.subTest(endpoint=endpoint):
                response = self.api.get(endpoint, **self.header)
                self.assertEqual(response.status_code, 200, response.data)
                payload = self._api_text(response)
                self.assertIn(visible, payload)
                self.assertNotIn(hidden, payload)

    def test_api_object_lookups_hide_foreign_records_like_missing_records(self):
        cases = (
            ("parties", self.other_party.pk),
            ("products", self.other_product.pk),
            ("accounts", self.other_accounts["cash"].pk),
            ("fiscal-periods", self.other_period.pk),
            ("journal-entries", self.other_journal.pk),
            ("vouchers", self.other_voucher.pk),
            ("expenses", self.other_expense.pk),
            ("sales", self.other_sale.pk),
            ("purchases", self.other_purchase.pk),
            ("balance-setoffs", self.other_setoff.pk),
        )
        for resource, foreign_pk in cases:
            with self.subTest(resource=resource):
                foreign = self.api.get(f"/api/v1/{resource}/{foreign_pk}/", **self.header)
                missing = self.api.get(f"/api/v1/{resource}/999999999/", **self.header)
                self.assertEqual(foreign.status_code, 404)
                self.assertEqual(foreign.data, missing.data)

    def test_cross_tenant_api_actions_cannot_mutate_financial_records(self):
        actions = (
            (f"/api/v1/journal-entries/{self.other_journal.pk}/post/", {}),
            (f"/api/v1/sales/{self.other_sale.pk}/post/", {}),
            (f"/api/v1/sales/{self.other_sale.pk}/receive-payment/", {}),
            (f"/api/v1/purchases/{self.other_purchase.pk}/post/", {}),
            (f"/api/v1/purchases/{self.other_purchase.pk}/pay-supplier/", {}),
            (f"/api/v1/expenses/{self.other_expense.pk}/pay/", {}),
        )
        original_sale_status = self.other_sale.status
        original_purchase_status = self.other_purchase.status
        for endpoint, payload in actions:
            with self.subTest(endpoint=endpoint):
                response = self.api.post(endpoint, payload, format="json", **self.header)
                self.assertEqual(response.status_code, 404, response.data)
        self.other_sale.refresh_from_db()
        self.other_purchase.refresh_from_db()
        self.assertEqual(self.other_sale.status, original_sale_status)
        self.assertEqual(self.other_purchase.status, original_purchase_status)

    def test_foreign_related_ids_are_rejected_without_creating_stock_or_journals(self):
        movement_count = StockMovement.objects.filter(business=self.business).count()
        movement = self.api.post(
            "/api/v1/stock-movements/",
            {
                "product": self.other_product.pk,
                "direction": StockMovement.Direction.IN,
                "quantity": "1.000",
                "unit_cost": "5.00",
                "reference": "FOREIGN-PRODUCT",
                "occurred_at": "2026-08-16T09:00:00+06:00",
            },
            format="json",
            **self.header,
        )
        self.assertEqual(movement.status_code, 400)
        self.assertEqual(
            StockMovement.objects.filter(business=self.business).count(), movement_count
        )

        journal_count = JournalEntry.objects.filter(business=self.business).count()
        base = {
            "period": self.period.pk,
            "reference": "CROSS-RELATION",
            "description": "Must not be created",
            "entry_date": "2026-08-16",
            "lines": [
                {
                    "account": self.accounts["cash"].pk,
                    "party": self.party.pk,
                    "debit": "5.00",
                    "credit": "0.00",
                },
                {
                    "account": self.accounts["sales"].pk,
                    "party": self.party.pk,
                    "debit": "0.00",
                    "credit": "5.00",
                },
            ],
        }
        invalid_payloads = []
        foreign_period = {**base, "period": self.other_period.pk}
        invalid_payloads.append(foreign_period)
        foreign_account = {**base, "lines": [dict(item) for item in base["lines"]]}
        foreign_account["lines"][0]["account"] = self.other_accounts["cash"].pk
        invalid_payloads.append(foreign_account)
        foreign_party = {**base, "lines": [dict(item) for item in base["lines"]]}
        foreign_party["lines"][0]["party"] = self.other_party.pk
        invalid_payloads.append(foreign_party)
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.api.post(
                    "/api/v1/journal-entries/", payload, format="json", **self.header
                )
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(JournalEntry.objects.filter(business=self.business).count(), journal_count)

    def test_trade_api_rejects_every_foreign_relationship_without_partial_document(self):
        baseline = TradeDocument.objects.filter(business=self.business).count()
        payload = {
            "document_date": "2026-08-16",
            "party": self.party.pk,
            "period": self.period.pk,
            "debit_account": self.accounts["receivable"].pk,
            "credit_account": self.accounts["sales"].pk,
            "notes": "Cross-tenant attempt",
            "lines": [
                {
                    "product": self.product.pk,
                    "description": "Line",
                    "quantity": "1.000",
                    "unit_price": "10.00",
                }
            ],
        }
        replacements = (
            ("party", self.other_party.pk),
            ("period", self.other_period.pk),
            ("debit_account", self.other_accounts["receivable"].pk),
            ("credit_account", self.other_accounts["sales"].pk),
            ("product", self.other_product.pk),
        )
        for field, foreign_pk in replacements:
            with self.subTest(field=field):
                attempt = {**payload, "lines": [dict(payload["lines"][0])]}
                if field == "product":
                    attempt["lines"][0]["product"] = foreign_pk
                else:
                    attempt[field] = foreign_pk
                response = self.api.post(
                    "/api/v1/sales/", attempt, format="json", **self.header
                )
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(TradeDocument.objects.filter(business=self.business).count(), baseline)

    def test_expense_and_setoff_inputs_reject_foreign_relationships_atomically(self):
        expense_count = ExpenseRecord.objects.filter(business=self.business).count()
        expense_payloads = (
            {
                "expense_date": "2026-08-16",
                "expense_account": self.other_accounts["expense"].pk,
                "payee": self.party.pk,
                "settlement": ExpenseRecord.Settlement.PAYABLE,
                "amount": "5.00",
                "description": "Foreign expense account",
            },
            {
                "expense_date": "2026-08-16",
                "expense_account": self.accounts["expense"].pk,
                "payee": self.other_party.pk,
                "settlement": ExpenseRecord.Settlement.PAYABLE,
                "amount": "5.00",
                "description": "Foreign party",
            },
            {
                "expense_date": "2026-08-16",
                "expense_account": self.accounts["expense"].pk,
                "settlement": ExpenseRecord.Settlement.PAID,
                "payment_account": self.other_accounts["cash"].pk,
                "amount": "5.00",
                "description": "Foreign payment account",
            },
        )
        for payload in expense_payloads:
            with self.subTest(payload=payload):
                response = self.api.post(
                    "/api/v1/expenses/", payload, format="json", **self.header
                )
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ExpenseRecord.objects.filter(business=self.business).count(), expense_count)

        setoff_count = BalanceSetoff.objects.filter(business=self.business).count()
        response = self.api.post(
            "/api/v1/balance-setoffs/",
            {
                "party": self.party.pk,
                "setoff_date": "2026-08-16",
                "sale_allocations": [
                    {"document_id": self.other_sale.pk, "amount": "1.00"}
                ],
                "purchase_allocations": [
                    {"document_id": self.other_purchase.pk, "amount": "1.00"}
                ],
                "idempotency_key": str(uuid.uuid4()),
            },
            format="json",
            **self.header,
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(BalanceSetoff.objects.filter(business=self.business).count(), setoff_count)

    def test_ui_object_routes_return_not_found_for_another_tenant(self):
        get_routes = (
            reverse("product-edit", args=[self.other_product.pk]),
            reverse("period-edit", args=[self.other_period.pk]),
            reverse("journal-detail", args=[self.other_journal.pk]),
            reverse("journal-edit", args=[self.other_journal.pk]),
            reverse("sale-detail", args=[self.other_sale.pk]),
            reverse("sale-edit", args=[self.other_sale.pk]),
            reverse("sale-document-pdf", args=[self.other_sale.pk]),
            reverse("sale-receive-payment", args=[self.other_sale.pk]),
            reverse("purchase-detail", args=[self.other_purchase.pk]),
            reverse("purchase-edit", args=[self.other_purchase.pk]),
            reverse("purchase-document-pdf", args=[self.other_purchase.pk]),
            reverse("purchase-pay-supplier", args=[self.other_purchase.pk]),
            reverse("expense-detail", args=[self.other_expense.pk]),
            reverse("expense-pay", args=[self.other_expense.pk]),
            reverse("expense-pdf", args=[self.other_expense.pk]),
            reverse("balance-setoff-detail", args=[self.other_setoff.pk]),
            reverse("balance-setoff-pdf", args=[self.other_setoff.pk]),
            reverse("money-receipt-document-pdf", args=[self.other_receipt.pk]),
        )
        for route in get_routes:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 404)

        post_routes = (
            (reverse("period-toggle-lock", args=[self.other_period.pk]), {"confirm": "on"}),
            (reverse("journal-post", args=[self.other_journal.pk]), {"confirm": "yes"}),
            (reverse("sale-post", args=[self.other_sale.pk]), {"confirm": "on"}),
            (reverse("sale-delete", args=[self.other_sale.pk]), {"confirm": "on"}),
            (reverse("purchase-post", args=[self.other_purchase.pk]), {"confirm": "on"}),
            (reverse("purchase-delete", args=[self.other_purchase.pk]), {"confirm": "on"}),
        )
        for route, payload in post_routes:
            with self.subTest(route=route):
                self.assertEqual(self.client.post(route, payload).status_code, 404)

    def test_csv_exports_include_selected_tenant_and_exclude_other_tenant(self):
        cases = (
            (reverse("contact-report-csv"), "VISIBLE-PARTY-MARKER", "HIDDEN-PARTY-MARKER"),
            (reverse("invoice-report-csv"), "26000101", "26000901"),
            (
                reverse("money-receipt-report-csv"),
                "VISIBLE-RECEIPT-MARKER",
                "HIDDEN-RECEIPT-MARKER",
            ),
            (
                reverse("transaction-register-csv"),
                "VISIBLE-JOURNAL-MARKER",
                "HIDDEN-JOURNAL-MARKER",
            ),
            (
                reverse("account-activity-report-csv"),
                "VISIBLE-CASH-MARKER",
                "HIDDEN-CASH-MARKER",
            ),
            (reverse("stock-movement-csv"), "VISIBLE-STOCK-MARKER", "HIDDEN-STOCK-MARKER"),
            (reverse("sale-csv"), "26000101", "26000901"),
            (reverse("purchase-csv"), "26000102", "26000902"),
            (reverse("expense-csv"), "VISIBLE-EXPENSE-MARKER", "HIDDEN-EXPENSE-MARKER"),
        )
        filters = {"date_from": "2026-01-01", "date_to": "2026-12-31", "state": "all"}
        for route, visible, hidden in cases:
            with self.subTest(route=route):
                response = self.client.get(route, filters)
                self.assertEqual(response.status_code, 200)
                content = response.content.decode("utf-8-sig")
                self.assertIn(visible, content)
                self.assertNotIn(hidden, content)

    def test_generic_party_supports_sales_and_purchases_and_names_are_tenant_unique(self):
        payload = {
            "document_date": "2026-08-17",
            "party": self.party.pk,
            "period": self.period.pk,
            "debit_account": self.accounts["receivable"].pk,
            "credit_account": self.accounts["sales"].pk,
            "lines": [
                {
                    "product": self.product.pk,
                    "description": "Generic party line",
                    "quantity": "1.000",
                    "unit_price": "10.00",
                }
            ],
        }
        sale = self.api.post("/api/v1/sales/", payload, format="json", **self.header)
        self.assertEqual(sale.status_code, 201, sale.data)
        purchase_payload = {
            **payload,
            "debit_account": self.accounts["inventory"].pk,
            "credit_account": self.accounts["payable"].pk,
        }
        purchase = self.api.post(
            "/api/v1/purchases/", purchase_payload, format="json", **self.header
        )
        self.assertEqual(purchase.status_code, 201, purchase.data)
        self.assertEqual(sale.data["party"], purchase.data["party"])

        duplicate = self.api.post(
            "/api/v1/parties/",
            {
                "name": "  visible-party-marker  ",
                "kind": Party.Kind.SUPPLIER,
                "opening_balance": "0.00",
            },
            format="json",
            **self.header,
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(
            Party.objects.filter(business=self.business, name__iexact="VISIBLE-PARTY-MARKER").count(),
            1,
        )
        self.assertTrue(
            Party.objects.filter(business=self.other, name="HIDDEN-PARTY-MARKER").exists()
        )
