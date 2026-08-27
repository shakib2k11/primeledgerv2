import django.db.models.deletion
from django.db import migrations, models
from django.utils.text import slugify


DEFAULT_UNITS = (
    ("piece", "Piece", "pc"),
    ("kilogram", "Kilogram", "kg"),
    ("gram", "Gram", "g"),
    ("litre", "Litre", "L"),
    ("millilitre", "Millilitre", "mL"),
    ("metre", "Metre", "m"),
    ("centimetre", "Centimetre", "cm"),
    ("box", "Box", "box"),
    ("pack", "Pack", "pack"),
    ("dozen", "Dozen", "doz"),
    ("pair", "Pair", "pair"),
    ("hour", "Hour", "hr"),
    ("day", "Day", "day"),
)

ALIASES = {
    "pc": "piece",
    "pcs": "piece",
    "pieces": "piece",
    "kg": "kilogram",
    "kilograms": "kilogram",
    "g": "gram",
    "grams": "gram",
    "l": "litre",
    "liter": "litre",
    "liters": "litre",
    "litres": "litre",
    "ml": "millilitre",
    "milliliter": "millilitre",
    "milliliters": "millilitre",
    "m": "metre",
    "meter": "metre",
    "meters": "metre",
    "metres": "metre",
    "cm": "centimetre",
    "centimeter": "centimetre",
    "centimeters": "centimetre",
    "boxes": "box",
    "packs": "pack",
    "pairs": "pair",
    "hours": "hour",
    "days": "day",
}


def populate_units(apps, schema_editor):
    InventoryUnit = apps.get_model("core", "InventoryUnit")
    Product = apps.get_model("core", "Product")
    defaults = {}
    for code, name, symbol in DEFAULT_UNITS:
        unit, _ = InventoryUnit.objects.get_or_create(
            business_id=None,
            code=code,
            defaults={"name": name, "symbol": symbol, "is_active": True},
        )
        defaults[code] = unit

    for product in Product.objects.all().iterator():
        original = (product.unit or "piece").strip()
        code = slugify(original).lower()[:30] or "piece"
        code = ALIASES.get(code, code)
        unit = defaults.get(code)
        if unit is None:
            unit, _ = InventoryUnit.objects.get_or_create(
                business_id=product.business_id,
                code=code,
                defaults={
                    "name": original[:80] or code.replace("-", " ").title(),
                    "symbol": original[:16] or code[:16],
                    "is_active": True,
                },
            )
        product.inventory_unit_id = unit.pk
        product.save(update_fields=["inventory_unit"])


def restore_text_units(apps, schema_editor):
    Product = apps.get_model("core", "Product")
    for product in Product.objects.select_related("inventory_unit").all().iterator():
        product.unit = product.inventory_unit.code if product.inventory_unit_id else "piece"
        product.save(update_fields=["unit"])


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="business",
            name="inherit_default_units",
            field=models.BooleanField(default=True),
        ),
        migrations.CreateModel(
            name="InventoryUnit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.SlugField(max_length=30)),
                ("name", models.CharField(max_length=80)),
                ("symbol", models.CharField(max_length=16)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "business",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="inventory_units",
                        to="core.business",
                    ),
                ),
            ],
            options={"ordering": ["name", "code"]},
        ),
        migrations.AddConstraint(
            model_name="inventoryunit",
            constraint=models.UniqueConstraint(
                condition=models.Q(("business__isnull", True)),
                fields=("code",),
                name="unique_global_inventory_unit_code",
            ),
        ),
        migrations.AddConstraint(
            model_name="inventoryunit",
            constraint=models.UniqueConstraint(
                condition=models.Q(("business__isnull", False)),
                fields=("business", "code"),
                name="unique_business_inventory_unit_code",
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="inventory_unit",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="migrating_products",
                to="core.inventoryunit",
            ),
        ),
        migrations.RunPython(populate_units, restore_text_units),
        migrations.RemoveField(model_name="product", name="unit"),
        migrations.RenameField(model_name="product", old_name="inventory_unit", new_name="unit"),
        migrations.AlterField(
            model_name="product",
            name="unit",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="products",
                to="core.inventoryunit",
            ),
        ),
    ]
