from django.contrib import admin
from .models import Account, FiscalPeriod, JournalEntry, JournalLine, Voucher


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
