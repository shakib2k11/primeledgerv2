from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from accounting.domain.policies import LIQUID_ACCOUNT_SYSTEM_ROLES
from accounting.models import (
    Account,
    AccountTemplateApplication,
    ChartOfAccountsTemplate,
    MoneyReceipt,
    Voucher,
)
from core.models import Business


@dataclass(frozen=True)
class TemplateApplicationResult:
    created: int
    matched: int


class DjangoAccountTemplateRepository:
    @transaction.atomic
    def apply(self, *, template_id: int, business_id: int, user_id: int | None = None):
        business = Business.objects.select_for_update().get(pk=business_id)
        template = (
            ChartOfAccountsTemplate.objects.select_for_update()
            .prefetch_related("lines")
            .get(pk=template_id, is_active=True)
        )
        created = 0
        matched = 0
        conflicts = []

        for line in template.lines.filter(is_active=True).order_by("code"):
            existing = Account.objects.filter(
                business=business,
                code__iexact=line.code,
            ).first()
            role_account = None
            if line.system_role:
                role_account = Account.objects.filter(
                    business=business,
                    system_role=line.system_role,
                ).first()
            if existing:
                if existing.account_type != line.account_type:
                    conflicts.append(
                        f"{line.code} is {existing.get_account_type_display()}, expected {line.get_account_type_display()}"
                    )
                    continue
                if line.system_role and role_account and role_account.pk != existing.pk:
                    conflicts.append(
                        f"{line.get_system_role_display()} is already assigned to {role_account.code}"
                    )
                    continue
                changed = []
                if line.system_role and not existing.system_role:
                    existing.system_role = line.system_role
                    changed.append("system_role")
                if changed:
                    existing.full_clean()
                    existing.save(update_fields=changed)
                matched += 1
                continue
            if role_account:
                matched += 1
                continue
            account = Account(
                business=business,
                code=line.code,
                name=line.name,
                account_type=line.account_type,
                system_role=line.system_role,
                is_system=True,
                is_active=line.account_is_active,
            )
            account.full_clean()
            account.save()
            created += 1

        if conflicts:
            raise ValidationError(
                "Template conflicts must be resolved first: " + "; ".join(conflicts)
            )
        AccountTemplateApplication.objects.create(
            business=business,
            template=template,
            applied_by_id=user_id,
            created_count=created,
            matched_count=matched,
        )
        return TemplateApplicationResult(created=created, matched=matched)


@dataclass(frozen=True)
class MoneyReceiptResult:
    receipt_id: int
    number: str
    created: bool


class DjangoMoneyReceiptRepository:
    @transaction.atomic
    def create_for_voucher(
        self,
        *,
        voucher_id: int,
        preferred_number: str,
        payment_account_id: int | None = None,
    ):
        voucher = (
            Voucher.objects.select_for_update()
            .select_related("business", "journal_entry")
            .get(pk=voucher_id)
        )
        existing = MoneyReceipt.objects.filter(voucher=voucher).first()
        if existing:
            return MoneyReceiptResult(existing.pk, existing.number, False)

        payment_account = None
        if payment_account_id:
            payment_account = Account.objects.filter(
                pk=payment_account_id,
                business=voucher.business,
                system_role__in=LIQUID_ACCOUNT_SYSTEM_ROLES,
            ).first()
        if payment_account is None:
            payment_account = (
                Account.objects.filter(
                    journal_lines__entry=voucher.journal_entry,
                    journal_lines__debit__gt=0,
                    business=voucher.business,
                    system_role__in=LIQUID_ACCOUNT_SYSTEM_ROLES,
                )
                .order_by("journal_lines__id")
                .first()
            )
        if voucher.voucher_type == Voucher.Type.SALE and payment_account is None:
            return None
        if voucher.voucher_type not in {Voucher.Type.SALE, Voucher.Type.RECEIPT}:
            return None

        Business.objects.select_for_update().get(pk=voucher.business_id)
        base_number = preferred_number.strip()[:40]
        candidate = base_number
        suffix = 2
        while MoneyReceipt.objects.filter(
            business=voucher.business,
            number=candidate,
        ).exists():
            marker = f"-{suffix}"
            candidate = f"{base_number[:40 - len(marker)]}{marker}"
            suffix += 1
        receipt = MoneyReceipt(
            business=voucher.business,
            number=candidate,
            voucher=voucher,
            party=voucher.party,
            payment_account=payment_account,
            amount=voucher.total,
            receipt_date=voucher.voucher_date,
        )
        receipt.full_clean()
        receipt.save()
        return MoneyReceiptResult(receipt.pk, receipt.number, True)
