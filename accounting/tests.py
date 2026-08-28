from datetime import date
from decimal import Decimal
import uuid

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounting.application.services import ApplyAccountTemplateCommand, apply_account_template
from accounting.infrastructure.repositories import (
    DjangoAccountTemplateRepository,
    DjangoExpensePaymentRepository,
    DjangoExpenseRepository,
)
from accounting.models import (
    Account,
    ChartOfAccountsTemplate,
    ExpensePayment,
    ExpenseRecord,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    MoneyReceipt,
    Voucher,
)
from core.application.services import ACCOUNTING_MANAGE, ACCOUNTING_POST, ACCOUNTING_VIEW
from core.infrastructure.repositories import DjangoJournalRepository
from core.models import Business, Membership, Party, Role


class AccountingInvariantTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="owner", password="pw")
        self.business = Business.objects.create(name="Ledger", slug="ledger")
        Membership.objects.create(
            user=self.user,
            business=self.business,
            level=Membership.Level.BUSINESS_ADMIN,
        )
        self.period = FiscalPeriod.objects.create(
            business=self.business,
            name="FY26",
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        self.cash = Account.objects.create(
            business=self.business, code="1000", name="Cash", account_type=Account.Type.ASSET,
            system_role=Account.SystemRole.CASH,
        )
        self.sales = Account.objects.create(
            business=self.business, code="4000", name="Sales", account_type=Account.Type.INCOME
        )

    def make_entry(self, debit="100.00", credit="100.00"):
        entry = JournalEntry.objects.create(
            business=self.business,
            period=self.period,
            reference="J-1",
            description="Sale",
            entry_date=date(2026, 8, 25),
            created_by=self.user,
        )
        JournalLine.objects.create(entry=entry, account=self.cash, debit=Decimal(debit))
        JournalLine.objects.create(entry=entry, account=self.sales, credit=Decimal(credit))
        return entry

    def test_fiscal_period_has_human_readable_label(self):
        self.assertEqual(str(self.period), "FY26 (01 Jan 2026 – 31 Dec 2026)")

    def test_balanced_entry_posts_idempotently(self):
        entry = self.make_entry()
        repository = DjangoJournalRepository()
        repository.post(entry_id=entry.pk, business_id=self.business.pk)
        repository.post(entry_id=entry.pk, business_id=self.business.pk)
        entry.refresh_from_db()
        self.assertTrue(entry.posted)
        entry.description = "Changed history"
        with self.assertRaises(ValidationError):
            entry.save()
        with self.assertRaises(ValidationError):
            entry.lines.first().delete()

    def test_unbalanced_and_locked_entries_do_not_post(self):
        entry = self.make_entry(credit="90.00")
        with self.assertRaises(ValidationError):
            DjangoJournalRepository().post(entry_id=entry.pk, business_id=self.business.pk)
        self.period.is_locked = True
        self.period.save(update_fields=["is_locked"])
        with self.assertRaises(ValidationError):
            entry.post()

    def test_cross_tenant_account_is_rejected(self):
        other = Business.objects.create(name="Other", slug="other-ledger")
        foreign = Account.objects.create(
            business=other, code="1000", name="Foreign", account_type=Account.Type.ASSET
        )
        entry = JournalEntry.objects.create(
            business=self.business,
            period=self.period,
            reference="J-2",
            description="Invalid",
            entry_date=date(2026, 8, 25),
        )
        line = JournalLine(entry=entry, account=foreign, debit=Decimal("1.00"))
        with self.assertRaises(ValidationError):
            line.full_clean()

    def test_periods_cannot_overlap(self):
        period = FiscalPeriod(
            business=self.business,
            name="Overlap",
            starts_on=date(2026, 12, 1),
            ends_on=date(2027, 1, 31),
        )
        with self.assertRaises(ValidationError):
            period.full_clean()

    def test_voucher_requires_matching_posted_journal(self):
        entry = self.make_entry()
        voucher = Voucher(
            business=self.business,
            voucher_type=Voucher.Type.SALE,
            number="S-1",
            journal_entry=entry,
            total=Decimal("100.00"),
            voucher_date=entry.entry_date,
        )
        with self.assertRaises(ValidationError):
            voucher.full_clean()
        entry.post()
        voucher.full_clean()


class ChartTemplateTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="template-owner", password="pw")
        self.business = Business.objects.create(name="Template Ledger", slug="template-ledger")
        Membership.objects.create(
            user=self.user,
            business=self.business,
            level=Membership.Level.BUSINESS_ADMIN,
        )
        self.template = ChartOfAccountsTemplate.objects.get(is_default=True)

    def test_seeded_default_template_has_expected_accounts_and_roles(self):
        self.assertEqual(self.template.lines.count(), 43)
        self.assertTrue(
            self.template.lines.filter(
                code="5010",
                system_role=Account.SystemRole.COST_OF_GOODS_SOLD,
            ).exists()
        )
        self.assertFalse(self.template.lines.get(code="1150").account_is_active)

    def test_template_application_is_tenant_owned_and_idempotent(self):
        repository = DjangoAccountTemplateRepository()
        first = apply_account_template(
            ApplyAccountTemplateCommand(
                template_id=self.template.pk,
                business_id=self.business.pk,
                user_id=self.user.pk,
            ),
            repository,
        )
        second = apply_account_template(
            ApplyAccountTemplateCommand(
                template_id=self.template.pk,
                business_id=self.business.pk,
                user_id=self.user.pk,
            ),
            repository,
        )
        self.assertEqual(first.created, 43)
        self.assertEqual(second.created, 0)
        self.assertEqual(second.matched, 43)
        self.assertEqual(Account.objects.filter(business=self.business).count(), 43)
        self.assertEqual(
            Account.objects.filter(business=self.business, is_system=True).count(),
            43,
        )
        self.assertEqual(Account.objects.filter(business=self.business, is_active=True).count(), 39)
        self.assertEqual(
            Account.objects.get(
                business=self.business,
                system_role=Account.SystemRole.INVENTORY,
            ).code,
            "1200",
        )

    def test_template_conflict_rolls_back_without_partial_chart(self):
        Account.objects.create(
            business=self.business,
            code="1010",
            name="Incorrect cash",
            account_type=Account.Type.LIABILITY,
        )
        with self.assertRaises(ValidationError):
            apply_account_template(
                ApplyAccountTemplateCommand(
                    template_id=self.template.pk,
                    business_id=self.business.pk,
                    user_id=self.user.pk,
                ),
                DjangoAccountTemplateRepository(),
            )
        self.assertEqual(Account.objects.filter(business=self.business).count(), 1)

    def test_business_admin_applies_template_from_ui(self):
        self.client.login(username="template-owner", password="pw")
        response = self.client.post(
            reverse("account-template-apply"),
            {"template": self.template.pk},
        )
        self.assertRedirects(response, reverse("account-list"))
        self.assertTrue(
            Account.objects.filter(
                business=self.business,
                system_role=Account.SystemRole.COST_OF_GOODS_SOLD,
            ).exists()
        )


class JournalApiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="accountant")
        self.business = Business.objects.create(name="API Ledger", slug="api-ledger")
        role = Role.objects.create(
            business=self.business,
            name="Accountant",
            permissions=[ACCOUNTING_VIEW, ACCOUNTING_MANAGE, ACCOUNTING_POST],
        )
        Membership.objects.create(user=self.user, business=self.business, role=role)
        self.period = FiscalPeriod.objects.create(
            business=self.business,
            name="FY26",
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        self.cash = Account.objects.create(
            business=self.business, code="1000", name="Cash", account_type=Account.Type.ASSET
        )
        self.sales = Account.objects.create(
            business=self.business, code="4000", name="Sales", account_type=Account.Type.INCOME
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_create_and_post_balanced_journal(self):
        response = self.client.post(
            "/api/v1/journal-entries/",
            {
                "period": self.period.pk,
                "reference": "API-1",
                "description": "Cash sale",
                "entry_date": "2026-08-25",
                "lines": [
                    {"account": self.cash.pk, "debit": "125.00", "credit": "0.00"},
                    {"account": self.sales.pk, "debit": "0.00", "credit": "125.00"},
                ],
            },
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
        post_response = self.client.post(
            f"/api/v1/journal-entries/{response.data['id']}/post/",
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(post_response.status_code, 200, post_response.data)
        self.assertTrue(post_response.data["posted"])

    def test_unbalanced_journal_returns_stable_validation_error(self):
        entry = JournalEntry.objects.create(
            business=self.business,
            period=self.period,
            reference="API-2",
            description="Bad",
            entry_date=date(2026, 8, 25),
        )
        JournalLine.objects.create(entry=entry, account=self.cash, debit=Decimal("5.00"))
        JournalLine.objects.create(entry=entry, account=self.sales, credit=Decimal("4.00"))
        response = self.client.post(
            f"/api/v1/journal-entries/{entry.pk}/post/",
            format="json",
            HTTP_X_BUSINESS_ID=self.business.pk,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("non_field_errors", response.data)


class AccountingUiTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="ui-owner", password="pw")
        self.business = Business.objects.create(name="UI Ledger", slug="ui-ledger")
        Membership.objects.create(
            user=self.user,
            business=self.business,
            level=Membership.Level.BUSINESS_ADMIN,
        )
        self.period = FiscalPeriod.objects.create(
            business=self.business,
            name="FY26",
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        self.cash = Account.objects.create(
            business=self.business, code="1000", name="Cash", account_type=Account.Type.ASSET,
            system_role=Account.SystemRole.CASH,
        )
        self.sales = Account.objects.create(
            business=self.business, code="4000", name="Sales", account_type=Account.Type.INCOME
        )
        self.client.login(username="ui-owner", password="pw")

    def test_accounting_navigation_and_overview_are_furnished(self):
        response = self.client.get(reverse("accounting-overview"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Chart of accounts")
        self.assertContains(response, "New journal")
        self.assertNotContains(response, "Soon")

    def test_business_admin_can_edit_open_fiscal_period(self):
        response = self.client.post(
            reverse("period-edit", args=[self.period.pk]),
            {
                "name": "Financial Year 2026",
                "starts_on": "2026-01-01",
                "ends_on": "2026-12-30",
            },
        )
        self.assertRedirects(response, reverse("period-list"))
        self.period.refresh_from_db()
        self.assertEqual(self.period.name, "Financial Year 2026")
        self.assertEqual(self.period.ends_on, date(2026, 12, 30))

    def test_locked_period_name_can_change_but_dates_remain_protected(self):
        self.period.is_locked = True
        self.period.save(update_fields=["is_locked"])
        response = self.client.get(reverse("period-edit", args=[self.period.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].fields["starts_on"].disabled)
        self.assertTrue(response.context["form"].fields["ends_on"].disabled)

        response = self.client.post(
            reverse("period-edit", args=[self.period.pk]),
            {
                "name": "Closed FY26",
                "starts_on": "2025-01-01",
                "ends_on": "2027-12-31",
            },
        )
        self.assertRedirects(response, reverse("period-list"))
        self.period.refresh_from_db()
        self.assertEqual(self.period.name, "Closed FY26")
        self.assertEqual(self.period.starts_on, date(2026, 1, 1))
        self.assertEqual(self.period.ends_on, date(2026, 12, 31))

    def test_period_edit_is_permission_and_tenant_scoped(self):
        viewer = get_user_model().objects.create_user(username="period-viewer", password="pw")
        role = Role.objects.create(
            business=self.business,
            name="Period viewer",
            permissions=[ACCOUNTING_VIEW],
        )
        Membership.objects.create(user=viewer, business=self.business, role=role)
        self.client.logout()
        self.client.login(username="period-viewer", password="pw")
        self.assertEqual(
            self.client.get(reverse("period-edit", args=[self.period.pk])).status_code,
            403,
        )

        other_business = Business.objects.create(name="Other Period Ledger", slug="other-period-ledger")
        other_period = FiscalPeriod.objects.create(
            business=other_business,
            name="FY27",
            starts_on=date(2027, 1, 1),
            ends_on=date(2027, 12, 31),
        )
        self.client.logout()
        self.client.login(username="ui-owner", password="pw")
        self.assertEqual(
            self.client.get(reverse("period-edit", args=[other_period.pk])).status_code,
            404,
        )

    def test_create_post_and_voucher_workflow_through_ui(self):
        response = self.client.post(
            reverse("journal-create"),
            {
                "reference": "UI-1",
                "entry_date": "2026-08-25",
                "period": self.period.pk,
                "description": "UI cash sale",
                "lines-TOTAL_FORMS": "2",
                "lines-INITIAL_FORMS": "0",
                "lines-MIN_NUM_FORMS": "2",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-account": self.cash.pk,
                "lines-0-party": "",
                "lines-0-description": "Cash received",
                "lines-0-debit": "75.00",
                "lines-0-credit": "0.00",
                "lines-1-account": self.sales.pk,
                "lines-1-party": "",
                "lines-1-description": "Revenue",
                "lines-1-debit": "0.00",
                "lines-1-credit": "75.00",
            },
        )
        entry = JournalEntry.objects.get(reference="UI-1")
        self.assertRedirects(response, reverse("journal-detail", args=[entry.pk]))
        response = self.client.post(reverse("journal-post", args=[entry.pk]), {"confirm": "yes"})
        self.assertRedirects(response, reverse("journal-detail", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertTrue(entry.posted)
        response = self.client.post(
            reverse("voucher-create"),
            {
                "voucher_type": Voucher.Type.SALE,
                "number": "V-UI-1",
                "party": "",
                "journal_entry": entry.pk,
                "notes": "UI voucher",
            },
        )
        self.assertRedirects(response, reverse("voucher-list"))
        self.assertTrue(Voucher.objects.filter(number="V-UI-1", total=Decimal("75.00")).exists())

    def test_receipt_voucher_generates_persisted_money_receipt(self):
        entry = JournalEntry.objects.create(
            business=self.business,
            period=self.period,
            reference="RECEIPT:UI-2",
            description="Customer payment",
            entry_date=date(2026, 8, 26),
            created_by=self.user,
        )
        JournalLine.objects.create(
            entry=entry,
            account=self.cash,
            description="Cash received",
            debit=Decimal("125.00"),
        )
        JournalLine.objects.create(
            entry=entry,
            account=self.sales,
            description="Settlement offset",
            credit=Decimal("125.00"),
        )
        DjangoJournalRepository().post(
            entry_id=entry.pk,
            business_id=self.business.pk,
        )

        response = self.client.post(
            reverse("voucher-create"),
            {
                "voucher_type": Voucher.Type.RECEIPT,
                "number": "MR-UI-2",
                "party": "",
                "journal_entry": entry.pk,
                "notes": "Cash received",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        receipt = MoneyReceipt.objects.get(number="MR-UI-2", business=self.business)
        self.assertEqual(receipt.amount, Decimal("125.00"))
        self.assertEqual(receipt.payment_account, self.cash)
        self.assertContains(response, "Money receipt MR-UI-2 is ready")


class ExpenseWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="expense-owner", password="pw"
        )
        self.business = Business.objects.create(name="Expense House", slug="expense-house")
        Membership.objects.create(
            user=self.user,
            business=self.business,
            level=Membership.Level.BUSINESS_ADMIN,
        )
        self.period = FiscalPeriod.objects.create(
            business=self.business,
            name="FY26",
            starts_on=date(2026, 1, 1),
            ends_on=date(2026, 12, 31),
        )
        self.cash = Account.objects.create(
            business=self.business, code="1010", name="Cash",
            account_type=Account.Type.ASSET, system_role=Account.SystemRole.CASH,
        )
        self.bank = Account.objects.create(
            business=self.business, code="1020", name="Bank",
            account_type=Account.Type.ASSET, system_role=Account.SystemRole.BANK,
        )
        self.payable = Account.objects.create(
            business=self.business, code="2100", name="Accounts Payable",
            account_type=Account.Type.LIABILITY,
            system_role=Account.SystemRole.ACCOUNTS_PAYABLE,
        )
        self.rent = Account.objects.create(
            business=self.business, code="6020", name="Rent",
            account_type=Account.Type.EXPENSE,
        )
        self.salary = Account.objects.create(
            business=self.business, code="6010", name="Salaries and Wages",
            account_type=Account.Type.EXPENSE,
        )
        self.payee = Party.objects.create(
            business=self.business, name="Property Owner", kind=Party.Kind.SUPPLIER
        )
        self.client.login(username="expense-owner", password="pw")

    def create_expense(self, settlement=ExpenseRecord.Settlement.PAID, amount="1000.00"):
        return DjangoExpenseRepository().create(
            business_id=self.business.pk,
            expense_date=date(2026, 8, 28),
            expense_account_id=self.rent.pk,
            settlement=settlement,
            amount=Decimal(amount),
            description="August office rent",
            idempotency_key=uuid.uuid4(),
            payee_id=self.payee.pk,
            payment_account_id=(
                self.cash.pk if settlement == ExpenseRecord.Settlement.PAID else None
            ),
            external_reference="LEASE-AUG",
            user_id=self.user.pk,
        )

    def test_paid_expense_posts_balanced_journal_and_expense_voucher(self):
        expense = self.create_expense()
        self.assertEqual(expense.voucher.voucher_type, Voucher.Type.EXPENSE)
        self.assertTrue(expense.journal_entry.posted)
        self.assertEqual(expense.journal_entry.total_debit, Decimal("1000.00"))
        self.assertTrue(expense.journal_entry.lines.filter(
            account=self.rent, debit=Decimal("1000.00")
        ).exists())
        self.assertTrue(expense.journal_entry.lines.filter(
            account=self.cash, credit=Decimal("1000.00")
        ).exists())
        self.assertEqual(expense.paid_amount, Decimal("1000.00"))
        self.assertEqual(expense.balance_due, Decimal("0.00"))
        self.assertEqual(expense.payment_status, "paid")
        self.assertEqual(
            self.client.get(reverse("expense-pdf", args=[expense.pk])).status_code, 200
        )

    def test_pay_later_expense_supports_partial_payments_without_duplicate_expense(self):
        expense = self.create_expense(ExpenseRecord.Settlement.PAYABLE)
        self.assertTrue(expense.journal_entry.lines.filter(
            account=self.rent, debit=Decimal("1000.00")
        ).exists())
        self.assertTrue(expense.journal_entry.lines.filter(
            account=self.payable, credit=Decimal("1000.00")
        ).exists())
        payment = DjangoExpensePaymentRepository().pay(
            expense_id=expense.pk,
            business_id=self.business.pk,
            payment_account_id=self.bank.pk,
            amount=Decimal("400.00"),
            payment_date=date(2026, 8, 29),
            idempotency_key=uuid.uuid4(),
            notes="First installment",
            user_id=self.user.pk,
        )
        expense.refresh_from_db()
        self.assertEqual(expense.paid_amount, Decimal("400.00"))
        self.assertEqual(expense.balance_due, Decimal("600.00"))
        self.assertEqual(expense.payment_status, "partial")
        self.assertTrue(payment.journal_entry.lines.filter(
            account=self.payable, debit=Decimal("400.00")
        ).exists())
        self.assertTrue(payment.journal_entry.lines.filter(
            account=self.bank, credit=Decimal("400.00")
        ).exists())
        self.assertFalse(payment.journal_entry.lines.filter(account=self.rent).exists())
        self.assertEqual(payment.voucher.voucher_type, Voucher.Type.PAYMENT)

    def test_overpayment_and_locked_period_roll_back_every_side_effect(self):
        expense = self.create_expense(ExpenseRecord.Settlement.PAYABLE)
        initial_journals = JournalEntry.objects.count()
        with self.assertRaises(ValidationError):
            DjangoExpensePaymentRepository().pay(
                expense_id=expense.pk, business_id=self.business.pk,
                payment_account_id=self.cash.pk, amount=Decimal("1000.01"),
                payment_date=date(2026, 8, 29), idempotency_key=uuid.uuid4(),
                user_id=self.user.pk,
            )
        self.assertEqual(ExpensePayment.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), initial_journals)
        self.period.is_locked = True
        self.period.save(update_fields=["is_locked"])
        with self.assertRaises(ValidationError):
            DjangoExpensePaymentRepository().pay(
                expense_id=expense.pk, business_id=self.business.pk,
                payment_account_id=self.cash.pk, amount=Decimal("100.00"),
                payment_date=date(2026, 8, 29), idempotency_key=uuid.uuid4(),
                user_id=self.user.pk,
            )
        self.assertEqual(ExpensePayment.objects.count(), 0)

    def test_expense_ui_register_exports_and_pay_form(self):
        response = self.client.post(reverse("expense-create"), {
            "expense_date": "2026-08-28",
            "expense_account": self.salary.pk,
            "payee": self.payee.pk,
            "settlement": ExpenseRecord.Settlement.PAYABLE,
            "payment_account": "",
            "amount": "2500.00",
            "description": "August payroll",
            "external_reference": "PAYROLL-AUG",
            "idempotency_key": str(uuid.uuid4()),
            "confirm": "on",
        })
        self.assertEqual(response.status_code, 302)
        expense = ExpenseRecord.objects.get(description="August payroll")
        listing = self.client.get(reverse("expense-list"))
        self.assertContains(listing, "August payroll")
        self.assertContains(listing, "2500.00")
        self.assertEqual(self.client.get(reverse("expense-csv")).status_code, 200)
        pdf = self.client.get(reverse("expense-report-pdf"))
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        paid = self.client.post(reverse("expense-pay", args=[expense.pk]), {
            "payment_date": "2026-08-29",
            "payment_account": self.bank.pk,
            "amount": "1500.00",
            "notes": "Payroll installment",
            "idempotency_key": str(uuid.uuid4()),
            "confirm": "on",
        })
        self.assertEqual(paid.status_code, 302)
        self.assertEqual(ExpensePayment.objects.get().amount, Decimal("1500.00"))

    def test_expense_api_is_tenant_scoped_and_posts_payment(self):
        api = APIClient()
        api.force_authenticate(self.user)
        created = api.post(
            reverse("api-expense-list"),
            {
                "expense_date": "2026-08-28",
                "expense_account": self.rent.pk,
                "payee": self.payee.pk,
                "settlement": "payable",
                "amount": "900.00",
                "description": "Accrued rent",
            },
            format="json", HTTP_X_BUSINESS_ID=str(self.business.pk),
        )
        self.assertEqual(created.status_code, 201, created.data)
        expense_id = created.data["id"]
        paid = api.post(
            reverse("api-expense-pay", args=[expense_id]),
            {
                "payment_date": "2026-08-29",
                "payment_account": self.cash.pk,
                "amount": "300.00",
            },
            format="json", HTTP_X_BUSINESS_ID=str(self.business.pk),
        )
        self.assertEqual(paid.status_code, 201, paid.data)
        other = Business.objects.create(name="Other", slug="other-expense")
        hidden = api.get(
            reverse("api-expense-detail", args=[expense_id]),
            HTTP_X_BUSINESS_ID=str(other.pk),
        )
        self.assertEqual(hidden.status_code, 404)

    def test_accounting_viewer_cannot_post_expenses(self):
        viewer = get_user_model().objects.create_user(username="expense-viewer", password="pw")
        role = Role.objects.create(
            business=self.business, name="Expense viewer", permissions=[ACCOUNTING_VIEW]
        )
        Membership.objects.create(
            user=viewer, business=self.business, level=Membership.Level.EMPLOYEE,
            role=role,
        )
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(reverse("expense-list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("expense-create")).status_code, 403)
