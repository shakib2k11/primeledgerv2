import uuid
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounting.models import Account, FiscalPeriod, JournalEntry, MoneyReceipt, Voucher
from core.application.services import SALES_VIEW
from core.infrastructure.numbering import allocate_reference_number
from core.models import Business, InventoryUnit, Membership, Party, Product, Role, StockMovement
from operations.infrastructure.repositories import (
    DjangoBalanceSetoffRepository,
    DjangoPurchasePaymentRepository,
    DjangoSalePaymentRepository,
    DjangoTradeDocumentRepository,
)
from operations.application.services import SetoffAllocationCommand
from operations.models import (
    BalanceSetoff,
    PurchasePayment,
    PurchaseSetoffAllocation,
    SalePayment,
    SaleSetoffAllocation,
    TradeDocument,
    TradeLine,
)


class TradeWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="operator", password="pw")
        self.business = Business.objects.create(name="Trade House", slug="trade-house")
        Membership.objects.create(
            user=self.user,
            business=self.business,
            level=Membership.Level.BUSINESS_ADMIN,
        )
        self.customer = Party.objects.create(
            business=self.business, name="Customer One", kind=Party.Kind.CUSTOMER
        )
        self.supplier = Party.objects.create(
            business=self.business, name="Supplier One", kind=Party.Kind.SUPPLIER
        )
        self.both_party = Party.objects.create(
            business=self.business,
            name="Mutual Trading Partner",
            kind=Party.Kind.BOTH,
        )
        self.pack = InventoryUnit.objects.get(business__isnull=True, code="pack")
        self.piece = InventoryUnit.objects.get(business__isnull=True, code="piece")
        self.product = Product.objects.create(
            business=self.business,
            name="Tea",
            sku="TEA-1",
            unit=self.pack,
            sale_price=Decimal("120.00"),
            purchase_price=Decimal("80.00"),
        )
        self.service = Product.objects.create(
            business=self.business,
            name="Delivery",
            sku="DELIVERY",
            unit=self.piece,
            is_service=True,
            sale_price=Decimal("50.00"),
        )
        self.period = FiscalPeriod.objects.create(
            business=self.business,
            name="FY26",
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        self.receivable = Account.objects.create(
            business=self.business, code="1100", name="Receivable", account_type=Account.Type.ASSET,
            system_role=Account.SystemRole.ACCOUNTS_RECEIVABLE,
        )
        self.cash = Account.objects.create(
            business=self.business, code="1010", name="Cash in Hand",
            account_type=Account.Type.ASSET,
            system_role=Account.SystemRole.CASH,
        )
        self.payable = Account.objects.create(
            business=self.business, code="2100", name="Payable", account_type=Account.Type.LIABILITY,
            system_role=Account.SystemRole.ACCOUNTS_PAYABLE,
        )
        self.inventory = Account.objects.create(
            business=self.business, code="1200", name="Inventory", account_type=Account.Type.ASSET,
            system_role=Account.SystemRole.INVENTORY,
        )
        self.revenue = Account.objects.create(
            business=self.business, code="4100", name="Sales", account_type=Account.Type.INCOME,
            system_role=Account.SystemRole.SALES_REVENUE,
        )
        self.cogs = Account.objects.create(
            business=self.business, code="5010", name="Cost of Goods Sold",
            account_type=Account.Type.EXPENSE,
            system_role=Account.SystemRole.COST_OF_GOODS_SOLD,
        )
        StockMovement.objects.create(
            business=self.business,
            number=allocate_reference_number(
                business_id=self.business.pk,
                occurred_on=timezone.now(),
            ),
            product=self.product,
            direction=StockMovement.Direction.IN,
            quantity=Decimal("20.000"),
            unit_cost=Decimal("80.00"),
            reference="OPENING",
            occurred_at=timezone.now(),
            created_by=self.user,
        )
        self.client.login(username="operator", password="pw")

    def make_document(
        self,
        kind=TradeDocument.Kind.SALE,
        quantity="2.000",
        discount_type=TradeDocument.DiscountType.NONE,
        discount_value="0.00",
        party=None,
    ):
        document_date = date(2026, 8, 26)
        document = TradeDocument.objects.create(
            business=self.business,
            kind=kind,
            number=allocate_reference_number(
                business_id=self.business.pk,
                occurred_on=document_date,
            ),
            party=(party or (self.customer if kind == TradeDocument.Kind.SALE else self.supplier)),
            period=self.period,
            debit_account=(self.receivable if kind == TradeDocument.Kind.SALE else self.inventory),
            credit_account=(self.revenue if kind == TradeDocument.Kind.SALE else self.payable),
            document_date=document_date,
            discount_type=discount_type,
            discount_value=Decimal(discount_value),
            created_by=self.user,
        )
        TradeLine.objects.create(
            document=document,
            product=self.product,
            quantity=Decimal(quantity),
            unit_price=(Decimal("120.00") if kind == TradeDocument.Kind.SALE else Decimal("80.00")),
        )
        document.recalculate_total()
        document.save(update_fields=["subtotal", "discount_amount", "total"])
        return document

    def test_sale_posts_journal_voucher_and_stock_idempotently(self):
        document = self.make_document()
        original_number = document.number
        document.number = "26999999"
        with self.assertRaises(ValidationError):
            document.save()
        document.number = original_number
        repository = DjangoTradeDocumentRepository()
        repository.post(document_id=document.pk, business_id=self.business.pk)
        repository.post(document_id=document.pk, business_id=self.business.pk)
        document.refresh_from_db()
        self.assertEqual(document.status, TradeDocument.Status.POSTED)
        self.assertEqual(document.total, Decimal("240.00"))
        self.assertTrue(document.journal_entry.posted)
        self.assertEqual(document.journal_entry.total_debit, Decimal("400.00"))
        self.assertTrue(
            document.journal_entry.lines.filter(
                account=self.cogs,
                debit=Decimal("160.00"),
            ).exists()
        )
        self.assertTrue(
            document.journal_entry.lines.filter(
                account=self.inventory,
                credit=Decimal("160.00"),
            ).exists()
        )
        self.assertEqual(Voucher.objects.filter(business=self.business).count(), 1)
        self.assertFalse(MoneyReceipt.objects.filter(business=self.business).exists())
        movement = StockMovement.objects.get(
            reference=document.number,
            direction=StockMovement.Direction.OUT,
        )
        self.assertNotEqual(movement.number, document.number)
        self.assertEqual(movement.number, "26000003")
        self.assertEqual(movement.unit_cost, Decimal("80.00"))

    def test_insufficient_sale_rolls_back_every_side_effect(self):
        document = self.make_document(quantity="25.000")
        with self.assertRaises(ValidationError):
            DjangoTradeDocumentRepository().post(
                document_id=document.pk, business_id=self.business.pk
            )
        document.refresh_from_db()
        self.assertEqual(document.status, TradeDocument.Status.DRAFT)
        self.assertFalse(JournalEntry.objects.filter(reference=f"SALE:{document.number}").exists())
        self.assertFalse(Voucher.objects.filter(business=self.business).exists())

    def test_missing_cogs_role_rolls_back_every_side_effect(self):
        self.cogs.is_active = False
        self.cogs.save(update_fields=["is_active"])
        document = self.make_document()
        with self.assertRaisesMessage(ValidationError, "Cost of Goods Sold posting roles"):
            DjangoTradeDocumentRepository().post(
                document_id=document.pk,
                business_id=self.business.pk,
            )
        document.refresh_from_db()
        self.assertEqual(document.status, TradeDocument.Status.DRAFT)
        self.assertFalse(JournalEntry.objects.filter(reference=f"SALE:{document.number}").exists())
        self.assertFalse(StockMovement.objects.filter(reference=document.number).exists())
        self.assertFalse(Voucher.objects.filter(business=self.business).exists())

    def test_purchase_post_creates_stock_inflow(self):
        document = self.make_document(TradeDocument.Kind.PURCHASE, "3.000")
        DjangoTradeDocumentRepository().post(
            document_id=document.pk, business_id=self.business.pk
        )
        self.assertTrue(
            StockMovement.objects.filter(
                reference=document.number,
                direction=StockMovement.Direction.IN,
                quantity=Decimal("3.000"),
            ).exists()
        )

    def test_business_admin_deletes_draft_purchase_after_confirmation(self):
        document = self.make_document(TradeDocument.Kind.PURCHASE, "3.000")
        response = self.client.get(reverse("purchase-delete", args=[document.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Delete {document.number}?")
        self.assertContains(response, "automatic number will not be reused")

        response = self.client.post(
            reverse("purchase-delete", args=[document.pk]),
            {"confirm": "yes"},
        )
        self.assertRedirects(response, reverse("purchase-list"))
        self.assertFalse(TradeDocument.objects.filter(pk=document.pk).exists())
        self.assertFalse(TradeLine.objects.filter(document_id=document.pk).exists())

        api_document = self.make_document(TradeDocument.Kind.PURCHASE, "1.000")
        api = APIClient()
        api.force_authenticate(self.user)
        response = api.delete(
            f"/api/v1/purchases/{api_document.pk}/",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(TradeDocument.objects.filter(pk=api_document.pk).exists())

    def test_posted_or_locked_purchase_cannot_be_deleted(self):
        posted = self.make_document(TradeDocument.Kind.PURCHASE, "3.000")
        DjangoTradeDocumentRepository().post(
            document_id=posted.pk,
            business_id=self.business.pk,
        )
        response = self.client.post(
            reverse("purchase-delete", args=[posted.pk]),
            {"confirm": "yes"},
        )
        self.assertRedirects(response, reverse("purchase-detail", args=[posted.pk]))
        self.assertTrue(TradeDocument.objects.filter(pk=posted.pk).exists())

        locked = self.make_document(TradeDocument.Kind.PURCHASE, "1.000")
        self.period.is_locked = True
        self.period.save(update_fields=["is_locked"])
        api = APIClient()
        api.force_authenticate(self.user)
        response = api.delete(
            f"/api/v1/purchases/{locked.pk}/",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(TradeDocument.objects.filter(pk=locked.pk).exists())

    def test_sale_uses_moving_weighted_average_cost(self):
        StockMovement.objects.create(
            business=self.business,
            number=allocate_reference_number(
                business_id=self.business.pk,
                occurred_on=date(2026, 8, 20),
            ),
            product=self.product,
            direction=StockMovement.Direction.IN,
            quantity=Decimal("10.000"),
            unit_cost=Decimal("100.00"),
            reference="SECOND-BATCH",
            occurred_at=timezone.now(),
            created_by=self.user,
        )
        document = self.make_document(quantity="3.000")
        DjangoTradeDocumentRepository().post(
            document_id=document.pk,
            business_id=self.business.pk,
        )
        document.refresh_from_db()
        movement = StockMovement.objects.get(
            reference=document.number,
            direction=StockMovement.Direction.OUT,
        )
        self.assertEqual(movement.unit_cost, Decimal("86.67"))
        self.assertTrue(
            document.journal_entry.lines.filter(
                account=self.cogs,
                debit=Decimal("260.01"),
            ).exists()
        )

    def test_percentage_discount_reduces_revenue_but_not_cogs(self):
        document = self.make_document(
            discount_type=TradeDocument.DiscountType.PERCENTAGE,
            discount_value="10.00",
        )
        self.assertEqual(document.subtotal, Decimal("240.00"))
        self.assertEqual(document.discount_amount, Decimal("24.00"))
        self.assertEqual(document.total, Decimal("216.00"))

        DjangoTradeDocumentRepository().post(
            document_id=document.pk,
            business_id=self.business.pk,
        )
        document.refresh_from_db()
        self.assertEqual(document.journal_entry.total_debit, Decimal("376.00"))
        self.assertTrue(
            document.journal_entry.lines.filter(
                account=self.receivable,
                debit=Decimal("216.00"),
            ).exists()
        )
        self.assertTrue(
            document.journal_entry.lines.filter(
                account=self.revenue,
                credit=Decimal("216.00"),
            ).exists()
        )
        self.assertTrue(
            document.journal_entry.lines.filter(
                account=self.cogs,
                debit=Decimal("160.00"),
            ).exists()
        )
        self.assertEqual(document.journal_entry.voucher.total, Decimal("216.00"))

    def test_immediate_paid_sale_generates_one_immutable_money_receipt(self):
        document = self.make_document(
            discount_type=TradeDocument.DiscountType.FIXED,
            discount_value="20.00",
        )
        document.debit_account = self.cash
        document.save(update_fields=["debit_account"])
        repository = DjangoTradeDocumentRepository()

        repository.post(document_id=document.pk, business_id=self.business.pk)
        repository.post(document_id=document.pk, business_id=self.business.pk)

        document.refresh_from_db()
        receipt = MoneyReceipt.objects.get(
            business=self.business,
            voucher__journal_entry=document.journal_entry,
        )
        self.assertEqual(receipt.number, f"MR-{document.number}")
        self.assertEqual(receipt.amount, Decimal("220.00"))
        self.assertEqual(receipt.party, self.customer)
        self.assertEqual(receipt.payment_account, self.cash)
        self.assertEqual(receipt.voucher.voucher_type, Voucher.Type.SALE)
        self.assertEqual(MoneyReceipt.objects.filter(voucher=receipt.voucher).count(), 1)
        receipt_number = receipt.number
        receipt.number = "CHANGED"
        with self.assertRaises(ValidationError):
            receipt.save()

        detail = self.client.get(reverse("sale-detail", args=[document.pk]))
        self.assertContains(detail, "Download money receipt")
        pdf = self.client.get(reverse("money-receipt-document-pdf", args=[receipt.pk]))
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF"))

        api = APIClient()
        api.force_authenticate(self.user)
        response = api.get(
            f"/api/v1/sales/{document.pk}/",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["money_receipt_number"], receipt_number)

    def test_credit_sale_receives_partial_and_final_payment_with_receipts(self):
        document = self.make_document()
        DjangoTradeDocumentRepository().post(
            document_id=document.pk,
            business_id=self.business.pk,
        )

        form_page = self.client.get(reverse("sale-receive-payment", args=[document.pk]))
        self.assertEqual(form_page.status_code, 200)
        self.assertContains(form_page, "Receive customer payment")
        self.assertContains(form_page, "Outstanding balance: 240.00 BDT")

        response = self.client.post(
            reverse("sale-receive-payment", args=[document.pk]),
            {
                "payment_date": "2026-08-27",
                "payment_account": self.cash.pk,
                "amount": "100.00",
                "notes": "Cash counter",
                "idempotency_key": str(uuid.uuid4()),
                "confirm": "on",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        payment = SalePayment.objects.get(sale=document)
        self.assertEqual(payment.amount, Decimal("100.00"))
        self.assertEqual(payment.payment_account, self.cash)
        self.assertEqual(payment.money_receipt.amount, Decimal("100.00"))
        self.assertEqual(payment.journal_entry.voucher.voucher_type, Voucher.Type.RECEIPT)
        self.assertTrue(payment.journal_entry.posted)
        self.assertTrue(payment.journal_entry.lines.filter(
            account=self.cash,
            party=self.customer,
            debit=Decimal("100.00"),
        ).exists())
        self.assertTrue(payment.journal_entry.lines.filter(
            account=self.receivable,
            party=self.customer,
            credit=Decimal("100.00"),
        ).exists())
        payment.notes = "Changed"
        with self.assertRaises(ValidationError):
            payment.save()
        self.assertContains(response, "Partially paid")
        self.assertContains(response, "140.00")
        self.assertContains(response, "Receipt PDF")
        invoice_report = self.client.get(reverse("invoice-report"))
        self.assertContains(invoice_report, "Partially paid")
        self.assertContains(invoice_report, "140.00")
        receipt_report = self.client.get(reverse("money-receipt-report"))
        self.assertContains(receipt_report, f"Invoice {document.number}")

        api = APIClient()
        api.force_authenticate(self.user)
        other_business = Business.objects.create(
            name="Foreign Payment House",
            slug="foreign-payment-house",
        )
        foreign_cash = Account.objects.create(
            business=other_business,
            code="1010",
            name="Foreign cash",
            account_type=Account.Type.ASSET,
            system_role=Account.SystemRole.CASH,
        )
        rejected = api.post(
            f"/api/v1/sales/{document.pk}/receive-payment/",
            {
                "payment_date": "2026-08-28",
                "payment_account": foreign_cash.pk,
                "amount": "10.00",
            },
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(SalePayment.objects.filter(sale=document).count(), 1)
        api_response = api.post(
            f"/api/v1/sales/{document.pk}/receive-payment/",
            {
                "payment_date": "2026-08-28",
                "payment_account": self.cash.pk,
                "amount": "140.00",
                "notes": "Final settlement",
                "idempotency_key": str(uuid.uuid4()),
            },
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(api_response.status_code, 201, api_response.data)
        self.assertEqual(api_response.data["amount"], "140.00")
        self.assertTrue(api_response.data["money_receipt_number"].startswith("MR-"))

        detail = api.get(
            f"/api/v1/sales/{document.pk}/",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(detail.data["payment_status"], TradeDocument.PaymentStatus.PAID)
        self.assertEqual(detail.data["paid_amount"], "240.00")
        self.assertEqual(detail.data["balance_due"], "0.00")
        self.assertEqual(len(detail.data["payments"]), 2)
        self.assertRedirects(
            self.client.get(reverse("sale-receive-payment", args=[document.pk])),
            reverse("sale-detail", args=[document.pk]),
        )
        invoice_pdf = self.client.get(reverse("sale-document-pdf", args=[document.pk]))
        self.assertTrue(invoice_pdf.content.startswith(b"%PDF"))

    def test_payment_is_idempotent_and_overpayment_or_locked_period_rolls_back(self):
        document = self.make_document()
        DjangoTradeDocumentRepository().post(
            document_id=document.pk,
            business_id=self.business.pk,
        )
        repository = DjangoSalePaymentRepository()
        key = uuid.uuid4()
        command = {
            "sale_id": document.pk,
            "business_id": self.business.pk,
            "payment_account_id": self.cash.pk,
            "amount": Decimal("100.00"),
            "payment_date": date(2026, 8, 27),
            "idempotency_key": key,
            "notes": "Deposit",
            "user_id": self.user.pk,
        }
        first = repository.receive(**command)
        second = repository.receive(**command)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SalePayment.objects.filter(sale=document).count(), 1)

        side_effect_counts = (
            JournalEntry.objects.count(),
            Voucher.objects.count(),
            MoneyReceipt.objects.count(),
            SalePayment.objects.count(),
        )
        with self.assertRaisesMessage(ValidationError, "cannot exceed"):
            repository.receive(
                **{
                    **command,
                    "amount": Decimal("141.00"),
                    "idempotency_key": uuid.uuid4(),
                }
            )
        self.assertEqual(side_effect_counts, (
            JournalEntry.objects.count(),
            Voucher.objects.count(),
            MoneyReceipt.objects.count(),
            SalePayment.objects.count(),
        ))

        self.period.is_locked = True
        self.period.save(update_fields=["is_locked"])
        with self.assertRaisesMessage(ValidationError, "locked"):
            repository.receive(
                **{
                    **command,
                    "amount": Decimal("10.00"),
                    "idempotency_key": uuid.uuid4(),
                }
            )
        self.assertEqual(SalePayment.objects.filter(sale=document).count(), 1)

    def test_purchase_payable_supports_partial_and_final_supplier_payments(self):
        purchase = self.make_document(kind=TradeDocument.Kind.PURCHASE)
        DjangoTradeDocumentRepository().post(
            document_id=purchase.pk,
            business_id=self.business.pk,
        )
        purchase.refresh_from_db()

        center = self.client.get(reverse("payment-center"))
        self.assertContains(center, "Open supplier payables")
        self.assertContains(center, purchase.number)
        self.assertContains(center, "Pay")
        detail = self.client.get(reverse("purchase-detail", args=[purchase.pk]))
        self.assertContains(detail, "Pay supplier")
        form_page = self.client.get(reverse("purchase-pay-supplier", args=[purchase.pk]))
        self.assertContains(form_page, "Record funds paid")

        response = self.client.post(
            reverse("purchase-pay-supplier", args=[purchase.pk]),
            {
                "payment_date": "2026-08-27",
                "payment_account": self.cash.pk,
                "amount": "60.00",
                "notes": "Cheque 1001",
                "idempotency_key": uuid.uuid4(),
                "confirm": "on",
            },
        )
        self.assertRedirects(response, reverse("purchase-detail", args=[purchase.pk]))
        payment = PurchasePayment.objects.get(purchase=purchase)
        self.assertEqual(payment.amount, Decimal("60.00"))
        self.assertEqual(payment.voucher.voucher_type, Voucher.Type.PAYMENT)
        self.assertEqual(payment.voucher.party, self.supplier)
        self.assertTrue(payment.journal_entry.posted)
        self.assertTrue(payment.journal_entry.lines.filter(
            account=self.payable,
            debit=Decimal("60.00"),
            credit=Decimal("0.00"),
        ).exists())
        self.assertTrue(payment.journal_entry.lines.filter(
            account=self.cash,
            debit=Decimal("0.00"),
            credit=Decimal("60.00"),
        ).exists())
        voucher_pdf = self.client.get(
            reverse("purchase-payment-document-pdf", args=[payment.pk])
        )
        self.assertEqual(voucher_pdf.status_code, 200)
        self.assertTrue(voucher_pdf.content.startswith(b"%PDF"))
        payment.notes = "Changed"
        with self.assertRaises(ValidationError):
            payment.save()

        purchase.refresh_from_db()
        self.assertEqual(purchase.paid_amount, Decimal("60.00"))
        self.assertEqual(purchase.balance_due, Decimal("100.00"))
        self.assertEqual(purchase.payment_status, TradeDocument.PaymentStatus.PARTIAL)
        self.assertContains(
            self.client.get(reverse("purchase-detail", args=[purchase.pk])),
            "Partially paid",
        )

        foreign = Business.objects.create(
            name="Foreign Supplier House",
            slug="foreign-supplier-house",
        )
        foreign_cash = Account.objects.create(
            business=foreign,
            code="1010",
            name="Foreign Cash",
            account_type=Account.Type.ASSET,
            system_role=Account.SystemRole.CASH,
        )
        rejected = self.client.post(
            f"/api/v1/purchases/{purchase.pk}/pay-supplier/",
            {
                "payment_date": "2026-08-28",
                "payment_account": foreign_cash.pk,
                "amount": "100.00",
            },
            content_type="application/json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(rejected.status_code, 400)

        paid = self.client.post(
            f"/api/v1/purchases/{purchase.pk}/pay-supplier/",
            {
                "payment_date": "2026-08-28",
                "payment_account": self.cash.pk,
                "amount": "100.00",
            },
            content_type="application/json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(paid.status_code, 201, paid.data)
        self.assertTrue(paid.data["voucher_number"].startswith("P-"))
        purchase.refresh_from_db()
        self.assertEqual(purchase.payment_status, TradeDocument.PaymentStatus.PAID)
        api_detail = self.client.get(
            f"/api/v1/purchases/{purchase.pk}/",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(api_detail.data["paid_amount"], "160.00")
        self.assertEqual(api_detail.data["balance_due"], "0.00")
        self.assertEqual(len(api_detail.data["supplier_payments"]), 2)
        self.assertRedirects(
            self.client.get(reverse("purchase-pay-supplier", args=[purchase.pk])),
            reverse("purchase-detail", args=[purchase.pk]),
        )

    def test_supplier_payment_is_idempotent_and_rolls_back_invalid_attempts(self):
        purchase = self.make_document(kind=TradeDocument.Kind.PURCHASE)
        DjangoTradeDocumentRepository().post(
            document_id=purchase.pk,
            business_id=self.business.pk,
        )
        key = uuid.uuid4()
        repository = DjangoPurchasePaymentRepository()
        values = {
            "purchase_id": purchase.pk,
            "business_id": self.business.pk,
            "payment_account_id": self.cash.pk,
            "amount": Decimal("40.00"),
            "payment_date": date(2026, 8, 27),
            "idempotency_key": key,
            "user_id": self.user.pk,
        }
        first = repository.pay(**values)
        repeated = repository.pay(**values)
        self.assertEqual(first.pk, repeated.pk)
        self.assertEqual(PurchasePayment.objects.filter(purchase=purchase).count(), 1)

        counts = (
            PurchasePayment.objects.count(),
            JournalEntry.objects.count(),
            Voucher.objects.count(),
        )
        with self.assertRaisesMessage(ValidationError, "cannot exceed"):
            repository.pay(**{
                **values,
                "amount": Decimal("121.00"),
                "idempotency_key": uuid.uuid4(),
            })
        self.assertEqual(
            counts,
            (
                PurchasePayment.objects.count(),
                JournalEntry.objects.count(),
                Voucher.objects.count(),
            ),
        )

        self.period.is_locked = True
        self.period.save(update_fields=["is_locked"])
        with self.assertRaisesMessage(ValidationError, "locked"):
            repository.pay(**{
                **values,
                "amount": Decimal("10.00"),
                "idempotency_key": uuid.uuid4(),
            })
        self.assertEqual(PurchasePayment.objects.filter(purchase=purchase).count(), 1)

    def test_ui_create_post_and_exports(self):
        response = self.client.post(
            reverse("sale-create"),
            {
                "number": "99999999",
                "document_date": "2026-08-26",
                "party": self.customer.pk,
                "period": self.period.pk,
                "debit_account": self.receivable.pk,
                "credit_account": self.revenue.pk,
                "discount_type": TradeDocument.DiscountType.FIXED,
                "discount_value": "20.00",
                "notes": "Counter sale",
                "lines-TOTAL_FORMS": "3",
                "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "1",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-product": self.product.pk,
                "lines-0-description": "Tea packs",
                "lines-0-quantity": "2.000",
                "lines-0-unit_price": "120.00",
                "lines-1-product": "",
                "lines-1-description": "",
                "lines-1-quantity": "",
                "lines-1-unit_price": "",
                "lines-2-product": "",
                "lines-2-description": "",
                "lines-2-quantity": "",
                "lines-2-unit_price": "",
            },
        )
        document = TradeDocument.objects.get(kind=TradeDocument.Kind.SALE)
        self.assertEqual(document.number, "26000002")
        self.assertNotEqual(document.number, "99999999")
        self.assertEqual(document.subtotal, Decimal("240.00"))
        self.assertEqual(document.discount_amount, Decimal("20.00"))
        self.assertEqual(document.total, Decimal("220.00"))
        self.assertRedirects(response, reverse("sale-detail", args=[document.pk]))
        response = self.client.post(reverse("sale-post", args=[document.pk]), {"confirm": "yes"})
        self.assertRedirects(response, reverse("sale-detail", args=[document.pk]))
        sale_csv = self.client.get(reverse("sale-csv"))
        self.assertEqual(sale_csv.status_code, 200)
        self.assertContains(sale_csv, document.number)
        pdf = self.client.get(reverse("sale-pdf"))
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        document_pdf = self.client.get(reverse("sale-document-pdf", args=[document.pk]))
        self.assertEqual(document_pdf.status_code, 200)
        self.assertTrue(document_pdf.content.startswith(b"%PDF"))
        stock_csv = self.client.get(reverse("stock-movement-csv"))
        self.assertContains(stock_csv, document.number)
        stock_pdf = self.client.get(reverse("stock-movement-pdf"))
        self.assertEqual(stock_pdf.status_code, 200)
        self.assertTrue(stock_pdf.content.startswith(b"%PDF"))

    def test_sale_and_purchase_line_grids_show_calculated_amount_column(self):
        for route in ("sale-create", "purchase-create"):
            response = self.client.get(reverse(route))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "<span class=\"numeric\">Amount</span>", html=True)
            self.assertContains(response, "data-line-amount")
            self.assertContains(response, "Calculated amount")

    def test_ui_rejects_discount_that_consumes_the_sale(self):
        response = self.client.post(
            reverse("sale-create"),
            {
                "document_date": "2026-08-26",
                "party": self.customer.pk,
                "period": self.period.pk,
                "debit_account": self.receivable.pk,
                "credit_account": self.revenue.pk,
                "discount_type": TradeDocument.DiscountType.FIXED,
                "discount_value": "120.00",
                "notes": "Invalid full discount",
                "lines-TOTAL_FORMS": "1",
                "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "1",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-product": self.product.pk,
                "lines-0-description": "Tea",
                "lines-0-quantity": "1.000",
                "lines-0-unit_price": "120.00",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Discount must be less than the sale subtotal")
        self.assertFalse(TradeDocument.objects.exists())

    def test_discount_controls_follow_the_sale_line_grid_only(self):
        sale_response = self.client.get(reverse("sale-create"))
        self.assertEqual(sale_response.status_code, 200)
        sale_html = sale_response.content.decode()
        self.assertContains(sale_response, "Sale adjustment")
        self.assertGreater(
            sale_html.index("id_discount_type"),
            sale_html.index("data-formset-lines"),
        )

        purchase_response = self.client.get(reverse("purchase-create"))
        self.assertEqual(purchase_response.status_code, 200)
        self.assertNotContains(purchase_response, "Sale adjustment")
        self.assertNotContains(purchase_response, "id_discount_type")

    def test_api_assigns_read_only_automatic_number(self):
        api = APIClient()
        api.force_authenticate(self.user)
        response = api.post(
            "/api/v1/sales/",
            {
                "number": "99999999",
                "document_date": "2026-08-26",
                "party": self.customer.pk,
                "period": self.period.pk,
                "debit_account": self.receivable.pk,
                "credit_account": self.revenue.pk,
                "notes": "API sale",
                "lines": [{
                    "product": self.product.pk,
                    "description": "Tea",
                    "quantity": "1.000",
                    "unit_price": "120.00",
                }],
            },
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["number"], "26000002")
        self.assertEqual(response.data["subtotal"], "120.00")
        self.assertEqual(response.data["discount_amount"], "0.00")

    def test_api_validates_sale_only_discount_boundaries(self):
        api = APIClient()
        api.force_authenticate(self.user)
        base_payload = {
            "document_date": "2026-08-26",
            "party": self.customer.pk,
            "period": self.period.pk,
            "debit_account": self.receivable.pk,
            "credit_account": self.revenue.pk,
            "discount_type": TradeDocument.DiscountType.PERCENTAGE,
            "discount_value": "100.00",
            "lines": [{
                "product": self.product.pk,
                "description": "Tea",
                "quantity": "1.000",
                "unit_price": "120.00",
            }],
        }
        response = api.post(
            "/api/v1/sales/",
            base_payload,
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("discount_value", response.data)

        purchase_payload = {
            **base_payload,
            "party": self.supplier.pk,
            "debit_account": self.inventory.pk,
            "credit_account": self.payable.pk,
            "discount_type": TradeDocument.DiscountType.FIXED,
            "discount_value": "10.00",
        }
        response = api.post(
            "/api/v1/purchases/",
            purchase_payload,
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 400)

    def test_empty_report_is_valid(self):
        response = self.client.get(reverse("purchase-csv"))
        self.assertContains(response, "No records for the selected filters.")
        response = self.client.get(reverse("purchase-pdf"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))
        response = self.client.get(reverse("stock-movement-csv"), {"date_from": "2099-01-01"})
        self.assertContains(response, "No records for the selected filters.")
        response = self.client.get(reverse("stock-movement-pdf"), {"date_from": "2099-01-01"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"%PDF"))


    def test_mutual_balances_can_be_set_off_atomically_and_printed(self):
        purchase = self.make_document(
            TradeDocument.Kind.PURCHASE, quantity="2.000", party=self.both_party
        )
        sale = self.make_document(
            TradeDocument.Kind.SALE, quantity="2.000", party=self.both_party
        )
        document_repository = DjangoTradeDocumentRepository()
        document_repository.post(
            document_id=purchase.pk, business_id=self.business.pk
        )
        document_repository.post(document_id=sale.pk, business_id=self.business.pk)

        key = uuid.uuid4()
        repository = DjangoBalanceSetoffRepository()
        setoff = repository.create(
            business_id=self.business.pk,
            party_id=self.both_party.pk,
            setoff_date=date(2026, 8, 27),
            sale_allocations=(
                SetoffAllocationCommand(sale.pk, Decimal("120.00")),
            ),
            purchase_allocations=(
                SetoffAllocationCommand(purchase.pk, Decimal("120.00")),
            ),
            idempotency_key=key,
            notes="Mutual invoice settlement",
            user_id=self.user.pk,
        )
        repeated = repository.create(
            business_id=self.business.pk,
            party_id=self.both_party.pk,
            setoff_date=date(2026, 8, 27),
            sale_allocations=(
                SetoffAllocationCommand(sale.pk, Decimal("120.00")),
            ),
            purchase_allocations=(
                SetoffAllocationCommand(purchase.pk, Decimal("120.00")),
            ),
            idempotency_key=key,
            user_id=self.user.pk,
        )
        self.assertEqual(repeated.pk, setoff.pk)
        self.assertEqual(BalanceSetoff.objects.count(), 1)
        self.assertEqual(setoff.voucher.voucher_type, Voucher.Type.CONTRA)
        self.assertTrue(setoff.journal_entry.posted)
        self.assertEqual(setoff.journal_entry.total_debit, Decimal("120.00"))
        self.assertTrue(
            setoff.journal_entry.lines.filter(
                account=self.payable, debit=Decimal("120.00")
            ).exists()
        )
        self.assertTrue(
            setoff.journal_entry.lines.filter(
                account=self.receivable, credit=Decimal("120.00")
            ).exists()
        )
        sale.refresh_from_db()
        purchase.refresh_from_db()
        self.assertEqual(sale.paid_amount, Decimal("120.00"))
        self.assertEqual(sale.balance_due, Decimal("120.00"))
        self.assertEqual(purchase.paid_amount, Decimal("120.00"))
        self.assertEqual(purchase.balance_due, Decimal("40.00"))
        self.assertEqual(SalePayment.objects.count(), 0)
        self.assertEqual(PurchasePayment.objects.count(), 0)

        center = self.client.get(reverse("payment-center"))
        self.assertContains(center, "Mutual balances")
        self.assertContains(center, self.both_party.name)
        detail = self.client.get(reverse("balance-setoff-detail", args=[setoff.pk]))
        self.assertContains(detail, setoff.number)
        pdf = self.client.get(reverse("balance-setoff-pdf", args=[setoff.pk]))
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")

    def test_setoff_rejects_unbalanced_or_excess_allocations_without_side_effects(self):
        purchase = self.make_document(
            TradeDocument.Kind.PURCHASE, quantity="1.000", party=self.both_party
        )
        sale = self.make_document(
            TradeDocument.Kind.SALE, quantity="1.000", party=self.both_party
        )
        documents = DjangoTradeDocumentRepository()
        documents.post(document_id=purchase.pk, business_id=self.business.pk)
        documents.post(document_id=sale.pk, business_id=self.business.pk)
        repository = DjangoBalanceSetoffRepository()
        journal_count = JournalEntry.objects.count()
        with self.assertRaises(ValidationError):
            repository.create(
                business_id=self.business.pk,
                party_id=self.both_party.pk,
                setoff_date=date(2026, 8, 27),
                sale_allocations=(SetoffAllocationCommand(sale.pk, Decimal("90.00")),),
                purchase_allocations=(SetoffAllocationCommand(purchase.pk, Decimal("80.00")),),
                idempotency_key=uuid.uuid4(),
                user_id=self.user.pk,
            )
        with self.assertRaises(ValidationError):
            repository.create(
                business_id=self.business.pk,
                party_id=self.both_party.pk,
                setoff_date=date(2026, 8, 27),
                sale_allocations=(SetoffAllocationCommand(sale.pk, Decimal("121.00")),),
                purchase_allocations=(SetoffAllocationCommand(purchase.pk, Decimal("121.00")),),
                idempotency_key=uuid.uuid4(),
                user_id=self.user.pk,
            )
        self.assertEqual(BalanceSetoff.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), journal_count)
        self.assertEqual(SaleSetoffAllocation.objects.count(), 0)
        self.assertEqual(PurchaseSetoffAllocation.objects.count(), 0)

    def test_setoff_form_posts_selected_allocations(self):
        purchase = self.make_document(
            TradeDocument.Kind.PURCHASE, quantity="1.000", party=self.both_party
        )
        sale = self.make_document(
            TradeDocument.Kind.SALE, quantity="1.000", party=self.both_party
        )
        documents = DjangoTradeDocumentRepository()
        documents.post(document_id=purchase.pk, business_id=self.business.pk)
        documents.post(document_id=sale.pk, business_id=self.business.pk)
        page = self.client.get(
            reverse("balance-setoff-create", args=[self.both_party.pk])
        )
        self.assertContains(page, "Set off mutual balances")
        response = self.client.post(
            reverse("balance-setoff-create", args=[self.both_party.pk]),
            {
                "setoff_date": "2026-08-27",
                "sale_%s" % sale.pk: "80.00",
                "purchase_%s" % purchase.pk: "80.00",
                "notes": "UI set-off",
                "idempotency_key": str(uuid.uuid4()),
                "confirm": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(BalanceSetoff.objects.count(), 1)

    def test_balance_setoff_api_creates_and_lists_contra(self):
        purchase = self.make_document(
            TradeDocument.Kind.PURCHASE, quantity="1.000", party=self.both_party
        )
        sale = self.make_document(
            TradeDocument.Kind.SALE, quantity="1.000", party=self.both_party
        )
        documents = DjangoTradeDocumentRepository()
        documents.post(document_id=purchase.pk, business_id=self.business.pk)
        documents.post(document_id=sale.pk, business_id=self.business.pk)
        api = APIClient()
        api.force_authenticate(self.user)
        response = api.post(
            reverse("api-balance-setoff-list"),
            {
                "party": self.both_party.pk,
                "setoff_date": "2026-08-27",
                "sale_allocations": [
                    {"document_id": sale.pk, "amount": "80.00"}
                ],
                "purchase_allocations": [
                    {"document_id": purchase.pk, "amount": "80.00"}
                ],
                "idempotency_key": str(uuid.uuid4()),
            },
            format="json",
            HTTP_X_BUSINESS_ID=str(self.business.pk),
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["total_amount"], "80.00")
        self.assertEqual(response.data["sale_allocations"][0]["document_number"], sale.number)
        listing = api.get(
            reverse("api-balance-setoff-list"),
            HTTP_X_BUSINESS_ID=str(self.business.pk),
        )
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.data), 1)


class TradeTenantApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="sales-user")
        self.business = Business.objects.create(name="Sales Tenant", slug="sales-tenant")
        self.other = Business.objects.create(name="Other Tenant", slug="other-tenant")
        role = Role.objects.create(
            business=self.business, name="Sales viewer", permissions=[SALES_VIEW]
        )
        Membership.objects.create(user=self.user, business=self.business, role=role)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_sales_api_does_not_disclose_other_tenant(self):
        response = self.api.get("/api/v1/sales/", HTTP_X_BUSINESS_ID=self.other.pk)
        self.assertEqual(response.status_code, 404)
        response = self.api.get("/api/v1/sales/", HTTP_X_BUSINESS_ID=self.business.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_sales_view_permission_does_not_allow_creation(self):
        response = self.api.post(
            "/api/v1/sales/", {}, format="json", HTTP_X_BUSINESS_ID=self.business.pk
        )
        self.assertEqual(response.status_code, 403)
        response = self.api.post(
            "/api/v1/purchases/999/pay-supplier/",
            {},
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 403)
        response = self.api.post(
            "/api/v1/sales/999/receive-payment/",
            {},
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 403)

    def test_payment_center_respects_independent_sales_and_purchase_permissions(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("payment-center"), {"business": self.business.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open customer receivables")
        self.assertNotContains(response, "Open supplier payables")
        self.assertEqual(
            self.client.get(
                reverse("purchase-pay-supplier", args=[999]),
                {"business": self.business.pk},
            ).status_code,
            403,
        )
