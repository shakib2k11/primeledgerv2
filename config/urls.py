from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from accounting.api import (
    AccountViewSet, ExpenseViewSet, FiscalPeriodViewSet, JournalEntryViewSet,
    VoucherViewSet,
)
from accounting import views as accounting_views
from core.api import InventoryUnitViewSet, PartyViewSet, ProductViewSet, StockMovementViewSet
from core.views import (
    dashboard,
    health_check,
    party_create,
    party_list,
    product_create,
    product_edit,
    product_list,
    stock_movement_create,
    stock_movement_csv,
    stock_movement_list,
    stock_movement_pdf,
    unit_create,
    unit_edit,
    unit_inheritance_update,
    unit_list,
)
from core import settings_views
from core import report_views
from operations import views as operations_views
from operations.api import BalanceSetoffViewSet, PurchaseViewSet, SaleViewSet

router = DefaultRouter()
router.register("parties", PartyViewSet, basename="api-party")
router.register("products", ProductViewSet, basename="api-product")
router.register("inventory-units", InventoryUnitViewSet, basename="api-inventory-unit")
router.register("stock-movements", StockMovementViewSet, basename="api-stock-movement")
router.register("accounts", AccountViewSet, basename="api-account")
router.register("fiscal-periods", FiscalPeriodViewSet, basename="api-fiscal-period")
router.register("journal-entries", JournalEntryViewSet, basename="api-journal-entry")
router.register("vouchers", VoucherViewSet, basename="api-voucher")
router.register("expenses", ExpenseViewSet, basename="api-expense")
router.register("sales", SaleViewSet, basename="api-sale")
router.register("purchases", PurchaseViewSet, basename="api-purchase")
router.register("balance-setoffs", BalanceSetoffViewSet, basename="api-balance-setoff")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", dashboard, name="dashboard"),
    path("parties/", party_list, name="party-list"),
    path("parties/new/", party_create, name="party-create"),
    path("products/", product_list, name="product-list"),
    path("products/new/", product_create, name="product-create"),
    path("products/<int:pk>/edit/", product_edit, name="product-edit"),
    path("settings/inventory-units/", unit_list, name="unit-list"),
    path("settings/inventory-units/new/", unit_create, name="unit-create"),
    path("settings/inventory-units/inheritance/", unit_inheritance_update, name="unit-inheritance-update"),
    path("settings/inventory-units/<int:pk>/edit/", unit_edit, name="unit-edit"),
    path("inventory/movements/", stock_movement_list, name="stock-movement-list"),
    path("inventory/movements/new/", stock_movement_create, name="stock-movement-create"),
    path("inventory/movements/export.csv", stock_movement_csv, name="stock-movement-csv"),
    path("inventory/movements/export.pdf", stock_movement_pdf, name="stock-movement-pdf"),
    path("reports/", report_views.report_index, name="report-index"),
    path("reports/transactions/", report_views.transaction_register, name="transaction-register"),
    path("reports/transactions/export.csv", report_views.transaction_register_csv, name="transaction-register-csv"),
    path("reports/transactions/export.pdf", report_views.transaction_register_pdf, name="transaction-register-pdf"),
    path("reports/accounts/", report_views.account_activity_report, name="account-activity-report"),
    path("reports/accounts/export.csv", report_views.account_activity_report_csv, name="account-activity-report-csv"),
    path("reports/accounts/export.pdf", report_views.account_activity_report_pdf, name="account-activity-report-pdf"),
    path("reports/contacts/", report_views.contact_report, name="contact-report"),
    path("reports/contacts/export.csv", report_views.contact_report_csv, name="contact-report-csv"),
    path("reports/contacts/export.pdf", report_views.contact_report_pdf, name="contact-report-pdf"),
    path("reports/invoices/", report_views.invoice_report, name="invoice-report"),
    path("reports/invoices/export.csv", report_views.invoice_report_csv, name="invoice-report-csv"),
    path("reports/invoices/export.pdf", report_views.invoice_report_pdf, name="invoice-report-pdf"),
    path("reports/money-receipts/", report_views.money_receipt_report, name="money-receipt-report"),
    path("reports/money-receipts/export.csv", report_views.money_receipt_report_csv, name="money-receipt-report-csv"),
    path("reports/money-receipts/export.pdf", report_views.money_receipt_report_pdf, name="money-receipt-report-pdf"),
    path("reports/money-receipts/<int:pk>.pdf", report_views.money_receipt_document_pdf, name="money-receipt-document-pdf"),
    path("settings/businesses/", settings_views.tenant_list, name="tenant-list"),
    path("settings/businesses/new/", settings_views.tenant_create, name="tenant-create"),
    path("settings/businesses/<int:pk>/", settings_views.tenant_detail, name="tenant-detail"),
    path("settings/businesses/<int:pk>/edit/", settings_views.tenant_edit, name="tenant-edit"),
    path("settings/businesses/<int:pk>/admins/new/", settings_views.tenant_admin_create, name="tenant-admin-create"),
    path("settings/default-units/", settings_views.default_unit_list, name="default-unit-list"),
    path("settings/default-units/new/", settings_views.default_unit_create, name="default-unit-create"),
    path("settings/default-units/<int:pk>/edit/", settings_views.default_unit_edit, name="default-unit-edit"),
    path("settings/account-templates/", settings_views.account_template_list, name="account-template-list"),
    path("settings/account-templates/new/", settings_views.account_template_create, name="account-template-create"),
    path("settings/account-templates/<int:pk>/", settings_views.account_template_detail, name="account-template-detail"),
    path("settings/account-templates/<int:pk>/edit/", settings_views.account_template_edit, name="account-template-edit"),
    path("settings/account-templates/<int:pk>/accounts/new/", settings_views.account_template_line_create, name="account-template-line-create"),
    path("settings/account-templates/<int:pk>/accounts/<int:line_pk>/edit/", settings_views.account_template_line_edit, name="account-template-line-edit"),
    path("sales/", operations_views.document_list, {"kind": "sale"}, name="sale-list"),
    path("sales/new/", operations_views.document_create, {"kind": "sale"}, name="sale-create"),
    path("sales/export.csv", operations_views.document_csv, {"kind": "sale"}, name="sale-csv"),
    path("sales/export.pdf", operations_views.document_pdf, {"kind": "sale"}, name="sale-pdf"),
    path("sales/<int:pk>/", operations_views.document_detail, {"kind": "sale"}, name="sale-detail"),
    path("sales/<int:pk>/edit/", operations_views.document_edit, {"kind": "sale"}, name="sale-edit"),
    path("sales/<int:pk>/delete/", operations_views.document_delete, {"kind": "sale"}, name="sale-delete"),
    path("sales/<int:pk>/document.pdf", operations_views.document_print_pdf, {"kind": "sale"}, name="sale-document-pdf"),
    path("sales/<int:pk>/post/", operations_views.document_post, {"kind": "sale"}, name="sale-post"),
    path("sales/<int:pk>/receive-payment/", operations_views.sale_receive_payment, name="sale-receive-payment"),
    path("payments/", operations_views.payment_center, name="payment-center"),
    path("payments/set-offs/<int:party_id>/new/", operations_views.balance_setoff_create, name="balance-setoff-create"),
    path("payments/set-offs/posted/<int:pk>/", operations_views.balance_setoff_detail, name="balance-setoff-detail"),
    path("payments/set-offs/posted/<int:pk>.pdf", operations_views.balance_setoff_pdf, name="balance-setoff-pdf"),
    path("payments/supplier/<int:pk>.pdf", operations_views.purchase_payment_document_pdf, name="purchase-payment-document-pdf"),
    path("purchases/", operations_views.document_list, {"kind": "purchase"}, name="purchase-list"),
    path("purchases/new/", operations_views.document_create, {"kind": "purchase"}, name="purchase-create"),
    path("purchases/export.csv", operations_views.document_csv, {"kind": "purchase"}, name="purchase-csv"),
    path("purchases/export.pdf", operations_views.document_pdf, {"kind": "purchase"}, name="purchase-pdf"),
    path("purchases/<int:pk>/", operations_views.document_detail, {"kind": "purchase"}, name="purchase-detail"),
    path("purchases/<int:pk>/edit/", operations_views.document_edit, {"kind": "purchase"}, name="purchase-edit"),
    path("purchases/<int:pk>/delete/", operations_views.document_delete, {"kind": "purchase"}, name="purchase-delete"),
    path("purchases/<int:pk>/document.pdf", operations_views.document_print_pdf, {"kind": "purchase"}, name="purchase-document-pdf"),
    path("purchases/<int:pk>/post/", operations_views.document_post, {"kind": "purchase"}, name="purchase-post"),
    path("purchases/<int:pk>/pay-supplier/", operations_views.purchase_pay_supplier, name="purchase-pay-supplier"),
    path("accounting/", accounting_views.accounting_overview, name="accounting-overview"),
    path("expenses/", accounting_views.expense_list, name="expense-list"),
    path("expenses/new/", accounting_views.expense_create, name="expense-create"),
    path("expenses/export.csv", accounting_views.expense_csv, name="expense-csv"),
    path("expenses/export.pdf", accounting_views.expense_report_pdf, name="expense-report-pdf"),
    path("expenses/<int:pk>/", accounting_views.expense_detail, name="expense-detail"),
    path("expenses/<int:pk>/pay/", accounting_views.expense_pay, name="expense-pay"),
    path("expenses/<int:pk>/voucher.pdf", accounting_views.expense_pdf, name="expense-pdf"),
    path("expenses/payments/<int:pk>.pdf", accounting_views.expense_payment_pdf, name="expense-payment-pdf"),
    path("accounting/accounts/", accounting_views.account_list, name="account-list"),
    path("accounting/accounts/new/", accounting_views.account_create, name="account-create"),
    path("accounting/accounts/templates/", accounting_views.account_template_apply, name="account-template-apply"),
    path("accounting/periods/", accounting_views.period_list, name="period-list"),
    path("accounting/periods/new/", accounting_views.period_create, name="period-create"),
    path("accounting/periods/<int:pk>/edit/", accounting_views.period_edit, name="period-edit"),
    path("accounting/periods/<int:pk>/toggle-lock/", accounting_views.period_toggle_lock, name="period-toggle-lock"),
    path("accounting/journals/", accounting_views.journal_list, name="journal-list"),
    path("accounting/journals/new/", accounting_views.journal_create, name="journal-create"),
    path("accounting/journals/<int:pk>/", accounting_views.journal_detail, name="journal-detail"),
    path("accounting/journals/<int:pk>/edit/", accounting_views.journal_edit, name="journal-edit"),
    path("accounting/journals/<int:pk>/post/", accounting_views.journal_post, name="journal-post"),
    path("accounting/vouchers/", accounting_views.voucher_list, name="voucher-list"),
    path("accounting/vouchers/new/", accounting_views.voucher_create, name="voucher-create"),
    path("api/health/", health_check, name="health-check"),
    path("api/v1/health/", health_check, name="api-health-check"),
    path("api/v1/", include(router.urls)),
]
