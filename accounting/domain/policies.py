SYSTEM_ROLE_ACCOUNT_TYPES = {
    "cash": "asset",
    "bank": "asset",
    "mobile_money": "asset",
    "accounts_receivable": "asset",
    "inventory": "asset",
    "accounts_payable": "liability",
    "owner_capital": "equity",
    "retained_earnings": "equity",
    "sales_revenue": "income",
    "service_revenue": "income",
    "cost_of_goods_sold": "expense",
}

LIQUID_ACCOUNT_SYSTEM_ROLES = frozenset({"cash", "bank", "mobile_money"})
