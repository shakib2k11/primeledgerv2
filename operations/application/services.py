from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from operations.domain.repositories import (
    BalanceSetoffRepository,
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


@dataclass(frozen=True)
class SetoffAllocationCommand:
    document_id: int
    amount: Decimal


@dataclass(frozen=True)
class CreateBalanceSetoffCommand:
    business_id: int
    party_id: int
    setoff_date: date
    sale_allocations: tuple[SetoffAllocationCommand, ...]
    purchase_allocations: tuple[SetoffAllocationCommand, ...]
    idempotency_key: UUID
    notes: str = ""
    user_id: int | None = None


def create_balance_setoff(
    command: CreateBalanceSetoffCommand,
    repository: BalanceSetoffRepository,
):
    return repository.create(
        business_id=command.business_id,
        party_id=command.party_id,
        setoff_date=command.setoff_date,
        sale_allocations=command.sale_allocations,
        purchase_allocations=command.purchase_allocations,
        idempotency_key=command.idempotency_key,
        notes=command.notes,
        user_id=command.user_id,
    )
