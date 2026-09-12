from decimal import Decimal

from rest_framework import serializers

from .models import Client, Debt, Installment, Payment, Preference


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


class PreferenceSerializer(serializers.ModelSerializer):
    overdueAlerts = serializers.BooleanField(source="overdue_alerts", required=False)

    class Meta:
        model = Preference
        fields = ["language", "currency", "reminders", "overdueAlerts"]


class UserSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)
    name = serializers.SerializerMethodField()

    def get_name(self, user):
        return user.get_full_name() or user.email


class UserUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
