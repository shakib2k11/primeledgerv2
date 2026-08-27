from dataclasses import dataclass
from typing import Any

from core.domain.repositories import BusinessReader, JournalRepository


CONTACTS_VIEW = "contacts.view"
CONTACTS_MANAGE = "contacts.manage"
INVENTORY_VIEW = "inventory.view"
INVENTORY_MANAGE = "inventory.manage"
ACCOUNTING_VIEW = "accounting.view"
ACCOUNTING_MANAGE = "accounting.manage"
ACCOUNTING_POST = "accounting.post"
SALES_VIEW = "sales.view"
SALES_MANAGE = "sales.manage"
SALES_POST = "sales.post"
PURCHASES_VIEW = "purchases.view"
PURCHASES_MANAGE = "purchases.manage"
PURCHASES_POST = "purchases.post"


class PermissionDenied(Exception):
    pass


def get_current_business(
    user: Any,
    business_reader: BusinessReader,
    business_id: int | None = None,
):
    """Resolve a tenant through a port; a supplied ID is selection, not authority."""

    if not getattr(user, "is_authenticated", False):
        return None
    return business_reader.for_user(user.pk, user.is_superuser, business_id)


def membership_has_permission(membership: Any, permission: str) -> bool:
    if membership is None or not membership.is_active:
        return False
    if membership.level == "business_admin":
        return True
    if not membership.role:
        return False
    granted = set(membership.role.permissions)
    if permission in granted:
        return True
    implied_by = {
        CONTACTS_VIEW: {CONTACTS_MANAGE},
        INVENTORY_VIEW: {INVENTORY_MANAGE},
        ACCOUNTING_VIEW: {ACCOUNTING_MANAGE, ACCOUNTING_POST},
        SALES_VIEW: {SALES_MANAGE, SALES_POST},
        PURCHASES_VIEW: {PURCHASES_MANAGE, PURCHASES_POST},
    }
    return bool(granted.intersection(implied_by.get(permission, set())))


def require_permission(user: Any, business: Any, permission: str) -> None:
    if user.is_superuser:
        return
    membership = next(
        (
            item
            for item in business.memberships.all()
            if item.user_id == user.pk and item.is_active
        ),
        None,
    )
    if not membership_has_permission(membership, permission):
        raise PermissionDenied(permission)


def inventory_unit_is_available(unit: Any, business: Any) -> bool:
    """Return whether a unit may be assigned to a product in this tenant."""

    if not unit or not getattr(unit, "is_active", False):
        return False
    if unit.business_id == business.pk:
        return True
    return unit.business_id is None and business.inherit_default_units


@dataclass(frozen=True)
class PostJournalCommand:
    entry_id: int
    business_id: int


def post_journal(command: PostJournalCommand, repository: JournalRepository):
    """Post through an atomic repository operation after delivery-layer auth."""

    return repository.post(entry_id=command.entry_id, business_id=command.business_id)
