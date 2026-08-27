import decimal

import django.core.validators
from django.db import migrations, models


def copy_existing_totals_to_subtotal(apps, schema_editor):
    TradeDocument = apps.get_model("operations", "TradeDocument")
    TradeDocument.objects.update(subtotal=models.F("total"))


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0002_automatic_reference_numbers"),
    ]

    operations = [
        migrations.AddField(
            model_name="tradedocument",
            name="subtotal",
            field=models.DecimalField(
                decimal_places=2,
                default=decimal.Decimal("0.00"),
                max_digits=14,
                validators=[django.core.validators.MinValueValidator(decimal.Decimal("0"))],
            ),
        ),
        migrations.AddField(
            model_name="tradedocument",
            name="discount_type",
            field=models.CharField(
                choices=[
                    ("none", "No discount"),
                    ("fixed", "Fixed amount"),
                    ("percentage", "Percentage"),
                ],
                default="none",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="tradedocument",
            name="discount_value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                default=decimal.Decimal("0.00"),
                max_digits=14,
                validators=[django.core.validators.MinValueValidator(decimal.Decimal("0"))],
            ),
        ),
        migrations.AddField(
            model_name="tradedocument",
            name="discount_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=decimal.Decimal("0.00"),
                max_digits=14,
                validators=[django.core.validators.MinValueValidator(decimal.Decimal("0"))],
            ),
        ),
        migrations.RunPython(
            copy_existing_totals_to_subtotal,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="tradedocument",
            constraint=models.CheckConstraint(
                condition=models.Q(("subtotal__gte", 0)),
                name="trade_document_nonnegative_subtotal",
            ),
        ),
        migrations.AddConstraint(
            model_name="tradedocument",
            constraint=models.CheckConstraint(
                condition=models.Q(("discount_value__gte", 0)),
                name="trade_document_nonnegative_discount_value",
            ),
        ),
        migrations.AddConstraint(
            model_name="tradedocument",
            constraint=models.CheckConstraint(
                condition=models.Q(("discount_amount__gte", 0)),
                name="trade_document_nonnegative_discount_amount",
            ),
        ),
        migrations.AddConstraint(
            model_name="tradedocument",
            constraint=models.CheckConstraint(
                condition=models.Q(("discount_amount__lte", models.F("subtotal"))),
                name="trade_document_discount_within_subtotal",
            ),
        ),
    ]
