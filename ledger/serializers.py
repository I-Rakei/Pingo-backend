from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from .models import (AmortizationPlan, BalanceNote, Client, CreditNote, Debt, DebitNote, Installment, Invoice, Organization,
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


def _detail_list(value, keys, label):
    """Normalise an optional list of small string records, dropping empty rows."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise serializers.ValidationError(f"{label} must be a list.")
    rows = []
    for item in value:
        if not isinstance(item, dict):
            raise serializers.ValidationError(f"Each {label.lower()} entry must be an object.")
        row = {}
        for key, max_length in keys.items():
            text = item.get(key) or ""
            if not isinstance(text, str):
                raise serializers.ValidationError({key: "Must be text."})
            text = text.strip()
            if len(text) > max_length:
                raise serializers.ValidationError({key: f"Use at most {max_length} characters."})
            row[key] = text
        if any(row.values()):
            rows.append(row)
    if len(rows) > 10:
        raise serializers.ValidationError(f"Add at most 10 {label.lower()}.")
    return rows


CONTACT_KEYS = {"name": 160, "relationship": 80, "phone": 40, "email": 254}
BANK_ACCOUNT_KEYS = {"bank": 120, "accountNumber": 60, "nib": 40}


class ClientSerializer(serializers.ModelSerializer):
    publicId = serializers.UUIDField(source="public_id", read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)
    address = serializers.CharField(required=False, allow_blank=True)
    city = serializers.CharField(max_length=120, required=False, allow_blank=True)
    countryOfBirth = serializers.CharField(source="country_of_birth", max_length=120, required=False, allow_blank=True)
    idNumber = serializers.CharField(source="id_number", max_length=60, required=False, allow_blank=True)
    altPhone = serializers.CharField(source="alt_phone", max_length=40, required=False, allow_blank=True)
    nuit = serializers.CharField(max_length=40, required=False, allow_blank=True)
    contacts = serializers.JSONField(required=False)
    bankAccounts = serializers.JSONField(source="bank_accounts", required=False)

    class Meta:
        model = Client
        # share_token is deliberately excluded here -- it is a capability token,
        # never part of the general authenticated payload. It is only ever
        # exposed via ClientShareSerializer from the dedicated share-management
        # endpoint.
        fields = ["id", "publicId", "name", "phone", "email", "address", "notes", "city", "countryOfBirth", "idNumber",
                  "altPhone", "nuit", "contacts", "bankAccounts", "createdAt", "updatedAt"]

    CORPORATE_FIELDS = ("city", "countryOfBirth", "idNumber", "altPhone", "nuit", "contacts", "bankAccounts")

    def __init__(self, *args, corporate=False, **kwargs):
        super().__init__(*args, **kwargs)
        # Personal accounts keep the original client shape; extra details are corporate only.
        if not corporate:
            for name in self.CORPORATE_FIELDS:
                self.fields.pop(name)

    def validate_contacts(self, value):
        return _detail_list(value, CONTACT_KEYS, "Contact persons")

    def validate_bankAccounts(self, value):
        return _detail_list(value, BANK_ACCOUNT_KEYS, "Bank accounts")


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


def _public_debt_statement(debt):
    """What the client owes on one debt right now, split into capital and interest.

    Only open amounts count: paid periods and repaid capital are left out, so
    total = capital + interest = everything still due. Single loans keep
    capital apart from their interest periods. Multi-period instalments mix
    both, so each instalment's unpaid base is split in the loan's own
    capital-to-interest ratio, and any overdue penalty counts as interest."""
    from .services import money

    zero = Decimal("0")
    periods = list(debt.installments.all())
    due = []
    capital = zero
    open_total = zero
    if debt.loan_type == Debt.LoanType.MULTI:
        bases = sum(((item.base_amount or item.amount - item.penalty_amount) for item in periods), zero)
        ratio = (debt.principal / bases) if bases > 0 else Decimal("1")
    for item in periods:
        remaining = max(item.amount - item.paid_amount, zero)
        if remaining <= 0:
            continue
        open_total += remaining
        if debt.loan_type == Debt.LoanType.MULTI:
            base = item.base_amount or item.amount - item.penalty_amount
            capital += min(remaining, base) * ratio
        due.append({"debtId": debt.reference, "kind": "installment" if debt.loan_type == Debt.LoanType.MULTI else "interest",
                    "number": item.number, "dueDate": item.due_date, "amount": remaining})
    if debt.loan_type == Debt.LoanType.SINGLE:
        capital = max(debt.capital_remaining, zero)
        if capital > 0:
            due.append({"debtId": debt.reference, "kind": "capital", "number": None,
                        "dueDate": debt.due_date, "amount": capital})
        total = money(capital + open_total)
    else:
        total = money(open_total)
    capital = min(money(capital), total)
    summary = {"id": debt.reference, "status": debt.current_status, "dueDate": debt.due_date,
               "capital": capital, "interest": total - capital, "total": total}
    return summary, due


class ClientPublicSerializer(serializers.Serializer):
    """Read-only, unauthenticated payload for a client's own share link.

    Shows only what is owed now (capital, interest and their total, for each
    open debt), the payments made, and the payments still due. Fully paid debts
    are left out. Deliberately excludes phone/email/address/notes, rates,
    internal ledger fields, and anything belonging to another client or the
    owner's account."""

    def to_representation(self, client):
        payments = list(client.payments.filter(reversed_at__isnull=True)
                        .select_related("debt").order_by("-payment_date", "-created_at"))
        debts, payments_due = [], []
        for debt in client.debts.prefetch_related("installments").order_by("-created_at"):
            summary, due = _public_debt_statement(debt)
            if summary["total"] > 0:
                debts.append(summary)
            payments_due.extend(due)
        payments_due.sort(key=lambda item: item["dueDate"])
        zero = Decimal("0")
        totals = {key: sum((item[key] for item in debts), zero) for key in ("capital", "interest", "total")}
        return {
            "name": client.name,
            "summary": totals,
            "debts": debts,
            "paymentsMade": [{"debtId": item.debt.reference, "date": item.payment_date,
                              "amount": item.amount, "type": item.payment_type} for item in payments],
            "paymentsDue": payments_due,
        }


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


class AmortizationSimulationSerializer(serializers.Serializer):
    clientName = serializers.CharField(max_length=160, required=False, allow_blank=True)
    loanType = serializers.ChoiceField(choices=Debt.LoanType.values)
    principal = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    interestRate = serializers.DecimalField(max_digits=7, decimal_places=2, min_value=Decimal("0"))
    durationMonths = serializers.IntegerField(min_value=1, max_value=600)
    startDate = serializers.DateField()

    def validate(self, attrs):
        start = attrs["startDate"]
        if start.year + (start.month - 1 + attrs["durationMonths"]) // 12 > 9999:
            raise serializers.ValidationError({"durationMonths": "The final date is outside the supported date range."})
        interest = attrs["principal"] * attrs["interestRate"] / 100
        period_amount = interest if attrs["loanType"] == Debt.LoanType.SINGLE else (attrs["principal"] + interest) / attrs["durationMonths"]
        if period_amount > Decimal("999999999999.99"):
            raise serializers.ValidationError({"principal": "The calculated payment exceeds the supported amount."})
        return attrs


class AmortizationPlanCreateSerializer(AmortizationSimulationSerializer):
    clientId = serializers.IntegerField()


class LoanPreviewSerializer(serializers.Serializer):
    """Terms of a corporate loan before creation; mirrors DebtCreateSerializer's rules."""

    clientName = serializers.CharField(max_length=160, required=False, allow_blank=True)
    loanType = serializers.ChoiceField(choices=Debt.LoanType.values)
    principal = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    interestRate = serializers.DecimalField(max_digits=7, decimal_places=2, min_value=Decimal("0"))
    durationMonths = serializers.IntegerField(min_value=1, max_value=600, required=False, default=1)
    startDate = serializers.DateField()
    dueDate = serializers.DateField(required=False)

    def validate(self, attrs):
        start = attrs["startDate"]
        if attrs["loanType"] == Debt.LoanType.SINGLE:
            attrs["durationMonths"] = 1
            if not attrs.get("dueDate"):
                raise serializers.ValidationError({"dueDate": "Choose when the interest period ends."})
            if attrs["dueDate"] < start:
                raise serializers.ValidationError({"dueDate": "Must be on or after startDate."})
        elif start.year + (start.month - 1 + attrs["durationMonths"]) // 12 > 9999:
            raise serializers.ValidationError({"durationMonths": "The final date is outside the supported date range."})
        interest = attrs["principal"] * attrs["interestRate"] / 100
        period_amount = interest if attrs["loanType"] == Debt.LoanType.SINGLE else (attrs["principal"] + interest) / attrs["durationMonths"]
        if period_amount > Decimal("999999999999.99") or attrs["principal"] + interest > Decimal("999999999999.99"):
            raise serializers.ValidationError({"principal": "The loan total exceeds the supported amount."})
        return attrs


class AmortizationPlanSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="public_id", read_only=True)
    clientId = serializers.IntegerField(source="client_id", read_only=True)
    clientName = serializers.CharField(source="client.name", read_only=True)
    createdBy = serializers.SerializerMethodField()
    loanType = serializers.CharField(source="loan_type", read_only=True)
    principal = serializers.DecimalField(max_digits=14, decimal_places=2, coerce_to_string=True, read_only=True)
    interestRate = serializers.DecimalField(source="interest_rate", max_digits=7, decimal_places=2, coerce_to_string=True, read_only=True)
    durationMonths = serializers.IntegerField(source="duration_months", read_only=True)
    startDate = serializers.DateField(source="start_date", read_only=True)
    scheduleTotal = serializers.DecimalField(source="schedule_total", max_digits=18, decimal_places=2, coerce_to_string=True, read_only=True)
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    debtId = serializers.SerializerMethodField()
    dueDate = serializers.DateField(source="due_date", read_only=True)

    class Meta:
        model = AmortizationPlan
        fields = ["id", "clientId", "clientName", "createdBy", "debtId", "loanType", "principal", "interestRate", "durationMonths",
                  "startDate", "dueDate", "scheduleTotal", "createdAt"]

    def get_debtId(self, plan):
        return plan.debt.reference if plan.debt_id else ""

    def get_createdBy(self, plan):
        user = plan.created_by
        return (user.get_full_name() or user.email or user.username) if user else ""


class AmortizationRowSerializer(serializers.Serializer):
    number = serializers.IntegerField()
    rowType = serializers.CharField()
    dueDate = serializers.DateField()
    principalAmount = serializers.DecimalField(max_digits=14, decimal_places=2, coerce_to_string=True)
    interestAmount = serializers.DecimalField(max_digits=14, decimal_places=2, coerce_to_string=True)
    penaltyAmount = serializers.DecimalField(max_digits=14, decimal_places=2, coerce_to_string=True)
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, coerce_to_string=True)
    paidAmount = serializers.DecimalField(max_digits=14, decimal_places=2, coerce_to_string=True)
    remainingAmount = serializers.DecimalField(max_digits=14, decimal_places=2, coerce_to_string=True)
    capitalBalance = serializers.DecimalField(max_digits=14, decimal_places=2, coerce_to_string=True)


class AmortizationScheduleSerializer(serializers.Serializer):
    organization = OrganizationSerializer()
    debtId = serializers.CharField(allow_blank=True)
    clientName = serializers.CharField(allow_blank=True)
    loanType = serializers.CharField()
    principal = serializers.DecimalField(max_digits=14, decimal_places=2, coerce_to_string=True)
    interestRate = serializers.DecimalField(max_digits=7, decimal_places=2, coerce_to_string=True)
    startDate = serializers.DateField()
    dueDate = serializers.DateField()
    principalTotal = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=True)
    interestTotal = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=True)
    penaltyTotal = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=True)
    scheduleTotal = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=True)
    # Simulations report the full schedule total here, which can exceed a single ledger amount.
    collected = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=True)
    outstanding = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=True)
    installments = AmortizationRowSerializer(many=True)


class UserSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    email = serializers.EmailField(read_only=True)
    name = serializers.SerializerMethodField()

    def get_name(self, user):
        return user.get_full_name() or user.email


class UserUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
