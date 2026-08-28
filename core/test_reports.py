from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from accounting.models import Account, FiscalPeriod, JournalEntry, MoneyReceipt, Voucher
from core.application.services import CONTACTS_VIEW
from core.models import Business, Membership, Party, Role
from operations.models import TradeDocument


class BusinessReportTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            username="report-owner",
            password="password",
        )
        self.viewer = user_model.objects.create_user(
            username="contact-viewer",
            password="password",
        )
        self.business = Business.objects.create(
            name="Report House",
            slug="report-house",
        )
        self.other = Business.objects.create(
            name="Hidden Report House",
            slug="hidden-report-house",
        )
        Membership.objects.create(
            user=self.owner,
            business=self.business,
            level=Membership.Level.BUSINESS_ADMIN,
        )
        contact_role = Role.objects.create(
            business=self.business,
            name="Contact reporter",
            permissions=[CONTACTS_VIEW],
        )
        Membership.objects.create(
            user=self.viewer,
            business=self.business,
            role=contact_role,
        )
        self.customer = Party.objects.create(
            business=self.business,
            name="Amina Customer",
            kind=Party.Kind.CUSTOMER,
            phone="01700000000",
            email="amina@example.com",
            address="Dhaka",
            opening_balance=Decimal("125.00"),
        )
        self.supplier = Party.objects.create(
            business=self.business,
            name="Bengal Supplier",
            kind=Party.Kind.SUPPLIER,
        )
        self.both = Party.objects.create(
            business=self.business,
            name="Combined Trading",
            kind=Party.Kind.BOTH,
        )
        hidden_party = Party.objects.create(
            business=self.other,
            name="Hidden Contact",
            kind=Party.Kind.CUSTOMER,
        )
        self.period = FiscalPeriod.objects.create(
            business=self.business,
            name="FY26",
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        other_period = FiscalPeriod.objects.create(
            business=self.other,
            name="FY26",
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        self.cash = Account.objects.create(
            business=self.business,
            code="1010",
            name="Cash",
            account_type=Account.Type.ASSET,
            system_role=Account.SystemRole.CASH,
        )
        self.sales = Account.objects.create(
            business=self.business,
            code="4010",
            name="Sales",
            account_type=Account.Type.INCOME,
        )
        self.invoice = TradeDocument.objects.create(
            business=self.business,
            kind=TradeDocument.Kind.SALE,
            number="26000001",
            party=self.customer,
            period=self.period,
            debit_account=self.cash,
            credit_account=self.sales,
            document_date=date(2026, 8, 20),
            subtotal=Decimal("500.00"),
            discount_type=TradeDocument.DiscountType.FIXED,
            discount_value=Decimal("50.00"),
            discount_amount=Decimal("50.00"),
            total=Decimal("450.00"),
            status=TradeDocument.Status.POSTED,
            created_by=self.owner,
        )
        TradeDocument.objects.create(
            business=self.business,
            kind=TradeDocument.Kind.SALE,
            number="26000002",
            party=self.customer,
            period=self.period,
            debit_account=self.cash,
            credit_account=self.sales,
            document_date=date(2026, 8, 21),
            subtotal=Decimal("300.00"),
            total=Decimal("300.00"),
            status=TradeDocument.Status.DRAFT,
            created_by=self.owner,
        )
        other_cash = Account.objects.create(
            business=self.other,
            code="1010",
            name="Cash",
            account_type=Account.Type.ASSET,
            system_role=Account.SystemRole.CASH,
        )
        other_sales = Account.objects.create(
            business=self.other,
            code="4010",
            name="Sales",
            account_type=Account.Type.INCOME,
        )
        TradeDocument.objects.create(
            business=self.other,
            kind=TradeDocument.Kind.SALE,
            number="26999999",
            party=hidden_party,
            period=other_period,
            debit_account=other_cash,
            credit_account=other_sales,
            document_date=date(2026, 8, 20),
            subtotal=Decimal("999.00"),
            total=Decimal("999.00"),
            status=TradeDocument.Status.POSTED,
        )
        receipt_journal = JournalEntry.objects.create(
            business=self.business,
            period=self.period,
            reference="RECEIPT:MR-1",
            description="Customer receipt",
            entry_date=date(2026, 8, 22),
            posted=True,
            created_by=self.owner,
        )
        self.receipt = Voucher.objects.create(
            business=self.business,
            voucher_type=Voucher.Type.RECEIPT,
            number="MR-1",
            party=self.customer,
            journal_entry=receipt_journal,
            total=Decimal("200.00"),
            notes="Cash received",
            voucher_date=date(2026, 8, 22),
        )
        self.money_receipt = MoneyReceipt.objects.create(
            business=self.business,
            number="MR-1",
            voucher=self.receipt,
            party=self.customer,
            payment_account=self.cash,
            amount=Decimal("200.00"),
            receipt_date=date(2026, 8, 22),
        )
        sale_journal = JournalEntry.objects.create(
            business=self.business,
            period=self.period,
            reference="SALE:NOT-RECEIPT",
            description="Sale voucher",
            entry_date=date(2026, 8, 22),
            posted=True,
            created_by=self.owner,
        )
        Voucher.objects.create(
            business=self.business,
            voucher_type=Voucher.Type.SALE,
            number="S-NOT-RECEIPT",
            party=self.customer,
            journal_entry=sale_journal,
            total=Decimal("500.00"),
            voucher_date=date(2026, 8, 22),
        )
        hidden_journal = JournalEntry.objects.create(
            business=self.other,
            period=other_period,
            reference="RECEIPT:HIDDEN",
            description="Hidden receipt",
            entry_date=date(2026, 8, 22),
            posted=True,
        )
        hidden_voucher = Voucher.objects.create(
            business=self.other,
            voucher_type=Voucher.Type.RECEIPT,
            number="MR-HIDDEN",
            party=hidden_party,
            journal_entry=hidden_journal,
            total=Decimal("999.00"),
            voucher_date=date(2026, 8, 22),
        )
        MoneyReceipt.objects.create(
            business=self.other,
            number="MR-HIDDEN",
            voucher=hidden_voucher,
            party=hidden_party,
            payment_account=other_cash,
            amount=Decimal("999.00"),
            receipt_date=date(2026, 8, 22),
        )
        self.client.login(username="report-owner", password="password")

    def test_contact_report_filters_types_and_is_tenant_scoped(self):
        response = self.client.get(reverse("contact-report"), {"kind": "customer"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="report-summary"')
        self.assertContains(response, 'class="report-filter-bar"')
        self.assertContains(response, 'class="report-table"')
        self.assertContains(response, "Amina Customer")
        self.assertContains(response, "Combined Trading")
        self.assertNotContains(response, "Bengal Supplier")
        self.assertNotContains(response, "Hidden Contact")

        csv_response = self.client.get(
            reverse("contact-report-csv"),
            {"kind": "supplier"},
        )
        self.assertContains(csv_response, "Bengal Supplier")
        self.assertContains(csv_response, "Combined Trading")
        self.assertNotContains(csv_response, "Amina Customer")
        self.assertNotContains(csv_response, "Hidden Contact")
        pdf_response = self.client.get(reverse("contact-report-pdf"))
        self.assertTrue(pdf_response.content.startswith(b"%PDF"))

    def test_invoice_report_contains_only_posted_tenant_sales(self):
        response = self.client.get(reverse("invoice-report"))
        self.assertContains(response, "Net invoice value")
        self.assertContains(response, "Reporting period")
        self.assertContains(response, 'class="report-row-action"')
        self.assertContains(response, "26000001")
        self.assertContains(response, "450.00")
        self.assertNotContains(response, "26000002")
        self.assertNotContains(response, "26999999")
        csv_response = self.client.get(reverse("invoice-report-csv"))
        self.assertContains(csv_response, "26000001")
        self.assertContains(csv_response, "Discount (BDT)")
        self.assertContains(csv_response, "450.00")
        self.assertNotContains(csv_response, "26000002")
        pdf_response = self.client.get(reverse("invoice-report-pdf"))
        self.assertTrue(pdf_response.content.startswith(b"%PDF"))
        empty_pdf = self.client.get(
            reverse("invoice-report-pdf"),
            {"date_from": "2099-01-01"},
        )
        self.assertTrue(empty_pdf.content.startswith(b"%PDF"))

    def test_money_receipt_report_excludes_other_voucher_types_and_tenants(self):
        response = self.client.get(reverse("money-receipt-report"))
        self.assertContains(response, "Amount received")
        self.assertContains(response, "Immutable evidence")
        self.assertContains(response, 'class="report-source"')
        self.assertContains(response, "MR-1")
        self.assertNotContains(response, "S-NOT-RECEIPT")
        self.assertNotContains(response, "MR-HIDDEN")
        csv_response = self.client.get(reverse("money-receipt-report-csv"))
        self.assertContains(csv_response, "MR-1")
        self.assertNotContains(csv_response, "S-NOT-RECEIPT")
        pdf_response = self.client.get(reverse("money-receipt-report-pdf"))
        self.assertTrue(pdf_response.content.startswith(b"%PDF"))
        document = self.client.get(
            reverse("money-receipt-document-pdf", args=[self.money_receipt.pk])
        )
        self.assertTrue(document.content.startswith(b"%PDF"))

    def test_report_permissions_are_enforced_per_domain(self):
        self.client.logout()
        self.client.login(username="contact-viewer", password="password")
        index = self.client.get(reverse("report-index"))
        self.assertEqual(index.status_code, 200)
        self.assertContains(index, 'class="panel data-panel report-library"')
        self.assertContains(index, "Contact directory")
        self.assertNotContains(index, "Invoice register")
        self.assertEqual(self.client.get(reverse("contact-report")).status_code, 200)
        self.assertEqual(self.client.get(reverse("invoice-report")).status_code, 403)
        self.assertEqual(self.client.get(reverse("money-receipt-report")).status_code, 403)
