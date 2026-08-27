from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounting.models import Account, FiscalPeriod, JournalEntry, Voucher
from core.application.services import SALES_VIEW
from core.infrastructure.numbering import allocate_reference_number
from core.models import Business, InventoryUnit, Membership, Party, Product, Role, StockMovement
from operations.infrastructure.repositories import DjangoTradeDocumentRepository
from operations.models import TradeDocument, TradeLine


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
    ):
        document_date = date(2026, 8, 26)
        document = TradeDocument.objects.create(
            business=self.business,
            kind=kind,
            number=allocate_reference_number(
                business_id=self.business.pk,
                occurred_on=document_date,
            ),
            party=(self.customer if kind == TradeDocument.Kind.SALE else self.supplier),
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
