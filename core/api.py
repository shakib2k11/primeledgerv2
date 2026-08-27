from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from rest_framework import serializers, status, viewsets
from rest_framework.exceptions import NotFound, PermissionDenied as DRFPermissionDenied
from rest_framework.response import Response

from core.application.services import (
    CONTACTS_MANAGE,
    CONTACTS_VIEW,
    INVENTORY_MANAGE,
    INVENTORY_VIEW,
    PermissionDenied,
    require_permission,
)
from core.infrastructure.repositories import DjangoBusinessReader
from core.infrastructure.numbering import allocate_reference_number
from core.models import InventoryUnit, Party, Product, StockMovement


def validation_detail(exc):
    if hasattr(exc, "message_dict"):
        return exc.message_dict
    return {"non_field_errors": list(exc.messages)}


class TenantViewSetMixin:
    business_reader = DjangoBusinessReader()
    view_permission = None
    manage_permission = None

    def permission_for_request(self, request):
        return self.view_permission if request.method in ("GET", "HEAD", "OPTIONS") else self.manage_permission

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        raw_id = request.headers.get("X-Business-ID") or request.query_params.get("business")
        if raw_id is None:
            raise NotFound("A valid business context is required.")
        try:
            business_id = int(raw_id)
        except (TypeError, ValueError):
            raise NotFound("A valid business context is required.")
        self.business = self.business_reader.for_user(
            request.user.pk, request.user.is_superuser, business_id
        )
        if self.business is None:
            # Deliberately identical for a missing business and another tenant's business.
            raise NotFound("A valid business context is required.")
        permission = self.permission_for_request(request)
        if permission:
            try:
                require_permission(request.user, self.business, permission)
            except PermissionDenied as exc:
                raise DRFPermissionDenied("You do not have permission for this operation.") from exc

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "business": self.business}


class PartySerializer(serializers.ModelSerializer):
    class Meta:
        model = Party
        exclude = ["business"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        business = self.context["business"]
        name = attrs.get("name", getattr(self.instance, "name", ""))
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        duplicate = Party.objects.filter(business=business, name__iexact=name, kind=kind)
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError("A contact with this name and type already exists.")
        return attrs

    def create(self, validated_data):
        return Party.objects.create(business=self.context["business"], **validated_data)


class ProductSerializer(serializers.ModelSerializer):
    unit = serializers.SlugRelatedField(
        slug_field="code",
        queryset=InventoryUnit.objects.none(),
    )
    unit_display = serializers.CharField(source="unit.name", read_only=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if business:
            self.fields["unit"].queryset = InventoryUnit.objects.available_to(business)

    class Meta:
        model = Product
        exclude = ["business"]
        read_only_fields = ["id"]

    def validate_sku(self, value):
        value = value.strip()
        if value:
            duplicate = Product.objects.filter(
                business=self.context["business"], sku__iexact=value
            )
            if self.instance:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError("This SKU is already in use in this business.")
        return value

    def create(self, validated_data):
        product = Product(business=self.context["business"], **validated_data)
        try:
            product.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(validation_detail(exc)) from exc
        product.save()
        return product

    def update(self, instance, validated_data):
        for attribute, value in validated_data.items():
            setattr(instance, attribute, value)
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(validation_detail(exc)) from exc
        instance.save()
        return instance


class InventoryUnitSerializer(serializers.ModelSerializer):
    origin = serializers.SerializerMethodField()

    class Meta:
        model = InventoryUnit
        fields = ["id", "code", "name", "symbol", "is_active", "origin"]
        read_only_fields = ["id", "origin"]

    def get_origin(self, obj):
        return "default" if obj.business_id is None else "business"

    def create(self, validated_data):
        unit = InventoryUnit(business=self.context["business"], **validated_data)
        try:
            unit.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(validation_detail(exc)) from exc
        unit.save()
        return unit

    def update(self, instance, validated_data):
        for attribute, value in validated_data.items():
            setattr(instance, attribute, value)
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(validation_detail(exc)) from exc
        instance.save()
        return instance


class StockMovementSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = StockMovement
        exclude = ["business", "created_by"]
        read_only_fields = ["id", "number", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if business:
            self.fields["product"].queryset = Product.objects.filter(
                business=business, is_active=True, is_service=False
            )

    def create(self, validated_data):
        movement = StockMovement(
            business=self.context["business"],
            created_by=self.context["request"].user,
            **validated_data,
        )
        movement.number = allocate_reference_number(
            business_id=movement.business_id,
            occurred_on=movement.occurred_at,
        )
        try:
            movement.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(validation_detail(exc)) from exc
        movement.save()
        return movement


class PartyViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    serializer_class = PartySerializer
    view_permission = CONTACTS_VIEW
    manage_permission = CONTACTS_MANAGE
    ordering_fields = ["name", "kind", "opening_balance"]

    def get_queryset(self):
        queryset = Party.objects.filter(business=self.business)
        if self.request.query_params.get("include_inactive") != "true":
            queryset = queryset.filter(is_active=True)
        query = self.request.query_params.get("search", "").strip()
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(phone__icontains=query) | Q(email__icontains=query))
        return queryset.order_by("name", "pk")

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active"])


class ProductViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    serializer_class = ProductSerializer
    view_permission = INVENTORY_VIEW
    manage_permission = INVENTORY_MANAGE
    ordering_fields = ["name", "sku", "sale_price", "reorder_level"]

    def get_queryset(self):
        queryset = Product.objects.filter(business=self.business).select_related("unit")
        if self.request.query_params.get("include_inactive") != "true":
            queryset = queryset.filter(is_active=True)
        query = self.request.query_params.get("search", "").strip()
        if query:
            queryset = queryset.filter(Q(name__icontains=query) | Q(sku__icontains=query) | Q(barcode__icontains=query))
        return queryset.order_by("name", "pk")

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active"])


class InventoryUnitViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    serializer_class = InventoryUnitSerializer
    view_permission = INVENTORY_VIEW
    manage_permission = INVENTORY_MANAGE
    ordering_fields = ["name", "code"]

    def get_queryset(self):
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return InventoryUnit.objects.available_to(
                self.business,
                include_inactive=self.request.query_params.get("include_inactive") == "true",
            ).order_by("name", "pk")
        return InventoryUnit.objects.filter(business=self.business).order_by("name", "pk")

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active"])


class StockMovementViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    serializer_class = StockMovementSerializer
    http_method_names = ["get", "post", "head", "options"]
    view_permission = INVENTORY_VIEW
    manage_permission = INVENTORY_MANAGE
    ordering_fields = ["number", "occurred_at", "quantity", "unit_cost"]

    def get_queryset(self):
        return StockMovement.objects.filter(business=self.business).select_related("product")

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)
