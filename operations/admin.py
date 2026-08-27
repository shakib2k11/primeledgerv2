from django.contrib import admin

from operations.models import TradeDocument, TradeLine


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
