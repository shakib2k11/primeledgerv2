from core.application.services import (
    ACCOUNTING_MANAGE,
    ACCOUNTING_POST,
    ACCOUNTING_VIEW,
    CONTACTS_MANAGE,
    CONTACTS_VIEW,
    INVENTORY_MANAGE,
    INVENTORY_VIEW,
    PURCHASES_MANAGE,
    PURCHASES_POST,
    PURCHASES_VIEW,
    SALES_MANAGE,
    SALES_POST,
    SALES_VIEW,
    membership_has_permission,
)
from core.models import Membership
from core.models import Business


def navigation_permissions(request):
    defaults = {
        "can_view_contacts": False,
        "can_manage_contacts": False,
        "can_view_inventory": False,
        "can_manage_inventory": False,
        "can_view_accounting": False,
        "can_manage_accounting": False,
        "can_post_accounting": False,
        "can_view_sales": False,
        "can_manage_sales": False,
        "can_post_sales": False,
        "can_view_purchases": False,
        "can_manage_purchases": False,
        "can_post_purchases": False,
        "available_businesses": [],
    }
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return defaults
    if user.is_superuser:
        return {
            **{key: True for key in defaults if key.startswith("can_")},
            "available_businesses": Business.objects.filter(is_active=True).order_by("name"),
        }
    business_id = request.session.get("business_id")
    memberships = Membership.objects.select_related("role").filter(
        user=user, is_active=True, business__is_active=True
    )
    if business_id:
        memberships = memberships.filter(business_id=business_id)
    membership = memberships.order_by("business__name", "pk").first()
    if membership is None:
        return {**defaults, "available_businesses": memberships.none()}
    return {
        "can_view_contacts": membership_has_permission(membership, CONTACTS_VIEW),
        "can_manage_contacts": membership_has_permission(membership, CONTACTS_MANAGE),
        "can_view_inventory": membership_has_permission(membership, INVENTORY_VIEW),
        "can_manage_inventory": membership_has_permission(membership, INVENTORY_MANAGE),
        "can_view_accounting": membership_has_permission(membership, ACCOUNTING_VIEW),
        "can_manage_accounting": membership_has_permission(membership, ACCOUNTING_MANAGE),
        "can_post_accounting": membership_has_permission(membership, ACCOUNTING_POST),
        "can_view_sales": membership_has_permission(membership, SALES_VIEW),
        "can_manage_sales": membership_has_permission(membership, SALES_MANAGE),
        "can_post_sales": membership_has_permission(membership, SALES_POST),
        "can_view_purchases": membership_has_permission(membership, PURCHASES_VIEW),
        "can_manage_purchases": membership_has_permission(membership, PURCHASES_MANAGE),
        "can_post_purchases": membership_has_permission(membership, PURCHASES_POST),
        "available_businesses": Business.objects.filter(
            memberships__user=user,
            memberships__is_active=True,
            is_active=True,
        ).distinct().order_by("name"),
    }
