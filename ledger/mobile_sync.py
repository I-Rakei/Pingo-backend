"""Import a Dividas SQLite snapshot into an authenticated Pingo account."""

import hashlib
import json
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
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


def _owned_queryset(model, user):
    if model is Installment:
        return model.objects.filter(debt__owner=user)
    return model.objects.filter(owner=user)


def _existing_for_row(model, user, device, local_id, row):
    server_id = row.get("serverId")
    if server_id:
        try:
            instance = _owned_queryset(model, user).filter(public_id=server_id).first()
        except (DjangoValidationError, ValueError, TypeError):
            instance = None
        if not instance:
            raise ValidationError({"snapshot": f"{model.__name__} serverId is invalid or no longer exists."})
        return instance
    return model.objects.filter(mobile_device=device, mobile_local_id=local_id).first()


def _mobile_is_newer(instance, row):
    value = row.get("updatedAt")
    if not value:
        return True
    parsed = parse_datetime(str(value))
    if not parsed:
        return True
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed > instance.updated_at


def _upsert(model, user, device, local_id, row, defaults, counts, bucket):
    instance = _existing_for_row(model, user, device, local_id, row)
    if instance and row.get("serverId") and not _mobile_is_newer(instance, row):
        return instance
    created = instance is None
    if created:
        instance = model(mobile_device=device, mobile_local_id=local_id, **defaults)
    else:
        for field, value in defaults.items():
            setattr(instance, field, value)
    instance.save()
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


def _apply_deletions(user, deletions):
    model_map = {
        "payment": Payment,
        "installment": Installment,
        "debt": Debt,
        "client": Client,
    }
    order = {"payment": 0, "installment": 1, "debt": 2, "client": 3}
    deleted = {name: set() for name in model_map}
    for item in sorted(deletions, key=lambda value: order.get(value.get("entity"), 99)):
        entity, server_id = item.get("entity"), item.get("serverId")
        model = model_map.get(entity)
        if not model or not server_id:
            continue
        try:
            deleted_count, _ = _owned_queryset(model, user).filter(public_id=server_id).delete()
            if deleted_count:
                deleted[entity].add(str(server_id))
        except DjangoValidationError:
            raise ValidationError({"snapshot": f"Invalid deleted {entity} serverId."})
        except ProtectedError as exc:
            raise ValidationError({
                "snapshot": f"Cannot delete this {entity} while linked records still exist."
            }) from exc
    return deleted


def snapshot_for_user(user, device=None):
    clients = list(Client.objects.filter(owner=user))
    debts = list(Debt.objects.filter(owner=user).select_related("client"))
    installments = list(Installment.objects.filter(debt__owner=user).select_related("debt"))
    payments = list(Payment.objects.filter(owner=user, reversed_at__isnull=True).select_related("debt", "installment"))

    def local_id(instance):
        return instance.mobile_local_id if device and instance.mobile_device_id == device.id else None

    return {
        "clients": [{
            "serverId": str(client.public_id), "localId": local_id(client), "name": client.name,
            "phone": client.phone, "email": client.email, "address": client.address, "notes": client.notes,
            "createdAt": client.created_at.isoformat(), "updatedAt": client.updated_at.isoformat(),
        } for client in clients],
        "debts": [{
            "serverId": str(debt.public_id), "localId": local_id(debt),
            "clientServerId": str(debt.client.public_id), "debtorName": debt.client.name,
            "amount": str(debt.principal), "interestRate": str(debt.interest_rate),
            "durationMonths": debt.duration_months, "penaltyRate": str(debt.penalty_rate),
            "totalAmount": str(debt.total), "dueDate": debt.due_date.isoformat(),
            "startDate": debt.start_date.isoformat(), "isPaid": debt.outstanding <= 0,
            "loanType": debt.loan_type, "capitalRemaining": str(debt.capital_remaining),
            "reference": debt.reference, "createdAt": debt.created_at.isoformat(),
            "updatedAt": debt.updated_at.isoformat(),
        } for debt in debts],
        "installments": [{
            "serverId": str(item.public_id), "localId": local_id(item),
            "debtServerId": str(item.debt.public_id), "installmentNumber": item.number,
            "baseAmount": str(item.base_amount), "penaltyAmount": str(item.penalty_amount),
            "totalAmount": str(item.amount), "dueDate": item.due_date.isoformat(),
            "isPaid": item.paid_amount >= item.amount, "paidAmount": str(item.paid_amount),
            "createdAt": item.created_at.isoformat(), "updatedAt": item.updated_at.isoformat(),
        } for item in installments],
        "payments": [{
            "serverId": str(payment.public_id), "localId": local_id(payment),
            "debtServerId": str(payment.debt.public_id),
            "installmentServerId": str(payment.installment.public_id) if payment.installment else None,
            "amount": str(payment.amount), "note": payment.note, "type": payment.payment_type,
            "createdAt": payment.payment_date.isoformat(), "updatedAt": payment.updated_at.isoformat(),
        } for payment in payments],
    }


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
        client_map[local_id] = _upsert(Client, user, device, local_id, row, {
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
        existing = _existing_for_row(Debt, user, device, local_id, row)
        reference = existing.reference if existing else next_reference()
        total = _decimal(row.get("totalAmount", row.get("total_amount")), "totalAmount")
        debt = _upsert(Debt, user, device, local_id, row, {
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
        existing_installment = _existing_for_row(Installment, user, device, local_id, row)
        conflicts = Installment.objects.filter(debt=debt, number=number)
        if existing_installment:
            conflicts = conflicts.exclude(pk=existing_installment.pk)
        conflict = conflicts.exists()
        if conflict:
            raise ValidationError({"snapshot": f"Installment {local_id} conflicts with an existing period number."})
        installment_map[local_id] = _upsert(Installment, user, device, local_id, row, {
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
        _upsert(Payment, user, device, local_id, row, {
            "owner": user, "debt": debt, "client": debt.client, "installment": installment,
            "amount": _decimal(row.get("amount"), "amount"), "note": str(row.get("note") or ""),
            "payment_type": payment_type, "payment_date": _date(row.get("createdAt", row.get("created_at")), "createdAt"),
        }, counts, "payments")

    deleted = _apply_deletions(user, snapshot.get("deletions", []))

    # Reconcile derived ledger values only after all snapshot relationships exist.
    for local_id, debt in debt_map.items():
        if str(debt.public_id) in deleted["debt"]:
            continue
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
