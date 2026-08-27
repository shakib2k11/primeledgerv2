from typing import Any, Protocol


class BusinessReader(Protocol):
    """Port used to resolve an authenticated user's tenant context."""

    def for_user(
        self, user_id: int, is_superuser: bool, business_id: int | None = None
    ) -> Any | None:
        ...


class JournalRepository(Protocol):
    """Persistence port for the atomic journal-posting use case."""

    def post(self, *, entry_id: int, business_id: int) -> Any:
        ...
