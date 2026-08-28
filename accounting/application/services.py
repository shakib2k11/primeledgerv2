from dataclasses import dataclass

from accounting.domain.repositories import AccountTemplateRepository, MoneyReceiptRepository


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


@dataclass(frozen=True)
class CreateMoneyReceiptCommand:
    voucher_id: int
    preferred_number: str
    payment_account_id: int | None = None


def create_money_receipt(
    command: CreateMoneyReceiptCommand,
    repository: MoneyReceiptRepository,
):
    return repository.create_for_voucher(
        voucher_id=command.voucher_id,
        preferred_number=command.preferred_number,
        payment_account_id=command.payment_account_id,
    )
