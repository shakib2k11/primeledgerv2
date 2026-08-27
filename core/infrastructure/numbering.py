from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.db import transaction

from core.domain.numbering import MAX_ANNUAL_SEQUENCE, format_reference_number, reference_year
from core.models import AnnualReferenceSequence, Business


@transaction.atomic
def allocate_reference_number(*, business_id: int, occurred_on: date | datetime) -> str:
    """Allocate one tenant-wide annual number while holding the tenant row lock."""

    try:
        year = reference_year(occurred_on)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    Business.objects.select_for_update().only("pk").get(pk=business_id)
    sequence, _ = AnnualReferenceSequence.objects.get_or_create(
        business_id=business_id,
        year=year,
        defaults={"last_value": 0},
    )
    if sequence.last_value >= MAX_ANNUAL_SEQUENCE:
        raise ValidationError(f"The {year} automatic reference sequence is exhausted.")
    sequence.last_value += 1
    sequence.save(update_fields=["last_value"])
    return format_reference_number(year, sequence.last_value)
