from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.response import Response

from accounting.models import Account, FiscalPeriod, JournalEntry, JournalLine, Voucher
from core.api import TenantViewSetMixin, validation_detail
from core.application.services import (
    ACCOUNTING_MANAGE,
    ACCOUNTING_POST,
    ACCOUNTING_VIEW,
    PermissionDenied,
    PostJournalCommand,
    post_journal,
    require_permission,
)
from core.infrastructure.repositories import DjangoJournalRepository
from core.models import Party


class CleanModelSerializer(serializers.ModelSerializer):
    def _clean(self, instance):
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(validation_detail(exc)) from exc

    def create(self, validated_data):
        instance = self.Meta.model(business=self.context["business"], **validated_data)
        self._clean(instance)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        for key, value in validated_data.items():
            setattr(instance, key, value)
        self._clean(instance)
        instance.save()
        return instance


class AccountSerializer(CleanModelSerializer):
    class Meta:
        model = Account
        exclude = ["business"]
        read_only_fields = ["id", "is_system"]


class FiscalPeriodSerializer(CleanModelSerializer):
    class Meta:
        model = FiscalPeriod
        exclude = ["business"]
        read_only_fields = ["id"]


class JournalLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = JournalLine
        fields = ["id", "account", "party", "description", "debit", "credit"]
        read_only_fields = ["id"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if business:
            self.fields["account"].queryset = Account.objects.filter(business=business, is_active=True)
            self.fields["party"].queryset = Party.objects.filter(business=business, is_active=True)

    def validate(self, attrs):
        debit = attrs.get("debit", 0)
        credit = attrs.get("credit", 0)
        if (debit > 0) == (credit > 0):
            raise serializers.ValidationError("Provide either a debit or a credit amount.")
        return attrs


class JournalEntrySerializer(serializers.ModelSerializer):
    lines = JournalLineSerializer(many=True)
    total_debit = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    total_credit = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = JournalEntry
        exclude = ["business", "created_by"]
        read_only_fields = ["id", "posted", "created_at"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if business:
            self.fields["period"].queryset = FiscalPeriod.objects.filter(business=business)
            self.fields["lines"].child.context.update(self.context)

    def validate(self, attrs):
        period = attrs.get("period", getattr(self.instance, "period", None))
        entry_date = attrs.get("entry_date", getattr(self.instance, "entry_date", None))
        if period and period.is_locked:
            raise serializers.ValidationError("This fiscal period is locked.")
        if period and entry_date and not period.starts_on <= entry_date <= period.ends_on:
            raise serializers.ValidationError("Journal entry date must fall within its fiscal period.")
        lines = attrs.get("lines")
        if self.instance is None and (not lines or len(lines) < 2):
            raise serializers.ValidationError("A journal entry requires at least two lines.")
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        lines = validated_data.pop("lines")
        entry = JournalEntry(
            business=self.context["business"],
            created_by=self.context["request"].user,
            **validated_data,
        )
        try:
            entry.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(validation_detail(exc)) from exc
        entry.save()
        for line_data in lines:
            line = JournalLine(entry=entry, **line_data)
            try:
                line.full_clean()
            except DjangoValidationError as exc:
                raise serializers.ValidationError(validation_detail(exc)) from exc
            line.save()
        return entry

    @transaction.atomic
    def update(self, instance, validated_data):
        if instance.posted:
            raise serializers.ValidationError("Posted journal entries cannot be edited.")
        if instance.period.is_locked:
            raise serializers.ValidationError("This fiscal period is locked.")
        lines = validated_data.pop("lines", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        try:
            instance.full_clean()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(validation_detail(exc)) from exc
        instance.save()
        if lines is not None:
            instance.lines.all().delete()
            for line_data in lines:
                line = JournalLine(entry=instance, **line_data)
                try:
                    line.full_clean()
                except DjangoValidationError as exc:
                    raise serializers.ValidationError(validation_detail(exc)) from exc
                line.save()
        return instance


class VoucherSerializer(CleanModelSerializer):
    class Meta:
        model = Voucher
        exclude = ["business"]
        read_only_fields = ["id"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if business:
            self.fields["party"].queryset = Party.objects.filter(business=business, is_active=True)
            self.fields["journal_entry"].queryset = JournalEntry.objects.filter(business=business, posted=True)

    def update(self, instance, validated_data):
        raise serializers.ValidationError("Financial vouchers cannot be edited after creation.")


class AccountingViewSet(TenantViewSetMixin):
    view_permission = ACCOUNTING_VIEW
    manage_permission = ACCOUNTING_MANAGE

    def permission_for_request(self, request):
        if getattr(self, "action", None) == "post":
            return ACCOUNTING_POST
        return super().permission_for_request(request)


class AccountViewSet(AccountingViewSet, viewsets.ModelViewSet):
    serializer_class = AccountSerializer
    ordering_fields = ["code", "name", "account_type"]

    def get_queryset(self):
        queryset = Account.objects.filter(business=self.business)
        if self.request.query_params.get("include_inactive") != "true":
            queryset = queryset.filter(is_active=True)
        return queryset.order_by("code")

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=["is_active"])


class FiscalPeriodViewSet(AccountingViewSet, viewsets.ModelViewSet):
    serializer_class = FiscalPeriodSerializer
    ordering_fields = ["starts_on", "ends_on", "name"]

    def get_queryset(self):
        return FiscalPeriod.objects.filter(business=self.business).order_by("starts_on")

    def perform_destroy(self, instance):
        raise DRFPermissionDenied("Fiscal periods cannot be deleted.")


class JournalEntryViewSet(AccountingViewSet, viewsets.ModelViewSet):
    serializer_class = JournalEntrySerializer
    repository = DjangoJournalRepository()
    ordering_fields = ["entry_date", "reference", "created_at"]

    def get_queryset(self):
        return JournalEntry.objects.filter(business=self.business).select_related("period").prefetch_related("lines")

    def perform_destroy(self, instance):
        if instance.posted or instance.period.is_locked:
            raise DRFPermissionDenied("Posted or locked journal entries cannot be deleted.")
        instance.delete()

    @action(detail=True, methods=["post"])
    def post(self, request, pk=None):
        try:
            require_permission(request.user, self.business, ACCOUNTING_POST)
        except PermissionDenied as exc:
            raise DRFPermissionDenied("You do not have permission to post journals.") from exc
        entry = self.get_object()
        try:
            entry = post_journal(
                PostJournalCommand(entry_id=entry.pk, business_id=self.business.pk),
                self.repository,
            )
        except DjangoValidationError as exc:
            return Response(validation_detail(exc), status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(entry).data)


class VoucherViewSet(AccountingViewSet, viewsets.ModelViewSet):
    serializer_class = VoucherSerializer
    ordering_fields = ["voucher_date", "number", "total"]

    def get_queryset(self):
        return Voucher.objects.filter(business=self.business).select_related("party", "journal_entry")

    def perform_destroy(self, instance):
        raise DRFPermissionDenied("Financial vouchers cannot be deleted.")
