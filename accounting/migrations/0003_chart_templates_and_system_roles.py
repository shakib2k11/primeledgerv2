from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


DEFAULT_ACCOUNTS = (
    ("1010", "Cash in Hand", "asset", "cash", True),
    ("1020", "Bank Account", "asset", "bank", True),
    ("1030", "Mobile Financial Services", "asset", "mobile_money", True),
    ("1100", "Accounts Receivable", "asset", "accounts_receivable", True),
    ("1150", "Input VAT / Tax Receivable", "asset", "", False),
    ("1160", "Tax Deducted at Source Receivable", "asset", "", False),
    ("1200", "Inventory", "asset", "inventory", True),
    ("1300", "Advances to Suppliers", "asset", "", True),
    ("1400", "Prepaid Expenses", "asset", "", True),
    ("1500", "Property and Equipment", "asset", "", True),
    ("1590", "Accumulated Depreciation", "asset", "", True),
    ("2010", "Accounts Payable", "liability", "accounts_payable", True),
    ("2020", "Accrued Expenses", "liability", "", True),
    ("2030", "Salaries Payable", "liability", "", True),
    ("2040", "Customer Advances", "liability", "", True),
    ("2060", "Output VAT / Tax Payable", "liability", "", False),
    ("2070", "Tax Deducted at Source Payable", "liability", "", False),
    ("2100", "Short-term Loans", "liability", "", True),
    ("2200", "Long-term Loans", "liability", "", True),
    ("3010", "Owner's Capital", "equity", "owner_capital", True),
    ("3020", "Owner's Drawings", "equity", "", True),
    ("3030", "Retained Earnings", "equity", "retained_earnings", True),
    ("4010", "Product Sales", "income", "sales_revenue", True),
    ("4020", "Service Income", "income", "service_revenue", True),
    ("4030", "Sales Returns and Allowances", "income", "", True),
    ("4090", "Other Operating Income", "income", "", True),
    ("5010", "Cost of Goods Sold", "expense", "cost_of_goods_sold", True),
    ("5020", "Purchase Freight", "expense", "", True),
    ("5030", "Inventory Adjustment or Loss", "expense", "", True),
    ("6010", "Salaries and Wages", "expense", "", True),
    ("6020", "Rent", "expense", "", True),
    ("6030", "Utilities", "expense", "", True),
    ("6040", "Internet and Communication", "expense", "", True),
    ("6050", "Transport and Delivery", "expense", "", True),
    ("6060", "Office Supplies", "expense", "", True),
    ("6070", "Advertising and Marketing", "expense", "", True),
    ("6080", "Bank and Mobile Banking Charges", "expense", "", True),
    ("6090", "Depreciation Expense", "expense", "", True),
    ("6100", "Repairs and Maintenance", "expense", "", True),
    ("6110", "Professional Fees", "expense", "", True),
    ("6120", "Licences and Registration", "expense", "", True),
    ("6130", "Bad Debt Expense", "expense", "", True),
    ("6190", "Miscellaneous Expense", "expense", "", True),
)


def seed_default_chart(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    Template = apps.get_model("accounting", "ChartOfAccountsTemplate")
    Line = apps.get_model("accounting", "AccountTemplateLine")
    template, _ = Template.objects.get_or_create(
        name="Small Business — Trading & Services",
        defaults={
            "description": (
                "A concise Bangladesh-ready starting chart for trading and service businesses. "
                "VAT and tax-control accounts are copied inactive until configured."
            ),
            "is_default": True,
            "is_active": True,
        },
    )
    for code, name, account_type, system_role, account_is_active in DEFAULT_ACCOUNTS:
        Line.objects.get_or_create(
            template=template,
            code=code,
            defaults={
                "name": name,
                "account_type": account_type,
                "system_role": system_role,
                "account_is_active": account_is_active,
                "is_active": True,
            },
        )

    role_by_code = {
        code: role for code, _, _, role, _ in DEFAULT_ACCOUNTS if role
    }
    for account in Account.objects.filter(system_role="").iterator():
        role = role_by_code.get(account.code)
        if role and not Account.objects.filter(
            business_id=account.business_id,
            system_role=role,
        ).exists():
            Account.objects.filter(pk=account.pk).update(system_role=role)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("accounting", "0002_journalentry_unique_business_journal_reference_and_more"),
        ("core", "0004_stock_movement_number_constraints"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="system_role",
            field=models.CharField(
                blank=True,
                choices=[
                    ("cash", "Cash"),
                    ("bank", "Bank"),
                    ("mobile_money", "Mobile financial services"),
                    ("accounts_receivable", "Accounts receivable"),
                    ("inventory", "Inventory"),
                    ("accounts_payable", "Accounts payable"),
                    ("owner_capital", "Owner capital"),
                    ("retained_earnings", "Retained earnings"),
                    ("sales_revenue", "Sales revenue"),
                    ("service_revenue", "Service revenue"),
                    ("cost_of_goods_sold", "Cost of goods sold"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.UniqueConstraint(
                condition=models.Q(("system_role", ""), _negated=True),
                fields=("business", "system_role"),
                name="unique_business_account_system_role",
            ),
        ),
        migrations.CreateModel(
            name="ChartOfAccountsTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=120, unique=True)),
                ("description", models.TextField(blank=True)),
                ("is_default", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["name"]},
        ),
        migrations.CreateModel(
            name="AccountTemplateLine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=20)),
                ("name", models.CharField(max_length=120)),
                ("account_type", models.CharField(choices=[("asset", "Asset"), ("liability", "Liability"), ("equity", "Equity"), ("income", "Income"), ("expense", "Expense")], max_length=10)),
                ("system_role", models.CharField(blank=True, choices=[("cash", "Cash"), ("bank", "Bank"), ("mobile_money", "Mobile financial services"), ("accounts_receivable", "Accounts receivable"), ("inventory", "Inventory"), ("accounts_payable", "Accounts payable"), ("owner_capital", "Owner capital"), ("retained_earnings", "Retained earnings"), ("sales_revenue", "Sales revenue"), ("service_revenue", "Service revenue"), ("cost_of_goods_sold", "Cost of goods sold")], max_length=32)),
                ("account_is_active", models.BooleanField(default=True)),
                ("is_active", models.BooleanField(default=True)),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lines", to="accounting.chartofaccountstemplate")),
            ],
            options={"ordering": ["code"]},
        ),
        migrations.AddConstraint(
            model_name="accounttemplateline",
            constraint=models.UniqueConstraint(fields=("template", "code"), name="unique_template_account_code"),
        ),
        migrations.AddConstraint(
            model_name="accounttemplateline",
            constraint=models.UniqueConstraint(condition=models.Q(("system_role", ""), _negated=True), fields=("template", "system_role"), name="unique_template_account_system_role"),
        ),
        migrations.CreateModel(
            name="AccountTemplateApplication",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_count", models.PositiveIntegerField(default=0)),
                ("matched_count", models.PositiveIntegerField(default=0)),
                ("applied_at", models.DateTimeField(auto_now_add=True)),
                ("applied_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="account_template_applications", to="core.business")),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="applications", to="accounting.chartofaccountstemplate")),
            ],
            options={"ordering": ["-applied_at", "-id"]},
        ),
        migrations.RunPython(seed_default_chart, migrations.RunPython.noop),
    ]
