from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from operations.domain.repositories import (
    PurchasePaymentRepository,
    SalePaymentRepository,
    TradeDocumentRepository,
)


@dataclass(frozen=True)
class PostTradeDocumentCommand:
    document_id: int
    business_id: int


def post_trade_document(
    command: PostTradeDocumentCommand, repository: TradeDocumentRepository
):
    return repository.post(
        document_id=command.document_id, business_id=command.business_id
    )


@dataclass(frozen=True)
class ReceiveSalePaymentCommand:
    sale_id: int
    business_id: int
    payment_account_id: int
    amount: Decimal
    payment_date: date
    idempotency_key: UUID
    notes: str = ""
    user_id: int | None = None


def receive_sale_payment(
    command: ReceiveSalePaymentCommand,
    repository: SalePaymentRepository,
):
    return repository.receive(
        sale_id=command.sale_id,
        business_id=command.business_id,
        payment_account_id=command.payment_account_id,
        amount=command.amount,
        payment_date=command.payment_date,
        idempotency_key=command.idempotency_key,
        notes=command.notes,
        user_id=command.user_id,
    )


@dataclass(frozen=True)
class PayPurchaseCommand:
    purchase_id: int
    business_id: int
    payment_account_id: int
    amount: Decimal
    payment_date: date
    idempotency_key: UUID
    notes: str = ""
    user_id: int | None = None


def pay_purchase(
    command: PayPurchaseCommand,
    repository: PurchasePaymentRepository,
):
    return repository.pay(
        purchase_id=command.purchase_id,
        business_id=command.business_id,
        payment_account_id=command.payment_account_id,
        amount=command.amount,
        payment_date=command.payment_date,
        idempotency_key=command.idempotency_key,
        notes=command.notes,
        user_id=command.user_id,
    )
