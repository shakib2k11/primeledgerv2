from django.contrib import admin
from .models import (
    Account, ExpensePayment, ExpenseRecord, FiscalPeriod, JournalEntry,
    JournalLine, MoneyReceipt, Voucher,
)


@admin.register(Account, FiscalPeriod)
class AccountingSetupAdmin(admin.ModelAdmin):
    list_filter = ("business",)


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("reference", "business", "entry_date", "posted")
    list_filter = ("business", "posted")

    def has_delete_permission(self, request, obj=None):
        return bool(obj and not obj.posted and not obj.period.is_locked)


@admin.register(JournalLine)
class JournalLineAdmin(admin.ModelAdmin):
    list_display = ("entry", "account", "debit", "credit")

    def has_delete_permission(self, request, obj=None):
        return bool(obj and not obj.entry.posted and not obj.entry.period.is_locked)


@admin.register(Voucher)
class VoucherAdmin(admin.ModelAdmin):
    list_display = ("number", "business", "voucher_type", "voucher_date", "total")
    list_filter = ("business", "voucher_type")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(MoneyReceipt)
class MoneyReceiptAdmin(admin.ModelAdmin):
    list_display = ("number", "business", "party", "receipt_date", "amount")
    list_filter = ("business", "receipt_date")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ExpensePaymentInline(admin.TabularInline):
    model = ExpensePayment
    extra = 0
    readonly_fields = (
        "number", "payment_date", "payment_account", "amount", "journal_entry",
        "voucher", "notes", "paid_by", "created_at",
    )
    can_delete = False


@admin.register(ExpenseRecord)
class ExpenseRecordAdmin(admin.ModelAdmin):
    list_display = (
        "number", "business", "expense_date", "expense_account", "payee",
        "settlement", "amount",
    )
    list_filter = ("business", "expense_date", "settlement", "expense_account")
    readonly_fields = (
        "business", "number", "expense_date", "payee", "expense_account",
        "settlement", "payment_account", "payable_account", "amount",
        "description", "external_reference", "journal_entry", "voucher",
        "idempotency_key", "created_by", "created_at",
    )
    inlines = (ExpensePaymentInline,)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ExpensePayment)
class ExpensePaymentAdmin(admin.ModelAdmin):
    list_display = ("number", "business", "expense", "payment_date", "amount")
    list_filter = ("business", "payment_date", "payment_account")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
