from dataclasses import dataclass

from accounting.domain.repositories import AccountTemplateRepository


@dataclass(frozen=True)
class ApplyAccountTemplateCommand:
    template_id: int
    business_id: int
    user_id: int | None = None


def apply_account_template(command: ApplyAccountTemplateCommand, repository: AccountTemplateRepository):
    return repository.apply(
        template_id=command.template_id,
        business_id=command.business_id,
        user_id=command.user_id,
    )
