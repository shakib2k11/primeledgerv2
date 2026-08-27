from typing import Protocol


class AccountTemplateRepository(Protocol):
    def apply(self, *, template_id: int, business_id: int, user_id: int | None = None): ...
