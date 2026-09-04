from django import forms


class OperationalAccountSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(
            name, value, label, selected, index, subindex=subindex, attrs=attrs
        )
        account = getattr(value, "instance", None)
        if account is not None:
            option["attrs"]["data-system-role"] = account.system_role
        return option


class OperationalAccountChoiceField(forms.ModelChoiceField):
    """A concise, business-facing label for operational payment accounts."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", OperationalAccountSelect)
        super().__init__(*args, **kwargs)

    def label_from_instance(self, account):
        role = account.get_system_role_display() if account.system_role else account.get_account_type_display()
        return f"{role} · {account.name} ({account.code})"
