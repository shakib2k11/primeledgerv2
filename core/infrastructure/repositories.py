from django.core.exceptions import ValidationError
from django.db import transaction

from core.models import Business, Membership
from django.utils.translation import gettext_lazy as _


class DjangoBusinessReader:
    def for_user(
        self, user_id: int, is_superuser: bool, business_id: int | None = None
    ):
        businesses = Business.objects.filter(is_active=True)
        if business_id is not None:
            businesses = businesses.filter(pk=business_id)
        if is_superuser:
            return businesses.order_by("name", "pk").first()
        membership = (
            Membership.objects.select_related("business")
            .filter(
                user_id=user_id,
                is_active=True,
                business__is_active=True,
                **({"business_id": business_id} if business_id is not None else {}),
            )
            .order_by("business__name", "pk")
            .first()
        )
        return membership.business if membership else None


class DjangoJournalRepository:
    @transaction.atomic
    def post(self, *, entry_id: int, business_id: int):
        from accounting.models import FiscalPeriod, JournalEntry

        entry = (
            JournalEntry.objects.select_for_update()
            .select_related("period")
            .prefetch_related("lines__account", "lines__party")
            .filter(pk=entry_id, business_id=business_id)
            .first()
        )
        if entry is None:
            raise JournalEntry.DoesNotExist
        locked_period = FiscalPeriod.objects.select_for_update().get(pk=entry.period_id)
        entry.period = locked_period
        if entry.posted:
            return entry
        entry.validate_for_posting()
        updated = JournalEntry.objects.filter(pk=entry.pk, posted=False).update(posted=True)
        if not updated:
            raise ValidationError(_("This journal entry was already posted."))
        entry.posted = True
        return entry
