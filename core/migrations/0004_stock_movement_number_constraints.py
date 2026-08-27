from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0003_annual_reference_sequence"),
        ("operations", "0002_automatic_reference_numbers"),
    ]

    operations = [
        migrations.AlterField(
            model_name="stockmovement",
            name="number",
            field=models.CharField(max_length=8),
        ),
        migrations.AddConstraint(
            model_name="stockmovement",
            constraint=models.UniqueConstraint(
                fields=("business", "number"),
                name="unique_business_stock_movement_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="stockmovement",
            constraint=models.CheckConstraint(
                condition=models.Q(number__regex=r"^[0-9]{8}$"),
                name="stock_movement_number_format",
            ),
        ),
    ]
