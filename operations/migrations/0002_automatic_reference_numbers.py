from django.db import migrations, models


def assign_reference_numbers(apps, schema_editor):
    AnnualReferenceSequence = apps.get_model("core", "AnnualReferenceSequence")
    StockMovement = apps.get_model("core", "StockMovement")
    TradeDocument = apps.get_model("operations", "TradeDocument")
    JournalEntry = apps.get_model("accounting", "JournalEntry")
    Voucher = apps.get_model("accounting", "Voucher")

    business_ids = set(TradeDocument.objects.values_list("business_id", flat=True))
    business_ids.update(StockMovement.objects.values_list("business_id", flat=True))
    number_changes = {}

    for business_id in sorted(business_ids):
        events_by_year = {}
        for document in TradeDocument.objects.filter(business_id=business_id):
            year = document.document_date.year
            if year < 2000 or year > 2099:
                raise RuntimeError("Automatic reference migration supports years from 2000 through 2099.")
            events_by_year.setdefault(year, []).append(
                (document.document_date, 0, document.pk, "document", document)
            )
        for movement in StockMovement.objects.filter(business_id=business_id):
            event_date = movement.occurred_at.date()
            year = event_date.year
            if year < 2000 or year > 2099:
                raise RuntimeError("Automatic reference migration supports years from 2000 through 2099.")
            events_by_year.setdefault(year, []).append(
                (event_date, 1, movement.pk, "movement", movement)
            )

        for year, events in sorted(events_by_year.items()):
            events.sort(key=lambda item: (item[0], item[1], item[2]))
            if len(events) > 999999:
                raise RuntimeError(f"The {year} reference sequence exceeds six digits.")
            for sequence, (_, _, _, record_type, record) in enumerate(events, start=1):
                new_number = f"{year % 100:02d}{sequence:06d}"
                if record_type == "document":
                    old_number = record.number
                    TradeDocument.objects.filter(pk=record.pk).update(number=new_number)
                    number_changes[(business_id, old_number)] = new_number
                    if record.journal_entry_id:
                        journal_reference = f"{record.kind.upper()}:{new_number}"
                        collision = JournalEntry.objects.filter(
                            business_id=business_id,
                            reference=journal_reference,
                        ).exclude(pk=record.journal_entry_id).exists()
                        if not collision:
                            JournalEntry.objects.filter(pk=record.journal_entry_id).update(
                                reference=journal_reference
                            )
                        voucher_prefix = "S" if record.kind == "sale" else "P"
                        voucher_number = f"{voucher_prefix}-{new_number}"
                        voucher_collision = Voucher.objects.filter(
                            business_id=business_id,
                            number=voucher_number,
                        ).exclude(journal_entry_id=record.journal_entry_id).exists()
                        if not voucher_collision:
                            Voucher.objects.filter(journal_entry_id=record.journal_entry_id).update(
                                number=voucher_number
                            )
                else:
                    StockMovement.objects.filter(pk=record.pk).update(number=new_number)
            AnnualReferenceSequence.objects.update_or_create(
                business_id=business_id,
                year=year,
                defaults={"last_value": len(events)},
            )

    for (business_id, old_number), new_number in number_changes.items():
        StockMovement.objects.filter(
            business_id=business_id,
            reference=old_number,
        ).update(reference=new_number)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_annual_reference_sequence"),
        ("operations", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(assign_reference_numbers, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="tradedocument",
            name="number",
            field=models.CharField(max_length=8),
        ),
        migrations.AddConstraint(
            model_name="tradedocument",
            constraint=models.CheckConstraint(
                condition=models.Q(number__regex=r"^[0-9]{8}$"),
                name="trade_document_number_format",
            ),
        ),
    ]
