from dataclasses import dataclass

from operations.domain.repositories import TradeDocumentRepository


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
