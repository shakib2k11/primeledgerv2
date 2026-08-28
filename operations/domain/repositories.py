from typing import Any, Protocol


class TradeDocumentRepository(Protocol):
    def post(self, *, document_id: int, business_id: int) -> Any:
        ...


class SalePaymentRepository(Protocol):
    def receive(
        self,
        *,
        sale_id: int,
        business_id: int,
        payment_account_id: int,
        amount: Any,
        payment_date: Any,
        idempotency_key: Any,
        notes: str = "",
        user_id: int | None = None,
    ) -> Any:
        ...


class PurchasePaymentRepository(Protocol):
    def pay(
        self,
        *,
        purchase_id: int,
        business_id: int,
        payment_account_id: int,
        amount: Any,
        payment_date: Any,
        idempotency_key: Any,
        notes: str = "",
        user_id: int | None = None,
    ) -> Any:
        ...


class BalanceSetoffRepository(Protocol):
    def create(
        self,
        *,
        business_id: int,
        party_id: int,
        setoff_date: Any,
        sale_allocations: Any,
        purchase_allocations: Any,
        idempotency_key: Any,
        notes: str = "",
        user_id: int | None = None,
    ) -> Any:
        ...
