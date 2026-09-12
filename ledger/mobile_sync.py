"""Import a Dividas SQLite snapshot into an authenticated Pingo account."""

import hashlib
import json
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Client, Debt, Installment, MobileDevice, MobileSyncBatch, Payment
from .services import money, next_reference


def _local_id(row):
    # ``id`` keeps the endpoint practical for current Dividas SQLite exports;
    # new clients should send localId explicitly.
    value = row.get("localId", row.get("id"))
    if value is None or str(value).strip() == "":
        raise ValidationError({"snapshot": "Every uploaded row needs a localId."})
    return str(value)


def _date(value, field):
    if not value:
        raise ValidationError({"snapshot": f"{field} is required."})
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationError({"snapshot": f"{field} must be an ISO date."}) from exc


def _count():
    return {name: {"inserted": 0, "updated": 0} for name in ("clients", "debts", "installments", "payments")}


def _upsert(model, device, local_id, defaults, counts, bucket):
    instance, created = model.objects.update_or_create(
        mobile_device=device,
        mobile_local_id=local_id,
        defaults=defaults,
    )
    counts[bucket]["inserted" if created else "updated"] += 1
    return instance


def _decimal(value, field, default="0"):
    try:
        return money(default if value is None else value)
    except Exception as exc:
        raise ValidationError({"snapshot": f"{field} must be a valid amount."}) from exc


def _status_for_snapshot(debt, is_paid):
    if is_paid:
        return Debt.Status.PAID
    debt.set_status()
    return debt.status


def snapshot_hash(snapshot):
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@transaction.atomic
def sync_snapshot(*, user, device_id, device_label, batch_id, snapshot):
    """Upsert a complete snapshot. Missing rows are never treated as deletions."""
    device, created = MobileDevice.objects.get_or_create(
        device_id=device_id,
        defaults={"owner": user, "label": device_label},
    )
    if not created and device.owner_id != user.id:
        raise ValidationError({"deviceId": "This device is already linked to another account."})
    if device_label and device.label != device_label:
        device.label = device_label
        device.save(update_fields=["label", "updated_at"])

    payload_digest = snapshot_hash(snapshot)
    existing_batch = MobileSyncBatch.objects.filter(device=device, batch_id=batch_id).first()
    if existing_batch:
        if existing_batch.payload_hash != payload_digest:
            raise ValidationError({"batchId": "This batch ID was already used with different data."})
        return existing_batch.counts, True

    counts = _count()
    clients, debts, installments, payments = (
        snapshot.get("clients", []), snapshot.get("debts", []), snapshot.get("installments", []), snapshot.get("payments", []),
    )
    client_map, debt_map, installment_map = {}, {}, {}

    for row in clients:
        local_id = _local_id(row)
        client_map[local_id] = _upsert(Client, device, local_id, {
            "owner": user,
            "name": str(row.get("name") or "").strip(),
            "phone": str(row.get("phone") or ""),
            "email": str(row.get("email") or ""),
            "address": str(row.get("address") or ""),
            "notes": str(row.get("notes") or ""),
        }, counts, "clients")
        if not client_map[local_id].name:
            raise ValidationError({"snapshot": "Client name is required."})

    for row in debts:
        local_id, client_local_id = _local_id(row), row.get("clientLocalId", row.get("client_id"))
        client = client_map.get(str(client_local_id))
        if not client:
            raise ValidationError({"snapshot": f"Debt {local_id} references a missing clientLocalId."})
        loan_type = row.get("loanType", row.get("loan_type", Debt.LoanType.MULTI))
        if loan_type not in Debt.LoanType.values:
            raise ValidationError({"snapshot": f"Debt {local_id} has an unsupported loanType."})
        existing = Debt.objects.filter(mobile_device=device, mobile_local_id=local_id).first()
        reference = existing.reference if existing else next_reference()
        total = _decimal(row.get("totalAmount", row.get("total_amount")), "totalAmount")
        debt = _upsert(Debt, device, local_id, {
            "owner": user, "client": client, "reference": reference, "loan_type": loan_type,
            "principal": _decimal(row.get("amount"), "amount"),
            "capital_remaining": _decimal(row.get("capitalRemaining", row.get("capital_remaining")), "capitalRemaining"),
            "interest_rate": _decimal(row.get("interestRate", row.get("interest_rate")), "interestRate"),
            "penalty_rate": _decimal(row.get("penaltyRate", row.get("penalty_rate")), "penaltyRate"),
            "duration_months": max(1, int(row.get("durationMonths", row.get("duration_months", 1)) or 1)),
            "total": total, "outstanding": Decimal("0") if row.get("isPaid", row.get("is_paid", False)) else total,
            "collected": Decimal("0"), "start_date": _date(row.get("startDate", row.get("start_date")), "startDate"),
            "due_date": _date(row.get("dueDate", row.get("due_date")), "dueDate"), "status": Debt.Status.UNPAID,
        }, counts, "debts")
        debt_map[local_id] = debt

    for row in installments:
        local_id, debt_local_id = _local_id(row), row.get("debtLocalId", row.get("debt_id"))
        debt = debt_map.get(str(debt_local_id))
        if not debt:
            raise ValidationError({"snapshot": f"Installment {local_id} references a missing debtLocalId."})
        number = int(row.get("installmentNumber", row.get("installment_number", 0)) or 0)
        if number < 1:
            raise ValidationError({"snapshot": f"Installment {local_id} needs a positive installmentNumber."})
        # Dividas has a per-debt number uniqueness invariant in addition to its local IDs.
        conflict = Installment.objects.filter(debt=debt, number=number).exclude(mobile_device=device, mobile_local_id=local_id).exists()
        if conflict:
            raise ValidationError({"snapshot": f"Installment {local_id} conflicts with an existing period number."})
        installment_map[local_id] = _upsert(Installment, device, local_id, {
            "debt": debt, "number": number, "due_date": _date(row.get("dueDate", row.get("due_date")), "dueDate"),
            "amount": _decimal(row.get("totalAmount", row.get("total_amount")), "totalAmount"),
            "base_amount": _decimal(row.get("baseAmount", row.get("base_amount")), "baseAmount"),
            "penalty_amount": _decimal(row.get("penaltyAmount", row.get("penalty_amount")), "penaltyAmount"),
            "paid_amount": _decimal(row.get("paidAmount", row.get("paid_amount")), "paidAmount"),
        }, counts, "installments")

    for row in payments:
        local_id, debt_local_id = _local_id(row), row.get("debtLocalId", row.get("debt_id"))
        debt = debt_map.get(str(debt_local_id))
        if not debt:
            raise ValidationError({"snapshot": f"Payment {local_id} references a missing debtLocalId."})
        installment_local_id = row.get("installmentLocalId", row.get("installment_id"))
        installment = installment_map.get(str(installment_local_id)) if installment_local_id is not None else None
        if installment and installment.debt_id != debt.id:
            raise ValidationError({"snapshot": f"Payment {local_id} references an installment from another debt."})
        payment_type = row.get("type") or Payment.PaymentType.PRINCIPAL
        if payment_type not in Payment.PaymentType.values:
            raise ValidationError({"snapshot": f"Payment {local_id} has an unsupported type."})
        _upsert(Payment, device, local_id, {
            "owner": user, "debt": debt, "client": debt.client, "installment": installment,
            "amount": _decimal(row.get("amount"), "amount"), "note": str(row.get("note") or ""),
            "payment_type": payment_type, "payment_date": _date(row.get("createdAt", row.get("created_at")), "createdAt"),
        }, counts, "payments")

    # Reconcile derived ledger values only after all snapshot relationships exist.
    for local_id, debt in debt_map.items():
        source = next(row for row in debts if _local_id(row) == local_id)
        is_paid = bool(source.get("isPaid", source.get("is_paid", False)))
        items = list(debt.installments.all())
        if debt.loan_type == Debt.LoanType.MULTI:
            debt.outstanding = money(0 if is_paid else sum((max(item.amount - item.paid_amount, 0) for item in items), Decimal("0")))
            debt.collected = money(sum((item.paid_amount for item in items), Decimal("0")))
        else:
            interest_paid = sum((item.paid_amount for item in items), Decimal("0"))
            principal_paid = sum((payment.amount for payment in debt.payments.filter(installment__isnull=True, payment_type=Payment.PaymentType.PRINCIPAL, reversed_at__isnull=True)), Decimal("0"))
            debt.collected = money(interest_paid + principal_paid)
            open_interest = sum((max(item.amount - item.paid_amount, 0) for item in items), Decimal("0"))
            debt.outstanding = money(0 if is_paid else debt.capital_remaining + open_interest)
        debt.status = _status_for_snapshot(debt, is_paid)
        debt.save(update_fields=["outstanding", "collected", "status", "updated_at"])

    device.last_synced_at = timezone.now()
    device.save(update_fields=["last_synced_at", "updated_at"])
    MobileSyncBatch.objects.create(device=device, batch_id=batch_id, payload_hash=payload_digest, counts=counts)
    return counts, False
