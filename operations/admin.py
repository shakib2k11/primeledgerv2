from django.contrib import admin

from operations.models import PurchasePayment, SalePayment, TradeDocument, TradeLine


class TradeLineInline(admin.TabularInline):
    model = TradeLine
    extra = 0


@admin.register(TradeDocument)
class TradeDocumentAdmin(admin.ModelAdmin):
    list_display = ("number", "kind", "business", "party", "document_date", "total", "status")
    list_filter = ("business", "kind", "status")
    inlines = [TradeLineInline]
    readonly_fields = ("number",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return bool(obj and obj.status == TradeDocument.Status.DRAFT)


@admin.register(TradeLine)
class TradeLineAdmin(admin.ModelAdmin):
    list_display = ("document", "product", "quantity", "unit_price", "line_total")

    def has_delete_permission(self, request, obj=None):
        return bool(obj and obj.document.status == TradeDocument.Status.DRAFT)

    def has_add_permission(self, request):
        return False


@admin.register(SalePayment)
class SalePaymentAdmin(admin.ModelAdmin):
    list_display = (
        "number", "sale", "business", "payment_date", "payment_account", "amount"
    )
    list_filter = ("business", "payment_date", "payment_account")
    readonly_fields = (
        "business", "sale", "number", "payment_account", "amount", "payment_date",
        "journal_entry", "money_receipt", "notes", "idempotency_key", "received_by",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PurchasePayment)
class PurchasePaymentAdmin(admin.ModelAdmin):
    list_display = (
        "number", "purchase", "business", "payment_date", "payment_account", "amount"
    )
    list_filter = ("business", "payment_date", "payment_account")
    readonly_fields = (
        "business", "purchase", "number", "payment_account", "amount", "payment_date",
        "journal_entry", "voucher", "notes", "idempotency_key", "paid_by", "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
