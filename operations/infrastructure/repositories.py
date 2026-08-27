from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from accounting.models import Account, FiscalPeriod, JournalEntry, JournalLine, Voucher
from core.models import Product, StockMovement
from core.infrastructure.numbering import allocate_reference_number
from operations.models import TradeDocument


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
            raise ValidationError("A sale or purchase requires at least one line.")
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
                    raise ValidationError(f"Insufficient stock for {product.name}.")
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
            raise ValidationError("A sale or purchase total must be greater than zero.")
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
                    "Product sales require active Inventory and Cost of Goods Sold posting roles. Apply the default chart template or map these accounts."
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
            raise ValidationError("This document was already posted.")
        document.status = TradeDocument.Status.POSTED
        document.subtotal = subtotal
        document.total = total
        document.journal_entry = journal
        document.posted_at = timezone.now()
        return document
