from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0006_user_language_preference")]

    operations = [
        migrations.AlterField(
            model_name="party",
            name="kind",
            field=models.CharField(
                choices=[
                    ("customer", "Customer"),
                    ("supplier", "Supplier"),
                    ("both", "Customer and Supplier"),
                    ("employee", "Employee"),
                ],
                default="both",
                max_length=10,
            ),
        ),
    ]
