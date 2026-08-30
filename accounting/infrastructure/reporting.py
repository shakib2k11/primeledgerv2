from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q

from accounting.application.reporting import TransactionRegisterRow
from accounting.models import JournalEntry, Voucher


class DjangoTransactionRegisterReader:
    TYPE_LABELS = dict(Voucher.Type.choices)
    VALID_TYPES = set(TYPE_LABELS) | {"journal"}

    def read(
        self,
        *,
        business_id: int,
        date_from=None,
        date_to=None,
        query: str = "",
        transaction_type: str = "",
    ):
        entries = (
            JournalEntry.objects.filter(business_id=business_id, posted=True)
            .select_related("voucher__party")
            .prefetch_related("lines__party")
        )
        if date_from:
            entries = entries.filter(entry_date__gte=date_from)
        if date_to:
            entries = entries.filter(entry_date__lte=date_to)
        if transaction_type in self.TYPE_LABELS:
            entries = entries.filter(voucher__voucher_type=transaction_type)
        elif transaction_type == "journal":
            entries = entries.filter(voucher__isnull=True)
        if query:
            entries = entries.filter(
                Q(reference__icontains=query)
                | Q(description__icontains=query)
                | Q(voucher__number__icontains=query)
                | Q(voucher__party__name__icontains=query)
                | Q(lines__party__name__icontains=query)
            ).distinct()

        for entry in entries.order_by("-entry_date", "-id"):
            try:
                voucher = entry.voucher
            except ObjectDoesNotExist:
                voucher = None
            parties = []
            if voucher and voucher.party_id:
                parties = [voucher.party.name]
            else:
                parties = list(dict.fromkeys(
                    line.party.name
                    for line in entry.lines.all()
                    if line.party_id
                ))
            party_name = (
                parties[0] if len(parties) == 1
                else "Multiple parties" if parties
                else "—"
            )
            debit = entry.total_debit
            credit = entry.total_credit
            type_code = voucher.voucher_type if voucher else "journal"
            yield TransactionRegisterRow(
                journal_id=entry.pk,
                transaction_date=entry.entry_date,
                transaction_type=(
                    self.TYPE_LABELS.get(type_code, "Journal entry")
                ),
                transaction_type_code=type_code,
                number=voucher.number if voucher else entry.reference,
                journal_reference=entry.reference,
                description=entry.description,
                party_name=party_name,
                amount=voucher.total if voucher else debit,
                debit=debit or Decimal("0.00"),
                credit=credit or Decimal("0.00"),
            )
