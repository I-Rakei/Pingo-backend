from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from .models import (BalanceNote, Client, CreditNote, Debt, DebitNote, Installment, Invoice, Organization,
                     OrganizationMembership, Payment, Preference)


class MobileAuthSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False, write_only=True)
    name = serializers.CharField(max_length=150, required=False, allow_blank=True)


class MobileSyncSerializer(serializers.Serializer):
    deviceId = serializers.CharField(max_length=128)
    deviceLabel = serializers.CharField(max_length=160, required=False, allow_blank=True)
    batchId = serializers.CharField(max_length=128)
    snapshot = serializers.DictField()

    def validate_snapshot(self, value):
        required = ("clients", "debts", "installments", "payments")
        missing = [name for name in required if name not in value]
        if missing:
            raise serializers.ValidationError(f"Missing collections: {', '.join(missing)}.")
        for name in required:
            if not isinstance(value[name], list):
                raise serializers.ValidationError({name: "Must be a list."})
            if not all(isinstance(item, dict) for item in value[name]):
                raise serializers.ValidationError({name: "Every row must be an object."})
        return value


class WebPushSubscriptionSerializer(serializers.Serializer):
    endpoint = serializers.URLField(max_length=2048)
    expirationTime = serializers.IntegerField(required=False, allow_null=True)
    keys = serializers.DictField()

    def validate_keys(self, value):
        p256dh = value.get("p256dh")
        auth = value.get("auth")
        if not isinstance(p256dh, str) or not p256dh or not isinstance(auth, str) or not auth:
            raise serializers.ValidationError("The p256dh and auth keys are required.")
        return {"p256dh": p256dh, "auth": auth}


class ClientSerializer(serializers.ModelSerializer):
    publicId = serializers.UUIDField(source="public_id", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)
    address = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = Client
        # share_token is deliberately excluded here -- it is a capability token,
        # never part of the general authenticated payload. It is only ever
        # exposed via ClientShareSerializer from the dedicated share-management
        # endpoint.
        fields = ["id", "publicId", "name", "phone", "email", "address", "notes", "createdAt", "updatedAt"]


class InstallmentSerializer(serializers.ModelSerializer):
    dueDate = serializers.DateField(source="due_date")
    paidAmount = serializers.DecimalField(source="paid_amount", max_digits=14, decimal_places=2)
    publicId = serializers.UUIDField(source="public_id", read_only=True)

    class Meta:
        model = Installment
        fields = ["publicId", "number", "dueDate", "amount", "paidAmount"]


class DebtSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="reference", read_only=True)
    publicId = serializers.UUIDField(source="public_id", read_only=True)
    clientId = serializers.IntegerField(source="client_id", read_only=True)
    name = serializers.CharField(source="client.name", read_only=True)
    type = serializers.SerializerMethodField()
    loanType = serializers.CharField(source="loan_type")
    capitalRemaining = serializers.DecimalField(source="capital_remaining", max_digits=14, decimal_places=2)
    interestRate = serializers.DecimalField(source="interest_rate", max_digits=7, decimal_places=2)
    penaltyRate = serializers.DecimalField(source="penalty_rate", max_digits=7, decimal_places=2)
    durationMonths = serializers.IntegerField(source="duration_months")
    installmentsPaid = serializers.SerializerMethodField(method_name="get_installments_paid")
    installmentsTotal = serializers.SerializerMethodField(method_name="get_installments_total")
    status = serializers.SerializerMethodField()
    startDate = serializers.DateField(source="start_date")
    dueDate = serializers.DateField(source="due_date")
    installments = InstallmentSerializer(many=True, read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Debt
        fields = ["id", "publicId", "clientId", "name", "type", "loanType", "principal", "capitalRemaining",
                  "interestRate", "penaltyRate", "durationMonths", "total", "outstanding", "collected",
                  "installmentsPaid", "installmentsTotal", "startDate", "dueDate", "status", "installments",
                  "createdAt", "updatedAt"]
        read_only_fields = ["principal", "capitalRemaining", "total", "outstanding", "collected", "status"]

    def get_type(self, obj):
        return "Multi-period" if obj.loan_type == Debt.LoanType.MULTI else "Single loan"

    def get_installments_paid(self, obj):
        return sum(item.paid_amount >= item.amount for item in obj.installments.all())

    def get_installments_total(self, obj):
        return obj.installments.count()

    def get_status(self, obj):
        return obj.current_status


class DebtCreateSerializer(serializers.Serializer):
    clientId = serializers.IntegerField(required=False)
    name = serializers.CharField(max_length=160, required=False)
    loanType = serializers.ChoiceField(choices=Debt.LoanType.values)
    principal = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    interestRate = serializers.DecimalField(max_digits=7, decimal_places=2, min_value=Decimal("0"))
    penaltyRate = serializers.DecimalField(max_digits=7, decimal_places=2, min_value=Decimal("0"), required=False, default=0)
    durationMonths = serializers.IntegerField(min_value=1, required=False, default=1)
    startDate = serializers.DateField()
    dueDate = serializers.DateField()

    def validate(self, attrs):
        if not attrs.get("clientId") and not attrs.get("name"):
            raise serializers.ValidationError("clientId or name is required.")
        if attrs["dueDate"] < attrs["startDate"]:
            raise serializers.ValidationError({"dueDate": "Must be on or after startDate."})
        if attrs["loanType"] == Debt.LoanType.SINGLE:
            attrs["durationMonths"] = 1
            attrs["penaltyRate"] = 0
        return attrs


class DebtUpdateSerializer(serializers.ModelSerializer):
    penaltyRate = serializers.DecimalField(source="penalty_rate", max_digits=7, decimal_places=2, min_value=Decimal("0"), required=False)
    dueDate = serializers.DateField(source="due_date", required=False)

    class Meta:
        model = Debt
        fields = ["penaltyRate", "dueDate"]

    def update(self, instance, validated_data):
        due_date = validated_data.get("due_date")
        debt = super().update(instance, validated_data)
        if due_date:
            final_installment = debt.installments.order_by("-number").first()
            if final_installment:
                final_installment.due_date = due_date
                final_installment.save(update_fields=["due_date", "updated_at"])
            debt._prefetched_objects_cache = {}
        return debt


class PaymentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="public_id", read_only=True)
    debtId = serializers.CharField(source="debt.reference", read_only=True)
    clientId = serializers.IntegerField(source="client_id", read_only=True)
    name = serializers.CharField(source="client.name", read_only=True)
    type = serializers.CharField(source="payment_type", read_only=True)
    date = serializers.DateField(source="payment_date", read_only=True)
    installmentNumber = serializers.IntegerField(source="installment.number", read_only=True, allow_null=True)
    reversedAt = serializers.DateTimeField(source="reversed_at", read_only=True)

    class Meta:
        model = Payment
        fields = ["id", "debtId", "clientId", "name", "amount", "type", "date", "installmentNumber", "reversedAt"]


class PaymentRequestSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=["balance", "interest", "principal", "settle"])
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), required=False)
    installmentNumber = serializers.IntegerField(min_value=1, required=False, allow_null=True)

    def validate(self, attrs):
        if attrs["action"] in {"balance", "principal"} and "amount" not in attrs:
            raise serializers.ValidationError({"amount": "This payment action requires an amount."})
        return attrs


class ClientShareSerializer(serializers.Serializer):
    """Owner-facing view of a client's share link. Never served publicly."""

    shareToken = serializers.CharField(source="share_token", allow_null=True, read_only=True)
    shareUrl = serializers.SerializerMethodField()

    def get_shareUrl(self, client):
        if not client.share_token:
            return None
        base = getattr(settings, "FRONTEND_BASE_URL", "").rstrip("/")
        return f"{base}/?share={client.share_token}"


class ClientPublicInstallmentSerializer(serializers.ModelSerializer):
    dueDate = serializers.DateField(source="due_date")
    paidAmount = serializers.DecimalField(source="paid_amount", max_digits=14, decimal_places=2)

    class Meta:
        model = Installment
        fields = ["number", "dueDate", "amount", "paidAmount"]


class ClientPublicPaymentSerializer(serializers.ModelSerializer):
    debtId = serializers.CharField(source="debt.reference", read_only=True)
    type = serializers.CharField(source="payment_type", read_only=True)
    date = serializers.DateField(source="payment_date", read_only=True)

    class Meta:
        model = Payment
        fields = ["debtId", "amount", "type", "date"]


class ClientPublicDebtSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="reference", read_only=True)
    type = serializers.SerializerMethodField()
    loanType = serializers.CharField(source="loan_type")
    status = serializers.SerializerMethodField()
    dueDate = serializers.DateField(source="due_date")
    installments = ClientPublicInstallmentSerializer(many=True, read_only=True)

    class Meta:
        model = Debt
        fields = ["id", "type", "loanType", "total", "outstanding", "collected", "status", "dueDate", "installments"]

    def get_type(self, obj):
        return "Multi-period" if obj.loan_type == Debt.LoanType.MULTI else "Single loan"

    def get_status(self, obj):
        return obj.current_status


class ClientPublicSerializer(serializers.Serializer):
    """Read-only, unauthenticated payload for a client's own share link.
    Deliberately excludes phone/email/address/notes and anything belonging to
    another client or the owner's account."""

    name = serializers.CharField()
    debts = serializers.SerializerMethodField()
    payments = serializers.SerializerMethodField()

    def get_debts(self, client):
        debts = client.debts.prefetch_related("installments")
        return ClientPublicDebtSerializer(debts, many=True).data

    def get_payments(self, client):
        payments = client.payments.filter(reversed_at__isnull=True).select_related("debt").order_by("-payment_date")
        return ClientPublicPaymentSerializer(payments, many=True).data


class PreferenceSerializer(serializers.ModelSerializer):
    overdueAlerts = serializers.BooleanField(source="overdue_alerts", required=False)

    class Meta:
        model = Preference
        fields = ["language", "currency", "reminders", "overdueAlerts"]


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False, write_only=True)


class OrganizationSetupSerializer(serializers.Serializer):
    """Company fields accepted alongside registration when accountType is
    corporate. All are optional except name -- nuit/address/logo/ivaRate are
    deliberately unvalidated free-form fields for v1 (see Organization model
    docstring)."""

    name = serializers.CharField(max_length=160)
    nuit = serializers.CharField(max_length=32, required=False, allow_blank=True)
    address = serializers.CharField(max_length=255, required=False, allow_blank=True)
    ivaRate = serializers.DecimalField(source="iva_rate", max_digits=6, decimal_places=2, required=False, default=Decimal("0"))
    logo = serializers.URLField(required=False, allow_blank=True)


class OrganizationSerializer(serializers.ModelSerializer):
    ivaRate = serializers.DecimalField(source="iva_rate", max_digits=6, decimal_places=2)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Organization
        fields = ["name", "nuit", "address", "ivaRate", "logo", "createdAt"]


class StaffMemberSerializer(serializers.Serializer):
    id = serializers.IntegerField(source="user.id", read_only=True)
    name = serializers.SerializerMethodField()
    email = serializers.EmailField(source="user.email", read_only=True)
    role = serializers.CharField()
    joinedAt = serializers.DateTimeField(source="created_at", read_only=True)

    def get_name(self, membership):
        return membership.user.get_full_name() or membership.user.email


class StaffCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False, write_only=True)
    role = serializers.ChoiceField(choices=OrganizationMembership.Role.choices)


class StaffUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    email = serializers.EmailField(required=False)
    password = serializers.CharField(trim_whitespace=False, write_only=True, required=False)
    role = serializers.ChoiceField(choices=OrganizationMembership.Role.choices, required=False)


class FiscalDocumentSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="public_id", read_only=True)
    clientId = serializers.IntegerField(source="client_id", read_only=True)
    clientName = serializers.CharField(source="client.name", read_only=True)
    debtId = serializers.CharField(source="debt.reference", read_only=True, allow_null=True)
    paymentId = serializers.UUIDField(source="payment.public_id", read_only=True, allow_null=True)
    issueDate = serializers.DateField(source="issue_date")
    issuedBy = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        fields = ["id", "number", "clientId", "clientName", "debtId", "paymentId", "issueDate", "amount", "description", "issuedBy", "createdAt"]

    def get_issuedBy(self, obj):
        return obj.issued_by.get_full_name() or obj.issued_by.email


class InvoiceSerializer(FiscalDocumentSerializer):
    class Meta(FiscalDocumentSerializer.Meta):
        model = Invoice


class DebitNoteSerializer(FiscalDocumentSerializer):
    class Meta(FiscalDocumentSerializer.Meta):
        model = DebitNote


class CreditNoteSerializer(FiscalDocumentSerializer):
    class Meta(FiscalDocumentSerializer.Meta):
        fields = FiscalDocumentSerializer.Meta.fields + ["reason"]
        model = CreditNote


class BalanceNoteSerializer(FiscalDocumentSerializer):
    class Meta(FiscalDocumentSerializer.Meta):
        model = BalanceNote


class DocumentCreateSerializer(serializers.Serializer):
    clientId = serializers.IntegerField()
    debtReference = serializers.CharField(required=False, allow_blank=True)
    paymentId = serializers.UUIDField(required=False)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0"))
    description = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)


class AmortizationScheduleSerializer(serializers.Serializer):
    """Constructed from a dict of {organization, debt} rather than a model
    instance -- see amortization_schedule_view."""

    organization = OrganizationSerializer()
    debtId = serializers.SerializerMethodField()
    clientName = serializers.SerializerMethodField()
    installments = serializers.SerializerMethodField()

    def get_debtId(self, obj):
        return obj["debt"].reference

    def get_clientName(self, obj):
        return obj["debt"].client.name

    def get_installments(self, obj):
        return InstallmentSerializer(obj["debt"].installments.all(), many=True).data


class UserSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)
    name = serializers.SerializerMethodField()

    def get_name(self, user):
        return user.get_full_name() or user.email


class UserUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
