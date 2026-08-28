import decimal

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


LIQUID_ROLES = ("cash", "bank", "mobile_money")


def _available_number(MoneyReceipt, business_id, preferred):
    candidate = preferred[:40]
    suffix = 2
    while MoneyReceipt.objects.filter(
        business_id=business_id,
        number=candidate,
    ).exists():
        marker = f"-{suffix}"
        candidate = f"{preferred[:40 - len(marker)]}{marker}"
        suffix += 1
    return candidate


def seed_existing_money_receipts(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    JournalLine = apps.get_model("accounting", "JournalLine")
    MoneyReceipt = apps.get_model("accounting", "MoneyReceipt")
    Voucher = apps.get_model("accounting", "Voucher")
    TradeDocument = apps.get_model("operations", "TradeDocument")

    for voucher in Voucher.objects.filter(voucher_type="receipt").iterator():
        payment_account_id = (
            JournalLine.objects.filter(
                entry_id=voucher.journal_entry_id,
                debit__gt=0,
                account__system_role__in=LIQUID_ROLES,
            )
            .values_list("account_id", flat=True)
            .first()
        )
        MoneyReceipt.objects.get_or_create(
            voucher_id=voucher.pk,
            defaults={
                "business_id": voucher.business_id,
                "number": _available_number(
                    MoneyReceipt,
                    voucher.business_id,
                    voucher.number,
                ),
                "party_id": voucher.party_id,
                "payment_account_id": payment_account_id,
                "amount": voucher.total,
                "receipt_date": voucher.voucher_date,
            },
        )

    cash_sales = TradeDocument.objects.filter(
        kind="sale",
        status="posted",
        journal_entry_id__isnull=False,
        debit_account__system_role__in=LIQUID_ROLES,
    ).iterator()
    for document in cash_sales:
        voucher = Voucher.objects.filter(
            journal_entry_id=document.journal_entry_id,
            voucher_type="sale",
        ).first()
        if voucher is None:
            continue
        MoneyReceipt.objects.get_or_create(
            voucher_id=voucher.pk,
            defaults={
                "business_id": document.business_id,
                "number": _available_number(
                    MoneyReceipt,
                    document.business_id,
                    f"MR-{document.number}",
                ),
                "party_id": document.party_id,
                "payment_account_id": document.debit_account_id,
                "amount": voucher.total,
                "receipt_date": voucher.voucher_date,
            },
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounting", "0003_chart_templates_and_system_roles"),
        ("operations", "0003_sale_discounts"),
    ]

    operations = [
        migrations.CreateModel(
            name="MoneyReceipt",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("number", models.CharField(max_length=40)),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=14,
                        validators=[
                            django.core.validators.MinValueValidator(
                                decimal.Decimal("0.01")
                            )
                        ],
                    ),
                ),
                ("receipt_date", models.DateField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="money_receipts",
                        to="core.business",
                    ),
                ),
                (
                    "party",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="money_receipts",
                        to="core.party",
                    ),
                ),
                (
                    "payment_account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="money_receipts",
                        to="accounting.account",
                    ),
                ),
                (
                    "voucher",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="money_receipt",
                        to="accounting.voucher",
                    ),
                ),
            ],
            options={"ordering": ["-receipt_date", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="moneyreceipt",
            constraint=models.UniqueConstraint(
                fields=("business", "number"),
                name="unique_business_money_receipt_number",
            ),
        ),
        migrations.RunPython(
            seed_existing_money_receipts,
            migrations.RunPython.noop,
        ),
    ]
