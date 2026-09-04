from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounting.application.services import (
    CreateMoneyReceiptCommand,
    create_money_receipt,
)
from accounting.infrastructure.repositories import DjangoMoneyReceiptRepository
from accounting.models import (
    Account,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    MoneyReceipt,
    Voucher,
)
from core.models import Party, Product, StockMovement
from core.infrastructure.numbering import allocate_reference_number
from operations.models import (
    BalanceSetoff,
    PurchasePayment,
    PurchaseSetoffAllocation,
    SalePayment,
    SaleSetoffAllocation,
    TradeDocument,
)
from django.utils.translation import gettext_lazy as _


class DjangoTradeDocumentRepository:
    @transaction.atomic
    def post(self, *, document_id: int, business_id: int):
        document = (
            TradeDocument.objects.select_for_update()
            .select_related(
                "business", "party", "period", "debit_account", "credit_account"
            )
            .prefetch_related("lines__product")
            .filter(pk=document_id, business_id=business_id)
            .first()
        )
        if document is None:
            raise TradeDocument.DoesNotExist
        if document.status == TradeDocument.Status.POSTED:
            return document

        document.period = FiscalPeriod.objects.select_for_update().get(pk=document.period_id)
        lines = list(document.lines.all())
        if not lines:
            raise ValidationError(_("A sale or purchase requires at least one line."))
        document.full_clean()
        for line in lines:
            line.full_clean()

        product_ids = {line.product_id for line in lines if not line.product.is_service}
        if product_ids:
            list(Product.objects.select_for_update().filter(pk__in=product_ids))
        cost_by_product = {}
        cogs_total = Decimal("0.00")
        if document.kind == TradeDocument.Kind.SALE:
            required = defaultdict(lambda: Decimal("0"))
            for line in lines:
                if not line.product.is_service:
                    required[line.product_id] += line.quantity
            for product_id, quantity in required.items():
                on_hand = Decimal("0")
                stock_value = Decimal("0.00")
                movements = StockMovement.objects.filter(
                    business_id=business_id,
                    product_id=product_id,
                ).order_by("occurred_at", "id")
                for movement in movements:
                    movement_value = movement.quantity * movement.unit_cost
                    if movement.direction == StockMovement.Direction.IN:
                        on_hand += movement.quantity
                        stock_value += movement_value
                    else:
                        on_hand -= movement.quantity
                        stock_value -= movement_value
                if on_hand < quantity:
                    product = next(line.product for line in lines if line.product_id == product_id)
                    raise ValidationError(
                        _("Insufficient stock for %(product)s.") % {"product": product.name}
                    )
                average_cost = (
                    stock_value / on_hand if on_hand > 0 else Decimal("0.00")
                ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                cost_by_product[product_id] = average_cost
                cogs_total += (quantity * average_cost).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

        subtotal = sum((line.line_total for line in lines), Decimal("0.00"))
        document.set_totals(subtotal)
        document.full_clean()
        total = document.total
        if total <= 0:
            raise ValidationError(_("A sale or purchase total must be greater than zero."))
        journal = JournalEntry.objects.create(
            business=document.business,
            period=document.period,
            reference=f"{document.kind.upper()}:{document.number}"[:80],
            description=f"{document.get_kind_display()} {document.number} — {document.party.name}",
            entry_date=document.document_date,
            created_by=document.created_by,
        )
        JournalLine.objects.create(
            entry=journal,
            account=document.debit_account,
            party=document.party,
            description=document.get_kind_display(),
            debit=total,
        )
        JournalLine.objects.create(
            entry=journal,
            account=document.credit_account,
            party=document.party,
            description=document.get_kind_display(),
            credit=total,
        )
        if cogs_total > 0:
            mapped_accounts = {
                account.system_role: account
                for account in Account.objects.filter(
                    business_id=business_id,
                    is_active=True,
                    system_role__in=[
                        Account.SystemRole.INVENTORY,
                        Account.SystemRole.COST_OF_GOODS_SOLD,
                    ],
                )
            }
            inventory_account = mapped_accounts.get(Account.SystemRole.INVENTORY)
            cogs_account = mapped_accounts.get(Account.SystemRole.COST_OF_GOODS_SOLD)
            if not inventory_account or not cogs_account:
                raise ValidationError(
                    _("Product sales require active Inventory and Cost of Goods Sold posting roles. Apply the default chart template or map these accounts.")
                )
            JournalLine.objects.create(
                entry=journal,
                account=cogs_account,
                description="Cost of goods sold",
                debit=cogs_total,
            )
            JournalLine.objects.create(
                entry=journal,
                account=inventory_account,
                description="Inventory issued",
                credit=cogs_total,
            )
        journal.validate_for_posting()
        JournalEntry.objects.filter(pk=journal.pk).update(posted=True)
        journal.posted = True

        occurred_at = timezone.make_aware(
            datetime.combine(document.document_date, time(hour=12))
        )
        direction = (
            StockMovement.Direction.OUT
            if document.kind == TradeDocument.Kind.SALE
            else StockMovement.Direction.IN
        )
        for line in lines:
            if line.product.is_service:
                continue
            movement = StockMovement(
                business=document.business,
                number=allocate_reference_number(
                    business_id=document.business_id,
                    occurred_on=document.document_date,
                ),
                product=line.product,
                direction=direction,
                quantity=line.quantity,
                unit_cost=(cost_by_product[line.product_id] if direction == StockMovement.Direction.OUT else line.unit_price),
                reference=document.number,
                occurred_at=occurred_at,
                created_by=document.created_by,
            )
            movement.full_clean()
            movement.save()

        voucher_prefix = "S" if document.kind == TradeDocument.Kind.SALE else "P"
        voucher = Voucher(
            business=document.business,
            voucher_type=(Voucher.Type.SALE if document.kind == TradeDocument.Kind.SALE else Voucher.Type.PURCHASE),
            number=f"{voucher_prefix}-{document.number}"[:40],
            party=document.party,
            journal_entry=journal,
            total=total,
            notes=document.notes,
            voucher_date=document.document_date,
        )
        voucher.full_clean()
        voucher.save()
        if document.kind == TradeDocument.Kind.SALE:
            create_money_receipt(
                CreateMoneyReceiptCommand(
                    voucher_id=voucher.pk,
                    preferred_number=f"MR-{document.number}",
                    payment_account_id=document.debit_account_id,
                ),
                DjangoMoneyReceiptRepository(),
            )

        updated = TradeDocument.objects.filter(
            pk=document.pk, status=TradeDocument.Status.DRAFT
        ).update(
            status=TradeDocument.Status.POSTED,
            subtotal=document.subtotal,
            discount_amount=document.discount_amount,
            total=total,
            journal_entry=journal,
            posted_at=timezone.now(),
        )
        if not updated:
            raise ValidationError(_("This document was already posted."))
        document.status = TradeDocument.Status.POSTED
        document.subtotal = subtotal
        document.total = total
        document.journal_entry = journal
        document.posted_at = timezone.now()
        return document


class DjangoSalePaymentRepository:
    @staticmethod
    def _validate_idempotent(existing, *, sale_id, payment_account_id, amount, payment_date):
        if (
            existing.sale_id != sale_id
            or existing.payment_account_id != payment_account_id
            or existing.amount != amount
            or existing.payment_date != payment_date
        ):
            raise ValidationError(
                _("This payment request key was already used with different details.")
            )
        return existing

    @transaction.atomic
    def receive(
        self,
        *,
        sale_id: int,
        business_id: int,
        payment_account_id: int,
        amount,
        payment_date,
        idempotency_key,
        notes: str = "",
        user_id: int | None = None,
    ):
        amount = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        existing = SalePayment.objects.filter(
            business_id=business_id,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            return self._validate_idempotent(
                existing,
                sale_id=sale_id,
                payment_account_id=payment_account_id,
                amount=amount,
                payment_date=payment_date,
            )

        sale = (
            TradeDocument.objects.select_for_update()
            .select_related("business", "party", "debit_account")
            .filter(
                pk=sale_id,
                business_id=business_id,
                kind=TradeDocument.Kind.SALE,
            )
            .first()
        )
        if sale is None:
            raise TradeDocument.DoesNotExist

        existing = SalePayment.objects.filter(
            business_id=business_id,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            return self._validate_idempotent(
                existing,
                sale_id=sale_id,
                payment_account_id=payment_account_id,
                amount=amount,
                payment_date=payment_date,
            )
        if sale.status != TradeDocument.Status.POSTED:
            raise ValidationError(_("Payment can be received only against a posted sale."))
        if (
            sale.debit_account.system_role
            != Account.SystemRole.ACCOUNTS_RECEIVABLE
        ):
            raise ValidationError(
                _("This sale was not posted to Accounts Receivable and cannot receive an allocated payment.")
            )

        payment_account = Account.objects.select_for_update().filter(
            pk=payment_account_id,
            business_id=business_id,
            is_active=True,
            system_role__in=[
                Account.SystemRole.CASH,
                Account.SystemRole.BANK,
                Account.SystemRole.MOBILE_MONEY,
            ],
        ).first()
        if payment_account is None:
            raise ValidationError(
                _("Select an active account mapped as Cash, Bank, or Mobile Financial Services.")
            )

        period = FiscalPeriod.objects.select_for_update().filter(
            business_id=business_id,
            starts_on__lte=payment_date,
            ends_on__gte=payment_date,
        ).first()
        if period is None:
            raise ValidationError(_("No fiscal period covers the payment date."))
        if period.is_locked:
            raise ValidationError(_("The fiscal period covering the payment date is locked."))

        paid = SalePayment.objects.filter(sale=sale).aggregate(total=Sum("amount"))[
            "total"
        ] or Decimal("0.00")
        paid += SaleSetoffAllocation.objects.filter(sale=sale).aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0.00")
        remaining = (sale.total - paid).quantize(Decimal("0.01"))
        if amount <= 0:
            raise ValidationError(_("Payment amount must be greater than zero."))
        if remaining <= 0:
            raise ValidationError(_("This invoice is already paid in full."))
        if amount > remaining:
            raise ValidationError(
                _("Payment cannot exceed the remaining balance of %(balance).2f.")
                % {"balance": remaining}
            )

        number = allocate_reference_number(
            business_id=business_id,
            occurred_on=payment_date,
        )
        journal = JournalEntry.objects.create(
            business=sale.business,
            period=period,
            reference=f"RECEIPT:{number}",
            description=f"Payment for sale {sale.number} — {sale.party.name}",
            entry_date=payment_date,
            created_by_id=user_id,
        )
        JournalLine.objects.create(
            entry=journal,
            account=payment_account,
            party=sale.party,
            description=f"Payment received for {sale.number}",
            debit=amount,
        )
        JournalLine.objects.create(
            entry=journal,
            account=sale.debit_account,
            party=sale.party,
            description=f"Receivable settled for {sale.number}",
            credit=amount,
        )
        journal.validate_for_posting()
        JournalEntry.objects.filter(pk=journal.pk).update(posted=True)
        journal.posted = True

        voucher_base = f"R-{number}"
        voucher_number = voucher_base
        suffix = 2
        while Voucher.objects.filter(
            business_id=business_id,
            number=voucher_number,
        ).exists():
            marker = f"-{suffix}"
            voucher_number = f"{voucher_base[:40 - len(marker)]}{marker}"
            suffix += 1
        voucher = Voucher(
            business=sale.business,
            voucher_type=Voucher.Type.RECEIPT,
            number=voucher_number,
            party=sale.party,
            journal_entry=journal,
            total=amount,
            notes=notes,
            voucher_date=payment_date,
        )
        voucher.full_clean()
        voucher.save()
        receipt_result = create_money_receipt(
            CreateMoneyReceiptCommand(
                voucher_id=voucher.pk,
                preferred_number=f"MR-{number}",
                payment_account_id=payment_account.pk,
            ),
            DjangoMoneyReceiptRepository(),
        )
        if receipt_result is None:
            raise ValidationError(_("The money receipt could not be generated."))
        receipt = MoneyReceipt.objects.get(pk=receipt_result.receipt_id)
        payment = SalePayment(
            business=sale.business,
            sale=sale,
            number=number,
            payment_account=payment_account,
            amount=amount,
            payment_date=payment_date,
            journal_entry=journal,
            money_receipt=receipt,
            notes=notes,
            idempotency_key=idempotency_key,
            received_by_id=user_id,
        )
        payment.full_clean()
        payment.save()
        return payment


class DjangoPurchasePaymentRepository:
    @staticmethod
    def _validate_idempotent(
        existing,
        *,
        purchase_id,
        payment_account_id,
        amount,
        payment_date,
    ):
        if (
            existing.purchase_id != purchase_id
            or existing.payment_account_id != payment_account_id
            or existing.amount != amount
            or existing.payment_date != payment_date
        ):
            raise ValidationError(
                _("This payment request key was already used with different details.")
            )
        return existing

    @transaction.atomic
    def pay(
        self,
        *,
        purchase_id: int,
        business_id: int,
        payment_account_id: int,
        amount,
        payment_date,
        idempotency_key,
        notes: str = "",
        user_id: int | None = None,
    ):
        amount = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        existing = PurchasePayment.objects.filter(
            business_id=business_id,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            return self._validate_idempotent(
                existing,
                purchase_id=purchase_id,
                payment_account_id=payment_account_id,
                amount=amount,
                payment_date=payment_date,
            )

        purchase = (
            TradeDocument.objects.select_for_update()
            .select_related("business", "party", "credit_account")
            .filter(
                pk=purchase_id,
                business_id=business_id,
                kind=TradeDocument.Kind.PURCHASE,
            )
            .first()
        )
        if purchase is None:
            raise TradeDocument.DoesNotExist

        existing = PurchasePayment.objects.filter(
            business_id=business_id,
            idempotency_key=idempotency_key,
        ).first()
        if existing:
            return self._validate_idempotent(
                existing,
                purchase_id=purchase_id,
                payment_account_id=payment_account_id,
                amount=amount,
                payment_date=payment_date,
            )
        if purchase.status != TradeDocument.Status.POSTED:
            raise ValidationError(_("Payment can be made only against a posted purchase."))
        if (
            purchase.credit_account.system_role
            != Account.SystemRole.ACCOUNTS_PAYABLE
        ):
            raise ValidationError(
                _("This purchase was not posted to Accounts Payable and cannot receive an allocated payment.")
            )

        payment_account = Account.objects.select_for_update().filter(
            pk=payment_account_id,
            business_id=business_id,
            is_active=True,
            system_role__in=[
                Account.SystemRole.CASH,
                Account.SystemRole.BANK,
                Account.SystemRole.MOBILE_MONEY,
            ],
        ).first()
        if payment_account is None:
            raise ValidationError(
                _("Select an active account mapped as Cash, Bank, or Mobile Financial Services.")
            )

        period = FiscalPeriod.objects.select_for_update().filter(
            business_id=business_id,
            starts_on__lte=payment_date,
            ends_on__gte=payment_date,
        ).first()
        if period is None:
            raise ValidationError(_("No fiscal period covers the payment date."))
        if period.is_locked:
            raise ValidationError(_("The fiscal period covering the payment date is locked."))

        paid = PurchasePayment.objects.filter(purchase=purchase).aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0.00")
        paid += PurchaseSetoffAllocation.objects.filter(purchase=purchase).aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0.00")
        remaining = (purchase.total - paid).quantize(Decimal("0.01"))
        if amount <= 0:
            raise ValidationError(_("Payment amount must be greater than zero."))
        if remaining <= 0:
            raise ValidationError(_("This supplier invoice is already paid in full."))
        if amount > remaining:
            raise ValidationError(
                _("Payment cannot exceed the remaining balance of %(balance).2f.")
                % {"balance": remaining}
            )

        number = allocate_reference_number(
            business_id=business_id,
            occurred_on=payment_date,
        )
        journal = JournalEntry.objects.create(
            business=purchase.business,
            period=period,
            reference=f"PAYMENT:{number}",
            description=f"Payment for purchase {purchase.number} — {purchase.party.name}",
            entry_date=payment_date,
            created_by_id=user_id,
        )
        JournalLine.objects.create(
            entry=journal,
            account=purchase.credit_account,
            party=purchase.party,
            description=f"Payable settled for {purchase.number}",
            debit=amount,
        )
        JournalLine.objects.create(
            entry=journal,
            account=payment_account,
            party=purchase.party,
            description=f"Supplier payment for {purchase.number}",
            credit=amount,
        )
        journal.validate_for_posting()
        JournalEntry.objects.filter(pk=journal.pk).update(posted=True)
        journal.posted = True

        voucher_base = f"P-{number}"
        voucher_number = voucher_base
        suffix = 2
        while Voucher.objects.filter(
            business_id=business_id,
            number=voucher_number,
        ).exists():
            marker = f"-{suffix}"
            voucher_number = f"{voucher_base[:40 - len(marker)]}{marker}"
            suffix += 1
        voucher = Voucher(
            business=purchase.business,
            voucher_type=Voucher.Type.PAYMENT,
            number=voucher_number,
            party=purchase.party,
            journal_entry=journal,
            total=amount,
            notes=notes,
            voucher_date=payment_date,
        )
        voucher.full_clean()
        voucher.save()
        payment = PurchasePayment(
            business=purchase.business,
            purchase=purchase,
            number=number,
            payment_account=payment_account,
            amount=amount,
            payment_date=payment_date,
            journal_entry=journal,
            voucher=voucher,
            notes=notes,
            idempotency_key=idempotency_key,
            paid_by_id=user_id,
        )
        payment.full_clean()
        payment.save()
        return payment


class DjangoBalanceSetoffRepository:
    @staticmethod
    def _normalized(allocations):
        return tuple(sorted(
            (
                int(allocation.document_id),
                Decimal(allocation.amount).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                ),
            )
            for allocation in allocations
        ))

    def _validate_idempotent(
        self,
        existing,
        *,
        party_id,
        setoff_date,
        sale_allocations,
        purchase_allocations,
    ):
        existing_sales = tuple(sorted(
            (item.sale_id, item.amount)
            for item in existing.sale_allocations.all()
        ))
        existing_purchases = tuple(sorted(
            (item.purchase_id, item.amount)
            for item in existing.purchase_allocations.all()
        ))
        if (
            existing.party_id != party_id
            or existing.setoff_date != setoff_date
            or existing_sales != self._normalized(sale_allocations)
            or existing_purchases != self._normalized(purchase_allocations)
        ):
            raise ValidationError(
                _("This set-off request key was already used with different details.")
            )
        return existing

    @transaction.atomic
    def create(
        self,
        *,
        business_id: int,
        party_id: int,
        setoff_date,
        sale_allocations,
        purchase_allocations,
        idempotency_key,
        notes: str = "",
        user_id: int | None = None,
    ):
        sale_allocations = tuple(sale_allocations)
        purchase_allocations = tuple(purchase_allocations)
        existing = BalanceSetoff.objects.filter(
            business_id=business_id,
            idempotency_key=idempotency_key,
        ).prefetch_related("sale_allocations", "purchase_allocations").first()
        if existing:
            return self._validate_idempotent(
                existing,
                party_id=party_id,
                setoff_date=setoff_date,
                sale_allocations=sale_allocations,
                purchase_allocations=purchase_allocations,
            )
        if setoff_date > timezone.localdate():
            raise ValidationError(_("Set-off date cannot be in the future."))

        party = Party.objects.select_for_update().filter(
            pk=party_id,
            business_id=business_id,
            is_active=True,
            kind=Party.Kind.BOTH,
        ).first()
        if party is None:
            raise ValidationError(
                _("Select an active contact classified as Customer and Supplier.")
            )

        existing = BalanceSetoff.objects.filter(
            business_id=business_id,
            idempotency_key=idempotency_key,
        ).prefetch_related("sale_allocations", "purchase_allocations").first()
        if existing:
            return self._validate_idempotent(
                existing,
                party_id=party_id,
                setoff_date=setoff_date,
                sale_allocations=sale_allocations,
                purchase_allocations=purchase_allocations,
            )

        normalized_sales = self._normalized(sale_allocations)
        normalized_purchases = self._normalized(purchase_allocations)
        if not normalized_sales or not normalized_purchases:
            raise ValidationError(
                _("Select at least one receivable invoice and one payable purchase.")
            )
        if (
            len({document_id for document_id, _ in normalized_sales})
            != len(normalized_sales)
            or len({document_id for document_id, _ in normalized_purchases})
            != len(normalized_purchases)
        ):
            raise ValidationError(_("Each document may be allocated only once per set-off."))
        if any(amount <= 0 for _, amount in normalized_sales + normalized_purchases):
            raise ValidationError(_("Every set-off allocation must be greater than zero."))
        sale_total = sum((amount for _, amount in normalized_sales), Decimal("0.00"))
        purchase_total = sum(
            (amount for _, amount in normalized_purchases),
            Decimal("0.00"),
        )
        if sale_total != purchase_total:
            raise ValidationError(
                _("Receivable and payable allocation totals must be equal.")
            )

        period = FiscalPeriod.objects.select_for_update().filter(
            business_id=business_id,
            starts_on__lte=setoff_date,
            ends_on__gte=setoff_date,
        ).first()
        if period is None:
            raise ValidationError(_("No fiscal period covers the set-off date."))
        if period.is_locked:
            raise ValidationError(_("The fiscal period covering the set-off date is locked."))

        accounts = {
            account.system_role: account
            for account in Account.objects.select_for_update().filter(
                business_id=business_id,
                is_active=True,
                system_role__in=[
                    Account.SystemRole.ACCOUNTS_RECEIVABLE,
                    Account.SystemRole.ACCOUNTS_PAYABLE,
                ],
            )
        }
        receivable_account = accounts.get(Account.SystemRole.ACCOUNTS_RECEIVABLE)
        payable_account = accounts.get(Account.SystemRole.ACCOUNTS_PAYABLE)
        if not receivable_account or not payable_account:
            raise ValidationError(
                _("Active Accounts Receivable and Accounts Payable posting accounts are required.")
            )

        sale_amounts = dict(normalized_sales)
        purchase_amounts = dict(normalized_purchases)
        sales = list(
            TradeDocument.objects.select_for_update()
            .filter(
                pk__in=sale_amounts,
                business_id=business_id,
                party_id=party_id,
                kind=TradeDocument.Kind.SALE,
                status=TradeDocument.Status.POSTED,
                debit_account=receivable_account,
            )
            .prefetch_related("payments", "sale_setoff_allocations")
        )
        purchases = list(
            TradeDocument.objects.select_for_update()
            .filter(
                pk__in=purchase_amounts,
                business_id=business_id,
                party_id=party_id,
                kind=TradeDocument.Kind.PURCHASE,
                status=TradeDocument.Status.POSTED,
                credit_account=payable_account,
            )
            .prefetch_related("supplier_payments", "purchase_setoff_allocations")
        )
        if len(sales) != len(sale_amounts):
            raise ValidationError(
                _("Every receivable allocation must reference an open posted invoice for this contact.")
            )
        if len(purchases) != len(purchase_amounts):
            raise ValidationError(
                _("Every payable allocation must reference an open posted purchase for this contact.")
            )
        for document in sales:
            if document.document_date > setoff_date:
                raise ValidationError(
                    _("Set-off date cannot precede sale %(number)s.")
                    % {"number": document.number}
                )
            if sale_amounts[document.pk] > document.balance_due:
                raise ValidationError(
                    _("Allocation cannot exceed sale %(number)s's balance of %(balance).2f.")
                    % {"number": document.number, "balance": document.balance_due}
                )
        for document in purchases:
            if document.document_date > setoff_date:
                raise ValidationError(
                    _("Set-off date cannot precede purchase %(number)s.")
                    % {"number": document.number}
                )
            if purchase_amounts[document.pk] > document.balance_due:
                raise ValidationError(
                    _("Allocation cannot exceed purchase %(number)s's balance of %(balance).2f.")
                    % {"number": document.number, "balance": document.balance_due}
                )

        number = allocate_reference_number(
            business_id=business_id,
            occurred_on=setoff_date,
        )
        journal = JournalEntry.objects.create(
            business=party.business,
            period=period,
            reference=f"CONTRA:{number}",
            description=f"Receivable/payable set-off — {party.name}",
            entry_date=setoff_date,
            created_by_id=user_id,
        )
        JournalLine.objects.create(
            entry=journal,
            account=payable_account,
            party=party,
            description="Supplier balance set off",
            debit=sale_total,
        )
        JournalLine.objects.create(
            entry=journal,
            account=receivable_account,
            party=party,
            description="Customer balance set off",
            credit=sale_total,
        )
        journal.validate_for_posting()
        JournalEntry.objects.filter(pk=journal.pk).update(posted=True)
        journal.posted = True

        voucher_base = f"C-{number}"
        voucher_number = voucher_base
        suffix = 2
        while Voucher.objects.filter(
            business_id=business_id,
            number=voucher_number,
        ).exists():
            marker = f"-{suffix}"
            voucher_number = f"{voucher_base[:40 - len(marker)]}{marker}"
            suffix += 1
        voucher = Voucher(
            business=party.business,
            voucher_type=Voucher.Type.CONTRA,
            number=voucher_number,
            party=party,
            journal_entry=journal,
            total=sale_total,
            notes=notes,
            voucher_date=setoff_date,
        )
        voucher.full_clean()
        voucher.save()
        setoff = BalanceSetoff(
            business=party.business,
            party=party,
            number=number,
            setoff_date=setoff_date,
            total_amount=sale_total,
            journal_entry=journal,
            voucher=voucher,
            notes=notes,
            idempotency_key=idempotency_key,
            created_by_id=user_id,
        )
        setoff.full_clean()
        setoff.save()
        for sale in sales:
            allocation = SaleSetoffAllocation(
                setoff=setoff,
                sale=sale,
                amount=sale_amounts[sale.pk],
            )
            allocation.full_clean()
            allocation.save()
        for purchase in purchases:
            allocation = PurchaseSetoffAllocation(
                setoff=setoff,
                purchase=purchase,
                amount=purchase_amounts[purchase.pk],
            )
            allocation.full_clean()
            allocation.save()
        return setoff
