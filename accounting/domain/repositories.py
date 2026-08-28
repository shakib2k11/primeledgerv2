from typing import Protocol


class AccountTemplateRepository(Protocol):
    def apply(self, *, template_id: int, business_id: int, user_id: int | None = None): ...


class MoneyReceiptRepository(Protocol):
    def create_for_voucher(
        self,
        *,
        voucher_id: int,
        preferred_number: str,
        payment_account_id: int | None = None,
    ): ...
