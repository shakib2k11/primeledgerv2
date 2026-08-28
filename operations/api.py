from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.response import Response

from accounting.models import Account, FiscalPeriod, MoneyReceipt
from core.api import TenantViewSetMixin, validation_detail
from core.application.services import (
    PURCHASES_MANAGE, PURCHASES_POST, PURCHASES_VIEW,
    SALES_MANAGE, SALES_POST, SALES_VIEW,
)
from core.models import Party, Product
from core.infrastructure.numbering import allocate_reference_number
from operations.application.services import PostTradeDocumentCommand, post_trade_document
from operations.infrastructure.repositories import DjangoTradeDocumentRepository
from operations.models import TradeDocument, TradeLine


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


class TradeDocumentSerializer(serializers.ModelSerializer):
    lines = TradeLineSerializer(many=True)
    kind = serializers.CharField(read_only=True)
    party_name = serializers.CharField(source="party.name", read_only=True)
    journal_entry = serializers.PrimaryKeyRelatedField(read_only=True)
    money_receipt_number = serializers.SerializerMethodField()

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
            raise serializers.ValidationError("Posted documents cannot be edited.")
        lines = attrs.get("lines")
        if self.instance is None and not lines:
            raise serializers.ValidationError("Add at least one product or service line.")
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
            raise serializers.ValidationError("Add at least one product or service line.")
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
        ).prefetch_related("lines__product")
        state = self.request.query_params.get("state")
        if state in TradeDocument.Status.values:
            queryset = queryset.filter(status=state)
        return queryset

    def permission_for_request(self, request):
        if getattr(self, "action", None) == "post":
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


class PurchaseViewSet(BaseTradeDocumentViewSet):
    kind = TradeDocument.Kind.PURCHASE
    view_permission = PURCHASES_VIEW
    manage_permission = PURCHASES_MANAGE
    post_permission = PURCHASES_POST
