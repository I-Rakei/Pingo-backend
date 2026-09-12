import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class TimeStampedPublicModel(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Client(TimeStampedPublicModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="clients")
    legacy_id = models.CharField(max_length=96, blank=True, db_index=True)
    name = models.CharField(max_length=160)
    phone = models.CharField(max_length=40, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    notes = models.TextField(blank=True)
    mobile_device = models.ForeignKey("MobileDevice", on_delete=models.SET_NULL, null=True, blank=True, related_name="clients")
    mobile_local_id = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [models.UniqueConstraint(fields=["mobile_device", "mobile_local_id"], name="unique_mobile_client_local_id")]

    def __str__(self):
        return self.name


class Preference(TimeStampedPublicModel):
    owner = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pingo_preferences")
    language = models.CharField(max_length=12, default="en")
    currency = models.CharField(max_length=3, default="MZN")
    reminders = models.BooleanField(default=True)
    overdue_alerts = models.BooleanField(default=True)


class Debt(TimeStampedPublicModel):
    class LoanType(models.TextChoices):
        MULTI = "multi", "Multi-period"
        SINGLE = "single", "Single loan"

    class Status(models.TextChoices):
        UNPAID = "unpaid", "Unpaid"
        OVERDUE = "overdue", "Overdue"
        PAID = "paid", "Paid"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="debts")
    legacy_id = models.CharField(max_length=96, blank=True, db_index=True)
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="debts")
    reference = models.CharField(max_length=24, unique=True, editable=False)
    loan_type = models.CharField(max_length=8, choices=LoanType.choices)
    principal = models.DecimalField(max_digits=14, decimal_places=2)
    capital_remaining = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    interest_rate = models.DecimalField(max_digits=7, decimal_places=2)
    penalty_rate = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0"))
    duration_months = models.PositiveIntegerField(default=1)
    total = models.DecimalField(max_digits=14, decimal_places=2)
    outstanding = models.DecimalField(max_digits=14, decimal_places=2)
    collected = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    start_date = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.UNPAID)
    mobile_device = models.ForeignKey("MobileDevice", on_delete=models.SET_NULL, null=True, blank=True, related_name="debts")
    mobile_local_id = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["mobile_device", "mobile_local_id"], name="unique_mobile_debt_local_id")]

    def __str__(self):
        return self.reference

    @property
    def current_status(self):
        if self.outstanding <= 0:
            return self.Status.PAID
        if self.due_date < timezone.localdate():
            return self.Status.OVERDUE
        return self.Status.UNPAID

    def set_status(self):
        self.status = self.current_status


class Installment(TimeStampedPublicModel):
    debt = models.ForeignKey(Debt, on_delete=models.CASCADE, related_name="installments")
    number = models.PositiveIntegerField()
    due_date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    base_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    penalty_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    paid_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    mobile_device = models.ForeignKey("MobileDevice", on_delete=models.SET_NULL, null=True, blank=True, related_name="installments")
    mobile_local_id = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(fields=["debt", "number"], name="unique_debt_installment_number"),
            models.UniqueConstraint(fields=["mobile_device", "mobile_local_id"], name="unique_mobile_installment_local_id"),
        ]


class Payment(TimeStampedPublicModel):
    class PaymentType(models.TextChoices):
        PRINCIPAL = "principal", "Principal"
        INTEREST = "interest", "Interest"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="payments")
    legacy_id = models.CharField(max_length=96, blank=True, db_index=True)
    debt = models.ForeignKey(Debt, on_delete=models.CASCADE, related_name="payments")
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name="payments")
    installment = models.ForeignKey(Installment, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments")
    operation_id = models.UUIDField(default=uuid.uuid4, db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    note = models.TextField(blank=True)
    payment_type = models.CharField(max_length=10, choices=PaymentType.choices)
    payment_date = models.DateField(default=timezone.localdate)
    reversed_at = models.DateTimeField(null=True, blank=True)
    mobile_device = models.ForeignKey("MobileDevice", on_delete=models.SET_NULL, null=True, blank=True, related_name="payments")
    mobile_local_id = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["-payment_date", "-created_at"]
        constraints = [models.UniqueConstraint(fields=["mobile_device", "mobile_local_id"], name="unique_mobile_payment_local_id")]

    @property
    def is_reversed(self):
        return self.reversed_at is not None


class MobileDevice(TimeStampedPublicModel):
    """A stable, app-generated identifier used to namespace offline SQLite IDs."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mobile_devices")
    device_id = models.CharField(max_length=128, unique=True)
    label = models.CharField(max_length=160, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_synced_at", "-created_at"]

    def __str__(self):
        return self.label or self.device_id


class MobileSyncBatch(TimeStampedPublicModel):
    """Records a completed upload so retrying it cannot duplicate ledger rows."""

    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"

    device = models.ForeignKey(MobileDevice, on_delete=models.CASCADE, related_name="sync_batches")
    batch_id = models.CharField(max_length=128)
    payload_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.COMPLETED)
    counts = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["device", "batch_id"], name="unique_mobile_device_batch")]


class WebPushSubscription(TimeStampedPublicModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="web_push_subscriptions")
    endpoint = models.TextField(unique=True)
    p256dh = models.TextField()
    auth = models.TextField()
    user_agent = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.owner_id}: {self.endpoint[:48]}"


class PushDelivery(TimeStampedPublicModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_deliveries")
    event_key = models.CharField(max_length=180)
    payload = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["owner", "event_key"], name="unique_owner_push_event")]

    def __str__(self):
        return self.event_key
