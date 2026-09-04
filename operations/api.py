import uuid
from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.response import Response

from accounting.models import Account, FiscalPeriod, MoneyReceipt
from core.api import TenantViewSetMixin, validation_detail
from core.application.services import (
    ACCOUNTING_POST, ACCOUNTING_VIEW, PermissionDenied, require_permission,
    PURCHASES_MANAGE, PURCHASES_POST, PURCHASES_VIEW,
    SALES_MANAGE, SALES_POST, SALES_VIEW,
)
from core.models import Party, Product
from core.infrastructure.numbering import allocate_reference_number
from operations.application.services import (
    CreateBalanceSetoffCommand,
    PayPurchaseCommand,
    PostTradeDocumentCommand,
    ReceiveSalePaymentCommand,
    SetoffAllocationCommand,
    create_balance_setoff,
    pay_purchase,
    post_trade_document,
    receive_sale_payment,
)
from operations.infrastructure.repositories import (
    DjangoBalanceSetoffRepository,
    DjangoPurchasePaymentRepository,
    DjangoSalePaymentRepository,
    DjangoTradeDocumentRepository,
)
from operations.models import (
    BalanceSetoff,
    PurchasePayment,
    PurchaseSetoffAllocation,
    SalePayment,
    SaleSetoffAllocation,
    TradeDocument,
    TradeLine,
)
from django.utils.translation import gettext_lazy as _


class TradeLineSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = TradeLine
        fields = ["id", "product", "product_name", "description", "quantity", "unit_price", "line_total"]
        read_only_fields = ["id", "line_total"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if business:
            self.fields["product"].queryset = Product.objects.filter(business=business, is_active=True)


class SalePaymentSerializer(serializers.ModelSerializer):
    payment_account_name = serializers.CharField(
        source="payment_account.name",
        read_only=True,
    )
    money_receipt_number = serializers.CharField(
        source="money_receipt.number",
        read_only=True,
    )
    journal_entry = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = SalePayment
        fields = [
            "id", "number", "payment_date", "payment_account",
            "payment_account_name", "amount", "notes", "journal_entry",
            "money_receipt_number", "created_at",
        ]
        read_only_fields = fields


class PaymentAllocationInputSerializer(serializers.Serializer):
    payment_account = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects.none()
    )
    amount = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    payment_date = serializers.DateField(default=timezone.localdate)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    idempotency_key = serializers.UUIDField(default=uuid.uuid4)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if business:
            self.fields["payment_account"].queryset = Account.objects.filter(
                business=business,
                is_active=True,
                system_role__in=[
                    Account.SystemRole.CASH,
                    Account.SystemRole.BANK,
                    Account.SystemRole.MOBILE_MONEY,
                ],
            )

    def validate_payment_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError(_("Payment date cannot be in the future."))
        return value


class PurchasePaymentSerializer(serializers.ModelSerializer):
    payment_account_name = serializers.CharField(
        source="payment_account.name",
        read_only=True,
    )
    voucher_number = serializers.CharField(source="voucher.number", read_only=True)
    journal_entry = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = PurchasePayment
        fields = [
            "id", "number", "payment_date", "payment_account",
            "payment_account_name", "amount", "notes", "journal_entry",
            "voucher_number", "created_at",
        ]
        read_only_fields = fields


class SaleSetoffAllocationSerializer(serializers.ModelSerializer):
    document_id = serializers.IntegerField(source="sale_id", read_only=True)
    document_number = serializers.CharField(source="sale.number", read_only=True)

    class Meta:
        model = SaleSetoffAllocation
        fields = ["document_id", "document_number", "amount"]
        read_only_fields = fields


class PurchaseSetoffAllocationSerializer(serializers.ModelSerializer):
    document_id = serializers.IntegerField(source="purchase_id", read_only=True)
    document_number = serializers.CharField(source="purchase.number", read_only=True)

    class Meta:
        model = PurchaseSetoffAllocation
        fields = ["document_id", "document_number", "amount"]
        read_only_fields = fields


class BalanceSetoffSerializer(serializers.ModelSerializer):
    party_name = serializers.CharField(source="party.name", read_only=True)
    voucher_number = serializers.CharField(source="voucher.number", read_only=True)
    sale_allocations = SaleSetoffAllocationSerializer(many=True, read_only=True)
    purchase_allocations = PurchaseSetoffAllocationSerializer(many=True, read_only=True)

    class Meta:
        model = BalanceSetoff
        fields = [
            "id", "number", "setoff_date", "party", "party_name", "total_amount",
            "journal_entry", "voucher_number", "notes", "sale_allocations",
            "purchase_allocations", "created_at",
        ]
        read_only_fields = fields


class SetoffAllocationInputSerializer(serializers.Serializer):
    document_id = serializers.IntegerField(min_value=1)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )


class BalanceSetoffCreateSerializer(serializers.Serializer):
    party = serializers.PrimaryKeyRelatedField(queryset=Party.objects.none())
    setoff_date = serializers.DateField(default=timezone.localdate)
    sale_allocations = SetoffAllocationInputSerializer(many=True)
    purchase_allocations = SetoffAllocationInputSerializer(many=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    idempotency_key = serializers.UUIDField(default=uuid.uuid4)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if business:
            self.fields["party"].queryset = Party.objects.filter(
                business=business, is_active=True, kind=Party.Kind.BOTH
            )

    def validate_setoff_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError(_("Set-off date cannot be in the future."))
        return value

    def validate(self, attrs):
        sales = attrs.get("sale_allocations", [])
        purchases = attrs.get("purchase_allocations", [])
        if not sales or not purchases:
            raise serializers.ValidationError(
                _("Allocate at least one receivable invoice and one payable purchase.")
            )
        if len({item["document_id"] for item in sales}) != len(sales) or len(
            {item["document_id"] for item in purchases}
        ) != len(purchases):
            raise serializers.ValidationError(_("Each document may be allocated only once."))
        sale_total = sum((item["amount"] for item in sales), Decimal("0.00"))
        purchase_total = sum((item["amount"] for item in purchases), Decimal("0.00"))
        if sale_total != purchase_total:
            raise serializers.ValidationError(
                _("Receivable and payable allocation totals must be equal.")
            )
        return attrs


class TradeDocumentSerializer(serializers.ModelSerializer):
    lines = TradeLineSerializer(many=True)
    kind = serializers.CharField(read_only=True)
    party_name = serializers.CharField(source="party.name", read_only=True)
    journal_entry = serializers.PrimaryKeyRelatedField(read_only=True)
    money_receipt_number = serializers.SerializerMethodField()
    payments = SalePaymentSerializer(many=True, read_only=True)
    supplier_payments = PurchasePaymentSerializer(many=True, read_only=True)
    paid_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    balance_due = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    payment_status = serializers.CharField(read_only=True)

    class Meta:
        model = TradeDocument
        exclude = ["business", "created_by"]
        read_only_fields = [
            "id", "number", "subtotal", "discount_amount", "total",
            "status", "posted_at", "created_at",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        kind = self.context.get("kind")
        if business:
            party_kinds = (
                [Party.Kind.CUSTOMER, Party.Kind.BOTH]
                if kind == TradeDocument.Kind.SALE
                else [Party.Kind.SUPPLIER, Party.Kind.BOTH]
            )
            self.fields["party"].queryset = Party.objects.filter(
                business=business, is_active=True, kind__in=party_kinds
            )
            self.fields["period"].queryset = FiscalPeriod.objects.filter(business=business, is_locked=False)
            accounts = Account.objects.filter(business=business, is_active=True)
            self.fields["debit_account"].queryset = accounts
            self.fields["credit_account"].queryset = accounts
            self.fields["lines"].child.context.update(self.context)

    def validate(self, attrs):
        if self.instance and self.instance.status == TradeDocument.Status.POSTED:
            raise serializers.ValidationError(_("Posted documents cannot be edited."))
        lines = attrs.get("lines")
        if self.instance is None and not lines:
            raise serializers.ValidationError(_("Add at least one product or service line."))
        return attrs

    def get_money_receipt_number(self, instance):
        if not instance.journal_entry_id:
            return None
        try:
            return instance.journal_entry.voucher.money_receipt.number
        except (AttributeError, ObjectDoesNotExist):
            return None

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        document = TradeDocument(
            business=self.context["business"],
            kind=self.context["kind"],
            created_by=self.context["request"].user,
            **validated_data,
        )
        document.number = allocate_reference_number(
            business_id=document.business_id,
            occurred_on=document.document_date,
        )
        try:
            self._set_document_totals(document, lines)
            document.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(validation_detail(exc)) from exc
        document.save()
        self._replace_lines(document, lines)
        document.recalculate_total()
        document.save(update_fields=["subtotal", "discount_amount", "total"])
        return document

    @transaction.atomic
    def update(self, instance, validated_data):
        lines = validated_data.pop("lines", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        try:
            self._set_document_totals(instance, lines)
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(validation_detail(exc)) from exc
        instance.save()
        if lines is not None:
            instance.lines.all().delete()
            self._replace_lines(instance, lines)
        instance.recalculate_total()
        instance.save(update_fields=["subtotal", "discount_amount", "total"])
        return instance

    def _set_document_totals(self, document, lines):
        if lines is None:
            subtotal = sum(
                (line.line_total for line in document.lines.all()),
                Decimal("0.00"),
            )
        else:
            subtotal = sum(
                (
                    (values["quantity"] * values["unit_price"]).quantize(
                        Decimal("0.01")
                    )
                    for values in lines
                ),
                Decimal("0.00"),
            )
        document.set_totals(subtotal)

    def _replace_lines(self, document, lines):
        if not lines:
            raise serializers.ValidationError(_("Add at least one product or service line."))
        for values in lines:
            line = TradeLine(document=document, **values)
            try:
                line.full_clean(exclude=["line_total"])
            except DjangoValidationError as exc:
                raise serializers.ValidationError(validation_detail(exc)) from exc
            line.save()


class BaseTradeDocumentViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    serializer_class = TradeDocumentSerializer
    repository = DjangoTradeDocumentRepository()
    kind = None
    post_permission = None
    ordering_fields = ["document_date", "number", "total", "status"]

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "kind": self.kind}

    def get_queryset(self):
        queryset = TradeDocument.objects.filter(
            business=self.business, kind=self.kind
        ).select_related(
            "party", "period", "journal_entry__voucher__money_receipt"
        ).prefetch_related(
            "lines__product",
            "payments__payment_account",
            "payments__money_receipt",
            "supplier_payments__payment_account",
            "supplier_payments__voucher",
            "sale_setoff_allocations__setoff",
            "purchase_setoff_allocations__setoff",
        )
        state = self.request.query_params.get("state")
        if state in TradeDocument.Status.values:
            queryset = queryset.filter(status=state)
        return queryset

    def permission_for_request(self, request):
        if getattr(self, "action", None) in {"post", "receive_payment", "pay_supplier"}:
            return self.post_permission
        return super().permission_for_request(request)

    def perform_destroy(self, instance):
        if instance.status == TradeDocument.Status.POSTED or instance.period.is_locked:
            raise DRFPermissionDenied(
                "Only drafts in an open fiscal period can be deleted."
            )
        instance.delete()

    @action(detail=True, methods=["post"])
    def post(self, request, pk=None):
        document = self.get_object()
        try:
            document = post_trade_document(
                PostTradeDocumentCommand(document_id=document.pk, business_id=self.business.pk),
                self.repository,
            )
        except DjangoValidationError as exc:
            return Response(validation_detail(exc), status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(document).data)


class SaleViewSet(BaseTradeDocumentViewSet):
    kind = TradeDocument.Kind.SALE
    view_permission = SALES_VIEW
    manage_permission = SALES_MANAGE
    post_permission = SALES_POST

    @action(detail=True, methods=["post"], url_path="receive-payment")
    def receive_payment(self, request, pk=None):
        sale = self.get_object()
        serializer = PaymentAllocationInputSerializer(
            data=request.data,
            context={**self.get_serializer_context(), "business": self.business},
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        try:
            payment = receive_sale_payment(
                ReceiveSalePaymentCommand(
                    sale_id=sale.pk,
                    business_id=self.business.pk,
                    payment_account_id=values["payment_account"].pk,
                    amount=values["amount"],
                    payment_date=values["payment_date"],
                    idempotency_key=values["idempotency_key"],
                    notes=values["notes"],
                    user_id=request.user.pk,
                ),
                DjangoSalePaymentRepository(),
            )
        except DjangoValidationError as exc:
            return Response(
                validation_detail(exc),
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(SalePaymentSerializer(payment).data, status=status.HTTP_201_CREATED)


class PurchaseViewSet(BaseTradeDocumentViewSet):
    kind = TradeDocument.Kind.PURCHASE
    view_permission = PURCHASES_VIEW
    manage_permission = PURCHASES_MANAGE
    post_permission = PURCHASES_POST

    @action(detail=True, methods=["post"], url_path="pay-supplier")
    def pay_supplier(self, request, pk=None):
        purchase = self.get_object()
        serializer = PaymentAllocationInputSerializer(
            data=request.data,
            context={**self.get_serializer_context(), "business": self.business},
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        try:
            payment = pay_purchase(
                PayPurchaseCommand(
                    purchase_id=purchase.pk,
                    business_id=self.business.pk,
                    payment_account_id=values["payment_account"].pk,
                    amount=values["amount"],
                    payment_date=values["payment_date"],
                    idempotency_key=values["idempotency_key"],
                    notes=values["notes"],
                    user_id=request.user.pk,
                ),
                DjangoPurchasePaymentRepository(),
            )
        except DjangoValidationError as exc:
            return Response(
                validation_detail(exc),
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            PurchasePaymentSerializer(payment).data,
            status=status.HTTP_201_CREATED,
        )


class BalanceSetoffViewSet(TenantViewSetMixin, viewsets.GenericViewSet):
    view_permission = ACCOUNTING_VIEW
    manage_permission = ACCOUNTING_POST
    http_method_names = ["get", "post", "head", "options"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method == "POST":
            try:
                require_permission(request.user, self.business, SALES_POST)
                require_permission(request.user, self.business, PURCHASES_POST)
            except PermissionDenied as exc:
                raise DRFPermissionDenied(
                    "Sales, purchase, and accounting posting permissions are required."
                ) from exc

    def get_queryset(self):
        return BalanceSetoff.objects.filter(business=self.business).select_related(
            "party", "voucher", "journal_entry"
        ).prefetch_related(
            "sale_allocations__sale", "purchase_allocations__purchase"
        )

    def list(self, request):
        return Response(BalanceSetoffSerializer(self.get_queryset(), many=True).data)

    def retrieve(self, request, pk=None):
        return Response(BalanceSetoffSerializer(self.get_object()).data)

    def create(self, request):
        serializer = BalanceSetoffCreateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        try:
            setoff = create_balance_setoff(
                CreateBalanceSetoffCommand(
                    business_id=self.business.pk,
                    party_id=values["party"].pk,
                    setoff_date=values["setoff_date"],
                    sale_allocations=tuple(
                        SetoffAllocationCommand(**item)
                        for item in values["sale_allocations"]
                    ),
                    purchase_allocations=tuple(
                        SetoffAllocationCommand(**item)
                        for item in values["purchase_allocations"]
                    ),
                    idempotency_key=values["idempotency_key"],
                    notes=values["notes"],
                    user_id=request.user.pk,
                ),
                DjangoBalanceSetoffRepository(),
            )
        except DjangoValidationError as exc:
            return Response(validation_detail(exc), status=status.HTTP_400_BAD_REQUEST)
        return Response(BalanceSetoffSerializer(setoff).data, status=status.HTTP_201_CREATED)
