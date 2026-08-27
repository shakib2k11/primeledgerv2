from datetime import date

from django.contrib.auth import get_user_model
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounting.models import Account, ChartOfAccountsTemplate

from .application.services import CONTACTS_VIEW, INVENTORY_VIEW
from .infrastructure.numbering import allocate_reference_number
from .models import AnnualReferenceSequence, Business, InventoryUnit, Membership, Party, Product, Role, StockMovement


class ReferenceNumberTests(TestCase):
    def setUp(self):
        self.business = Business.objects.create(name="Numbered Business", slug="numbered-business")
        self.other = Business.objects.create(name="Other Numbered Business", slug="other-numbered-business")

    def test_sequence_is_shared_per_tenant_and_rolls_each_year(self):
        self.assertEqual(
            allocate_reference_number(business_id=self.business.pk, occurred_on=date(2026, 1, 1)),
            "26000001",
        )
        self.assertEqual(
            allocate_reference_number(business_id=self.business.pk, occurred_on=date(2026, 12, 31)),
            "26000002",
        )
        self.assertEqual(
            allocate_reference_number(business_id=self.business.pk, occurred_on=date(2027, 1, 1)),
            "27000001",
        )
        self.assertEqual(
            allocate_reference_number(business_id=self.other.pk, occurred_on=date(2026, 6, 1)),
            "26000001",
        )

    def test_exhausted_annual_sequence_is_rejected(self):
        AnnualReferenceSequence.objects.create(
            business=self.business,
            year=2026,
            last_value=999999,
        )
        with self.assertRaises(ValidationError):
            allocate_reference_number(
                business_id=self.business.pk,
                occurred_on=date(2026, 12, 31),
            )

    def test_unsupported_century_is_rejected(self):
        with self.assertRaises(ValidationError):
            allocate_reference_number(
                business_id=self.business.pk,
                occurred_on=date(2100, 1, 1),
            )


class TenantDashboardTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="owner", password="test-password")
        self.business = Business.objects.create(name="Harbor Goods", slug="harbor-goods")
        Membership.objects.create(user=self.user, business=self.business, level=Membership.Level.BUSINESS_ADMIN)
        self.other_business = Business.objects.create(name="Other Shop", slug="other-shop")
        self.piece = InventoryUnit.objects.get(business__isnull=True, code="piece")
        Party.objects.create(business=self.business, name="Amina Traders", kind=Party.Kind.SUPPLIER)
        Party.objects.create(business=self.other_business, name="Hidden Contact", kind=Party.Kind.CUSTOMER)
        Product.objects.create(business=self.business, name="Rice", sku="RICE-1", unit=self.piece)
        Product.objects.create(business=self.other_business, name="Hidden Product", sku="HIDDEN-1", unit=self.piece)

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse("dashboard"))
        self.assertRedirects(response, "/login/?next=/")

    def test_dashboard_and_lists_only_show_membership_business(self):
        self.client.login(username="owner", password="test-password")

        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Harbor Goods")
        self.assertNotContains(response, "Hidden Contact")

        response = self.client.get(reverse("party-list"))
        self.assertContains(response, "Amina Traders")
        self.assertNotContains(response, "Hidden Contact")

        response = self.client.get(reverse("product-list"))
        self.assertContains(response, "Rice")
        self.assertNotContains(response, "Hidden Product")

    def test_search_is_tenant_scoped(self):
        self.client.login(username="owner", password="test-password")
        response = self.client.get(reverse("party-list"), {"q": "Hidden"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Hidden Contact")

    def test_business_admin_can_record_stock_movement_from_ui(self):
        self.client.login(username="owner", password="test-password")
        response = self.client.post(
            reverse("stock-movement-create"),
            {
                "product": Product.objects.get(business=self.business).pk,
                "direction": StockMovement.Direction.IN,
                "quantity": "12.500",
                "unit_cost": "48.00",
                "reference": "OPENING",
                "occurred_at": "2026-08-26T10:30",
            },
        )
        self.assertRedirects(response, reverse("stock-movement-list"))
        self.assertTrue(
            StockMovement.objects.filter(business=self.business, reference="OPENING").exists()
        )


class PermissionAndApiTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="employee", password="test-password")
        self.business = Business.objects.create(name="Harbor Goods", slug="harbor")
        self.other = Business.objects.create(name="Other", slug="other")
        self.piece = InventoryUnit.objects.get(business__isnull=True, code="piece")
        self.role = Role.objects.create(
            business=self.business,
            name="Viewer",
            permissions=[CONTACTS_VIEW, INVENTORY_VIEW],
        )
        Membership.objects.create(
            user=self.user,
            business=self.business,
            level=Membership.Level.EMPLOYEE,
            role=self.role,
        )
        self.party = Party.objects.create(
            business=self.business, name="Visible", kind=Party.Kind.CUSTOMER
        )
        Party.objects.create(
            business=self.other, name="Secret", kind=Party.Kind.CUSTOMER
        )
        self.product = Product.objects.create(
            business=self.business, name="Rice", sku="R-1", unit=self.piece
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_view_permission_does_not_grant_manage_permission(self):
        self.client.login(username="employee", password="test-password")
        self.assertEqual(self.client.get(reverse("party-list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("party-create")).status_code, 403)

    def test_api_requires_authorized_explicit_business(self):
        response = self.api.get("/api/v1/parties/")
        self.assertEqual(response.status_code, 404)
        response = self.api.get("/api/v1/parties/", HTTP_X_BUSINESS_ID=self.other.pk)
        self.assertEqual(response.status_code, 404)
        response = self.api.get("/api/v1/parties/", HTTP_X_BUSINESS_ID=self.business.pk)
        self.assertEqual(response.status_code, 200)
        names = [item["name"] for item in response.data["results"]]
        self.assertEqual(names, ["Visible"])

    def test_api_manage_permission_is_enforced(self):
        response = self.api.post(
            "/api/v1/products/",
            {
                "name": "Oil",
                "unit": "litre",
                "sale_price": "10.00",
                "purchase_price": "8.00",
                "reorder_level": "1.000",
            },
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 403)

    def test_business_admin_can_create_without_client_tenant_field(self):
        self.role.permissions.append("inventory.manage")
        self.role.save(update_fields=["permissions"])
        response = self.api.post(
            "/api/v1/products/",
            {
                "name": "Oil",
                "business": self.other.pk,
                "unit": "litre",
                "sale_price": "10.00",
                "purchase_price": "8.00",
                "reorder_level": "1.000",
            },
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Product.objects.get(name="Oil").business, self.business)

    def test_stock_movement_screen_respects_manage_permission(self):
        response = self.client.get(reverse("stock-movement-list"))
        self.assertEqual(response.status_code, 302)
        self.client.login(username="employee", password="test-password")
        response = self.client.get(reverse("stock-movement-list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Record movement</a>")
        response = self.client.get(reverse("stock-movement-create"))
        self.assertEqual(response.status_code, 403)


class TenantOnboardingUiTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.root = user_model.objects.create_superuser(
            username="root-admin", email="root@example.com", password="root-password"
        )
        self.regular = user_model.objects.create_user(
            username="regular", password="regular-password"
        )

    def test_only_superuser_can_access_business_register(self):
        self.client.login(username="regular", password="regular-password")
        self.assertEqual(self.client.get(reverse("tenant-list")).status_code, 403)
        self.client.logout()
        self.client.login(username="root-admin", password="root-password")
        response = self.client.get(reverse("tenant-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create business")

    def test_only_superuser_can_manage_account_templates(self):
        template = ChartOfAccountsTemplate.objects.get(is_default=True)
        self.client.login(username="regular", password="regular-password")
        self.assertEqual(self.client.get(reverse("account-template-list")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("account-template-detail", args=[template.pk])).status_code,
            403,
        )
        self.client.logout()
        self.client.login(username="root-admin", password="root-password")
        response = self.client.get(reverse("account-template-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Small Business")
        self.assertContains(response, "43")

    def test_create_business_and_assign_first_admin(self):
        self.client.login(username="root-admin", password="root-password")
        response = self.client.post(
            reverse("tenant-create"),
            {
                "name": "Amina Traders",
                "slug": "amina-traders",
                "legal_name": "Amina Traders Limited",
                "phone": "01700000000",
                "address": "Dhaka",
                "currency": "BDT",
                "locale": "en-bd",
                "inherit_default_units": "on",
                "is_active": "on",
            },
        )
        business = Business.objects.get(slug="amina-traders")
        self.assertEqual(Account.objects.filter(business=business).count(), 43)
        self.assertTrue(
            Account.objects.filter(
                business=business,
                system_role=Account.SystemRole.COST_OF_GOODS_SOLD,
            ).exists()
        )
        self.assertRedirects(response, reverse("tenant-detail", args=[business.pk]))
        response = self.client.post(
            reverse("tenant-admin-create", args=[business.pk]),
            {
                "username": "amina-admin",
                "first_name": "Amina",
                "last_name": "Rahman",
                "email": "amina@example.com",
                "password1": "business-password",
                "password2": "business-password",
            },
        )
        self.assertRedirects(response, reverse("tenant-detail", args=[business.pk]))
        membership = Membership.objects.get(user__username="amina-admin", business=business)
        self.assertEqual(membership.level, Membership.Level.BUSINESS_ADMIN)
        self.assertTrue(membership.is_active)
        self.assertFalse(membership.user.is_superuser)


class InventoryUnitTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(username="unit-owner", password="password")
        self.root = user_model.objects.create_superuser(
            username="unit-root", email="root@example.com", password="password"
        )
        self.business = Business.objects.create(name="Unit Shop", slug="unit-shop")
        self.other = Business.objects.create(name="Other Unit Shop", slug="other-unit-shop")
        Membership.objects.create(
            user=self.owner,
            business=self.business,
            level=Membership.Level.BUSINESS_ADMIN,
        )
        self.piece = InventoryUnit.objects.get(business__isnull=True, code="piece")

    def test_business_admin_creates_custom_unit_and_uses_it_for_product(self):
        self.client.login(username="unit-owner", password="password")
        response = self.client.post(reverse("unit-create"), {
            "code": "sack",
            "name": "Sack",
            "symbol": "sack",
            "is_active": "on",
        })
        self.assertRedirects(response, reverse("unit-list"))
        unit = InventoryUnit.objects.get(business=self.business, code="sack")
        response = self.client.post(reverse("product-create"), {
            "name": "Rice sack",
            "sku": "RICE-SACK",
            "unit": unit.pk,
            "sale_price": "1500.00",
            "purchase_price": "1300.00",
            "reorder_level": "2.000",
        })
        self.assertRedirects(response, reverse("product-list"))
        self.assertEqual(Product.objects.get(sku="RICE-SACK").unit, unit)

    def test_cross_tenant_unit_is_rejected(self):
        foreign = InventoryUnit.objects.create(
            business=self.other,
            code="crate",
            name="Crate",
            symbol="crt",
        )
        product = Product(
            business=self.business,
            name="Invalid",
            sku="INVALID",
            unit=foreign,
        )
        with self.assertRaises(ValidationError):
            product.full_clean()

    def test_inheritance_cannot_be_disabled_while_default_unit_is_used(self):
        Product.objects.create(
            business=self.business,
            name="Default piece item",
            sku="PIECE-1",
            unit=self.piece,
        )
        self.client.login(username="unit-owner", password="password")
        response = self.client.post(reverse("unit-inheritance-update"), {})
        self.assertEqual(response.status_code, 400)
        self.business.refresh_from_db()
        self.assertTrue(self.business.inherit_default_units)
        self.assertContains(response, "Reassign products", status_code=400)

    def test_unit_api_lists_defaults_and_tenant_units_without_leaking(self):
        own = InventoryUnit.objects.create(
            business=self.business, code="bundle", name="Bundle", symbol="bdl"
        )
        InventoryUnit.objects.create(
            business=self.other, code="crate", name="Crate", symbol="crt"
        )
        api = APIClient()
        api.force_authenticate(self.owner)
        response = api.get(
            "/api/v1/inventory-units/",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.data["results"]}
        self.assertIn(self.piece.pk, ids)
        self.assertIn(own.pk, ids)
        self.assertNotIn(
            InventoryUnit.objects.get(business=self.other, code="crate").pk,
            ids,
        )

    def test_product_api_rejects_unregistered_unit_code(self):
        api = APIClient()
        api.force_authenticate(self.owner)
        response = api.post(
            "/api/v1/products/",
            {
                "name": "Loose item",
                "sku": "LOOSE-1",
                "unit": "made-up-unit",
                "sale_price": "10.00",
                "purchase_price": "8.00",
                "reorder_level": "1.000",
            },
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("unit", response.data)

    def test_used_unit_code_is_immutable(self):
        Product.objects.create(
            business=self.business,
            name="Piece item",
            sku="LOCKED-UNIT",
            unit=self.piece,
        )
        self.piece.code = "changed-piece"
        with self.assertRaises(ValidationError):
            self.piece.full_clean()

    def test_only_superadmin_manages_default_units(self):
        self.client.login(username="unit-owner", password="password")
        self.assertEqual(self.client.get(reverse("default-unit-list")).status_code, 403)
        self.client.logout()
        self.client.login(username="unit-root", password="password")
        response = self.client.post(reverse("default-unit-create"), {
            "code": "roll",
            "name": "Roll",
            "symbol": "roll",
            "is_active": "on",
        })
        self.assertRedirects(response, reverse("default-unit-list"))
        self.assertTrue(
            InventoryUnit.objects.filter(business__isnull=True, code="roll").exists()
        )
