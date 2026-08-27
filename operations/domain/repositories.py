from typing import Any, Protocol


class TradeDocumentRepository(Protocol):
    def post(self, *, document_id: int, business_id: int) -> Any:
        ...
