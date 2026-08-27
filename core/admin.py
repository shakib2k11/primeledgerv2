from django.contrib import admin
from .models import Business, InventoryUnit, Membership, Party, Product, Role, StockMovement


class NoDeleteAdmin(admin.ModelAdmin):
    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Business)
class BusinessAdmin(NoDeleteAdmin):
    list_display = ("name", "slug", "currency", "is_active")


@admin.register(Membership)
class MembershipAdmin(NoDeleteAdmin):
    list_display = ("user", "business", "level", "is_active")
    list_filter = ("business", "level", "is_active")


@admin.register(Role, Party, Product, InventoryUnit)
class BusinessOwnedAdmin(NoDeleteAdmin):
    list_filter = ("business",)


@admin.register(StockMovement)
class StockMovementAdmin(NoDeleteAdmin):
    list_display = ("number", "product", "business", "direction", "quantity", "occurred_at")
    list_filter = ("business", "direction")

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)

    def has_add_permission(self, request):
        return False
