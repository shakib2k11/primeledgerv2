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


class ExpenseRepository(Protocol):
    def create(self, **kwargs): ...


class ExpensePaymentRepository(Protocol):
    def pay(self, **kwargs): ...


class TransactionRegisterReader(Protocol):
    def read(
        self,
        *,
        business_id: int,
        date_from=None,
        date_to=None,
        query: str = "",
        transaction_type: str = "",
    ): ...
