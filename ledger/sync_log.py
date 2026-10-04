"""Append-only, scope-filtered v2 ledger feed."""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timezone as utc_timezone

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver
from rest_framework.authtoken.models import Token

from .models import Client, Debt, Installment, OrganizationMembership, Payment, SyncChange

logger = logging.getLogger(__name__)
_last_broadcast_error = 0.0
SYNC_MODELS = {Client: "client", Debt: "debt", Installment: "installment", Payment: "payment"}
_origin = ContextVar("pingo_sync_origin", default=(None, None))


@contextmanager
def sync_origin(device=None, mutation=None):
    token = _origin.set((device, mutation))
    try:
        yield
    finally:
        _origin.reset(token)


def scope_ids(instance):
    if isinstance(instance, Installment):
        debt = instance.debt
        organization_id, owner_id = debt.organization_id, debt.owner_id
    else:
        organization_id, owner_id = instance.organization_id, instance.owner_id
    return {"owner_id": None if organization_id else owner_id, "organization_id": organization_id}


def _decimal(value):
    return f"{value:.2f}"


def _timestamp(value):
    return value.astimezone(utc_timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _date(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)[:10]


def wire_fields(instance):
    if isinstance(instance, Client):
        return {"name": instance.name, "phone": instance.phone, "email": instance.email,
                "address": instance.address, "notes": instance.notes}
    if isinstance(instance, Debt):
        return {"clientId": str(instance.client.public_id), "reference": instance.reference,
                "loanType": instance.loan_type, "principal": _decimal(instance.principal),
                "interestRate": _decimal(instance.interest_rate), "penaltyRate": _decimal(instance.penalty_rate),
                "durationMonths": instance.duration_months, "startDate": _date(instance.start_date),
                "dueDate": _date(instance.due_date), "capitalRemaining": _decimal(instance.capital_remaining),
                "total": _decimal(instance.total), "collected": _decimal(instance.collected),
                "outstanding": _decimal(instance.outstanding), "status": instance.status}
    if isinstance(instance, Installment):
        return {"debtId": str(instance.debt.public_id), "number": instance.number,
                "dueDate": _date(instance.due_date), "baseAmount": _decimal(instance.base_amount),
                "penaltyAmount": _decimal(instance.penalty_amount), "amount": _decimal(instance.amount),
                "paidAmount": _decimal(instance.paid_amount)}
    if isinstance(instance, Payment):
        return {"debtId": str(instance.debt.public_id),
                "installmentId": str(instance.installment.public_id) if instance.installment_id else None,
                "operationId": str(instance.operation_id), "amount": _decimal(instance.amount),
                "type": instance.payment_type, "date": _date(instance.payment_date),
                "note": instance.note, "reversedAt": _timestamp(instance.reversed_at)}
    raise TypeError("Unsupported sync entity")


def canonical_row(instance):
    return {"entity": SYNC_MODELS[type(instance)], "id": str(instance.public_id),
            "revision": instance.revision, "fields": wire_fields(instance)}


def notify_scope(scope, first_id, last_id):
    """Phase 2 installs the channel layer; notification failure never rolls back a write."""
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
    except ImportError:
        return
    try:
        layer = get_channel_layer()
        if layer is None:
            return
        group = f"ledger.org.{scope['organization_id']}" if scope["organization_id"] else f"ledger.user.{scope['owner_id']}"
        async_to_sync(layer.group_send)(group, {"type": "ledger.changed", "from": first_id, "to": last_id})
    except Exception as exc:
        _log_broadcast_error(exc)


def _log_broadcast_error(exc):
    # Redis outages must never fail an HTTP write. One warning per minute keeps
    # a bulk import or test run from producing thousands of identical traces.
    global _last_broadcast_error
    now = time.monotonic()
    if now - _last_broadcast_error > 60:
        logger.warning("Could not broadcast realtime event: %s", exc)
        _last_broadcast_error = now


def notify_auth_revoked(user_id):
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is not None:
            async_to_sync(layer.group_send)(f"auth.user.{user_id}", {"type": "auth.revoked"})
    except Exception as exc:
        _log_broadcast_error(exc)


@receiver(post_delete, sender=Token)
@receiver(post_delete, sender=OrganizationMembership)
def revoke_deleted_credential(sender, instance, **kwargs):
    transaction.on_commit(lambda: notify_auth_revoked(instance.user_id))


@receiver(pre_save, sender=Client)
@receiver(pre_save, sender=Debt)
@receiver(pre_save, sender=Installment)
@receiver(pre_save, sender=Payment)
def remember_old_fields(sender, instance, **kwargs):
    old = sender.objects.filter(pk=instance.pk).first() if instance.pk else None
    instance._sync_old_fields = wire_fields(old) if old else None


@receiver(pre_delete, sender=Installment)
def remember_installment_scope(sender, instance, origin=None, **kwargs):
    instance._sync_scope_ids = scope_ids(instance)
    # Kept on the tombstone so structural-conflict checks can find deleted periods.
    instance._sync_debt_public_id = str(instance.debt.public_id)
    # SET_NULL cascades use QuerySet.update() internally and skip Payment's
    # signals. Make that relationship change explicit for a direct period
    # delete. A debt cascade deletes those payments, so only tombstones matter.
    deleting_debt = isinstance(origin, Debt) or getattr(origin, "model", None) is Debt
    if not deleting_debt:
        for payment in instance.payments.all():
            payment.installment = None
            payment.save(update_fields=["installment", "updated_at"])


@receiver(post_save, sender=Client)
@receiver(post_save, sender=Debt)
@receiver(post_save, sender=Installment)
@receiver(post_save, sender=Payment)
def append_saved_change(sender, instance, created, **kwargs):
    fields = wire_fields(instance)
    old = getattr(instance, "_sync_old_fields", None)
    changed = list(fields) if created or old is None else [key for key, value in fields.items() if old.get(key) != value]
    scope = scope_ids(instance)
    device, mutation = _origin.get()
    change = SyncChange.objects.create(**scope, entity=SYNC_MODELS[sender], entity_id=instance.public_id,
                                       op="upsert", fields=fields, changed_fields=changed,
                                       origin_device=device, mutation=mutation)
    sender.objects.filter(pk=instance.pk).update(revision=change.pk)
    instance.revision = change.pk
    transaction.on_commit(lambda: notify_scope(scope, change.pk, change.pk))


@receiver(post_delete, sender=Client)
@receiver(post_delete, sender=Debt)
@receiver(post_delete, sender=Installment)
@receiver(post_delete, sender=Payment)
def append_deleted_change(sender, instance, **kwargs):
    scope = getattr(instance, "_sync_scope_ids", None) or scope_ids(instance)
    device, mutation = _origin.get()
    debt_id = getattr(instance, "_sync_debt_public_id", None)
    change = SyncChange.objects.create(**scope, entity=SYNC_MODELS[sender], entity_id=instance.public_id,
                                       op="delete", fields={"debtId": debt_id} if debt_id else {}, changed_fields=[],
                                       origin_device=device, mutation=mutation)
    transaction.on_commit(lambda: notify_scope(scope, change.pk, change.pk))
