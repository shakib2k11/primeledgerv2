import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.response import Response

from accounting.models import (
    Account, ExpensePayment, ExpenseRecord, FiscalPeriod, JournalEntry,
    JournalLine, Voucher,
)
from accounting.application.services import (
    CreateExpenseCommand, PayExpenseCommand, create_expense, pay_expense,
)
from accounting.infrastructure.repositories import (
    DjangoExpensePaymentRepository, DjangoExpenseRepository,
)
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


class ExpensePaymentSerializer(serializers.ModelSerializer):
    payment_account_name = serializers.CharField(
        source="payment_account.name", read_only=True
    )
    voucher_number = serializers.CharField(source="voucher.number", read_only=True)

    class Meta:
        model = ExpensePayment
        fields = [
            "id", "number", "payment_date", "payment_account",
            "payment_account_name", "amount", "journal_entry", "voucher_number",
            "notes", "created_at",
        ]
        read_only_fields = fields


class ExpenseSerializer(serializers.ModelSerializer):
    payee_name = serializers.CharField(source="payee.name", read_only=True)
    expense_account_name = serializers.CharField(
        source="expense_account.name", read_only=True
    )
    voucher_number = serializers.CharField(source="voucher.number", read_only=True)
    payments = ExpensePaymentSerializer(many=True, read_only=True)
    paid_amount = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    balance_due = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    payment_status = serializers.CharField(read_only=True)

    class Meta:
        model = ExpenseRecord
        exclude = ["business", "idempotency_key", "created_by"]
        read_only_fields = [field.name for field in ExpenseRecord._meta.fields]


class ExpenseCreateSerializer(serializers.Serializer):
    expense_date = serializers.DateField(default=timezone.localdate)
    expense_account = serializers.PrimaryKeyRelatedField(queryset=Account.objects.none())
    payee = serializers.PrimaryKeyRelatedField(
        queryset=Party.objects.none(), required=False, allow_null=True
    )
    settlement = serializers.ChoiceField(choices=ExpenseRecord.Settlement.choices)
    payment_account = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects.none(), required=False, allow_null=True
    )
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    description = serializers.CharField(max_length=255)
    external_reference = serializers.CharField(
        max_length=80, required=False, allow_blank=True, default=""
    )
    idempotency_key = serializers.UUIDField(default=uuid.uuid4)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        business = self.context.get("business")
        if business:
            self.fields["expense_account"].queryset = Account.objects.filter(
                business=business, is_active=True, account_type=Account.Type.EXPENSE
            )
            self.fields["payment_account"].queryset = Account.objects.filter(
                business=business,
                is_active=True,
                system_role__in=[
                    Account.SystemRole.CASH, Account.SystemRole.BANK,
                    Account.SystemRole.MOBILE_MONEY,
                ],
            )
            self.fields["payee"].queryset = Party.objects.filter(
                business=business, is_active=True
            )

    def validate(self, attrs):
        if attrs["expense_date"] > timezone.localdate():
            raise serializers.ValidationError("Expense date cannot be in the future.")
        if attrs["settlement"] == ExpenseRecord.Settlement.PAID:
            if not attrs.get("payment_account"):
                raise serializers.ValidationError("Paid expenses require a payment account.")
        elif not attrs.get("payee"):
            raise serializers.ValidationError("Pay-later expenses require a payee.")
        return attrs


class ExpensePaymentInputSerializer(serializers.Serializer):
    payment_date = serializers.DateField(default=timezone.localdate)
    payment_account = serializers.PrimaryKeyRelatedField(queryset=Account.objects.none())
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
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
                    Account.SystemRole.CASH, Account.SystemRole.BANK,
                    Account.SystemRole.MOBILE_MONEY,
                ],
            )


class ExpenseViewSet(TenantViewSetMixin, viewsets.GenericViewSet):
    view_permission = ACCOUNTING_VIEW
    manage_permission = ACCOUNTING_POST
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        return ExpenseRecord.objects.filter(business=self.business).select_related(
            "payee", "expense_account", "payment_account", "payable_account",
            "journal_entry", "voucher",
        ).prefetch_related("payments__payment_account", "payments__voucher")

    def list(self, request):
        return Response(ExpenseSerializer(self.get_queryset(), many=True).data)

    def retrieve(self, request, pk=None):
        return Response(ExpenseSerializer(self.get_object()).data)

    def create(self, request):
        serializer = ExpenseCreateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        try:
            expense = create_expense(
                CreateExpenseCommand(
                    business_id=self.business.pk,
                    expense_date=values["expense_date"],
                    expense_account_id=values["expense_account"].pk,
                    settlement=values["settlement"],
                    amount=values["amount"],
                    description=values["description"],
                    idempotency_key=values["idempotency_key"],
                    payee_id=values.get("payee").pk if values.get("payee") else None,
                    payment_account_id=(
                        values.get("payment_account").pk
                        if values.get("payment_account") else None
                    ),
                    external_reference=values["external_reference"],
                    user_id=request.user.pk,
                ),
                DjangoExpenseRepository(),
            )
        except DjangoValidationError as exc:
            return Response(validation_detail(exc), status=status.HTTP_400_BAD_REQUEST)
        return Response(ExpenseSerializer(expense).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        expense = self.get_object()
        serializer = ExpensePaymentInputSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        try:
            payment = pay_expense(
                PayExpenseCommand(
                    expense_id=expense.pk,
                    business_id=self.business.pk,
                    payment_account_id=values["payment_account"].pk,
                    amount=values["amount"],
                    payment_date=values["payment_date"],
                    idempotency_key=values["idempotency_key"],
                    notes=values["notes"],
                    user_id=request.user.pk,
                ),
                DjangoExpensePaymentRepository(),
            )
        except DjangoValidationError as exc:
            return Response(validation_detail(exc), status=status.HTTP_400_BAD_REQUEST)
        return Response(ExpensePaymentSerializer(payment).data, status=status.HTTP_201_CREATED)
import uuid
from decimal import Decimal
