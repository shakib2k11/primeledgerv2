from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0002_inventory_units")]

    operations = [
        migrations.CreateModel(
            name="AnnualReferenceSequence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("year", models.PositiveSmallIntegerField()),
                ("last_value", models.PositiveIntegerField(default=0)),
                (
                    "business",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="annual_reference_sequences",
                        to="core.business",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="annualreferencesequence",
            constraint=models.UniqueConstraint(
                fields=("business", "year"),
                name="unique_business_annual_reference_sequence",
            ),
        ),
        migrations.AddConstraint(
            model_name="annualreferencesequence",
            constraint=models.CheckConstraint(
                condition=models.Q(year__gte=2000, year__lte=2099),
                name="reference_sequence_supported_year",
            ),
        ),
        migrations.AddConstraint(
            model_name="annualreferencesequence",
            constraint=models.CheckConstraint(
                condition=models.Q(last_value__gte=0, last_value__lte=999999),
                name="reference_sequence_value_range",
            ),
        ),
        migrations.AddField(
            model_name="stockmovement",
            name="number",
            field=models.CharField(max_length=8, null=True),
        ),
    ]
