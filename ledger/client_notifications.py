"""Durable lifecycle and reminder emails to clients with recorded addresses.

Model signals queue events from web and mobile writes. The notification worker
delivers them after commit and retries failures without blocking ledger writes.
"""

import hashlib
import json
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.db.models import F, Q
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from .models import Client, Debt, Payment, PushDelivery

logger = logging.getLogger(__name__)
_payment_batch = ContextVar("pingo_payment_email_batch", default=None)


@contextmanager
def combine_payment_emails():
    """Combine newly imported payment rows per debt in one mobile sync action."""
    token = _payment_batch.set(str(uuid.uuid4()))
    try:
        yield
    finally:
        _payment_batch.reset(token)


def send_to_client(client, subject, message):
    """Every client notification funnels through here so the "has an email"
    gate and error handling live in exactly one place."""
    if not client.email:
        return False
    try:
        return bool(send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[client.email],
            fail_silently=False,
        ))
    except Exception:
        logger.exception("Failed to send client notification for %s", client.public_id)
        return False


def notify_client_added(client):
    return send_to_client(client, "Welcome to Pingo", (
        f"Hello {client.name},\n\n"
        "Your client profile has been added to Pingo. "
        "You will receive updates when a loan is recorded, a payment is received, "
        "a loan is fully paid, or a payment becomes overdue.\n\n"
        "If you have questions, please contact your lender directly."
    ))


def notify_debt_created(debt, original_total=None):
    return send_to_client(
        debt.client,
        subject="A new loan has been recorded for you",
        message=(
            f"Hello {debt.client.name},\n\n"
            f"A new loan of {debt.principal:.2f} has been recorded in your name "
            f"(reference {debt.reference}).\n"
            f"Total to repay: {Decimal(original_total) if original_total is not None else debt.total:.2f}, due by {debt.due_date.isoformat()}.\n\n"
            "If you have questions about this, please contact us directly."
        ),
    )


def notify_payment_received(debt, payments, fully_paid=False):
    total = sum((payment.amount for payment in payments), Decimal("0"))
    confirmation = "Your loan has been fully paid. There is no remaining balance.\n" if fully_paid else ""
    details = ""
    if len(payments) > 1:
        details = "Payment breakdown:\n" + "".join(
            f"- {payment.get_payment_type_display()}"
            f"{f' (installment {payment.installment.number})' if payment.installment_id else ''}: {payment.amount:.2f}\n"
            for payment in payments
        )
    return send_to_client(
        debt.client,
        subject="Loan fully paid" if fully_paid else "Payment received",
        message=(
            f"Hello {debt.client.name},\n\n"
            f"We've recorded a payment of {total:.2f} on your loan {debt.reference}.\n"
            f"{details}"
            f"Remaining balance: {debt.outstanding:.2f}.\n"
            f"{confirmation}\n"
            "Thank you."
        ),
    )


def notify_debt_paid(debt, payments):
    if payments:
        return notify_payment_received(debt, payments, fully_paid=True)
    return send_to_client(debt.client, "Loan fully paid", (
        f"Hello {debt.client.name},\n\n"
        f"Your loan {debt.reference} has been fully paid. "
        "There is no remaining balance on this loan.\n\nThank you."
    ))


def notify_debt_overdue(debt, today):
    periods = list(debt.installments.filter(due_date__lt=today, paid_amount__lt=F("amount")))
    if not periods or debt.outstanding <= 0:
        return None
    due = sum(item.amount - item.paid_amount for item in periods)
    due_date = min(item.due_date for item in periods)
    return send_to_client(debt.client, "Payment overdue", (
        f"Hello {debt.client.name},\n\n"
        f"Your loan {debt.reference} has an overdue payment balance of {due:.2f}, "
        f"with payments due since {due_date.isoformat()}.\n"
        f"Total remaining loan balance: {debt.outstanding:.2f}.\n\n"
        "Please contact your lender to arrange payment. "
        "We will send another reminder in 30 days if the loan remains overdue."
    ))


def _settlement(debt):
    facts = list(debt.payments.filter(reversed_at__isnull=True).order_by("public_id")
                 .values_list("public_id", "operation_id", "amount"))
    return hashlib.sha256(json.dumps(facts, default=str).encode()).hexdigest()[:24]


def queue_client_email(client, kind, event_key, **details):
    if not client.email:
        return None
    # Reuse the durable delivery ledger already used for client reminder emails.
    # Signals run within the model save's transaction, so rollback also removes
    # the queued mail. The worker performs SMTP after ledger changes commit.
    event, _ = PushDelivery.objects.get_or_create(owner=client.owner, event_key=event_key,
        defaults={"payload": {"emailStatus": "pending", "kind": kind,
                             "clientId": str(client.public_id), **details}})
    return event


@receiver(post_save, sender=Client)
def queue_client_added(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        queue_client_email(instance, "client_added", f"client:{instance.public_id}:added")


@receiver(post_save, sender=Debt)
def queue_debt_event(sender, instance, created, raw=False, **kwargs):
    if raw or not instance.client.email:
        return
    if created:
        queue_client_email(instance.client, "debt_created", f"client:debt:{instance.public_id}:created",
                           debtId=str(instance.public_id), originalTotal=str(instance.total))
    if instance.status == Debt.Status.PAID and instance.outstanding <= 0:
        settlement = _settlement(instance)
        payments = _pending_payment_ids(instance)
        queue_client_email(instance.client, "debt_paid", f"client:debt:{instance.public_id}:paid:{settlement}",
                           debtId=str(instance.public_id), settlement=settlement, paymentIds=payments)


@receiver(post_save, sender=Payment)
def queue_payment_event(sender, instance, created, raw=False, **kwargs):
    if created and not raw and instance.reversed_at is None:
        group = _payment_batch.get() or str(instance.operation_id)
        event = queue_client_email(instance.client, "payment_received",
            f"client:debt:{instance.debt.public_id}:payment:{group}",
            debtId=str(instance.debt.public_id), paymentIds=[])
        if event:
            event.payload["paymentIds"].append(str(instance.public_id))
            event.save(update_fields=["payload", "updated_at"])


def _receipt_payments(debt, payload):
    active = debt.payments.filter(reversed_at__isnull=True).select_related("installment")
    if payload.get("paymentIds"):
        return list(active.filter(public_id__in=payload["paymentIds"]).order_by("id"))
    # Queued events from the previous worker used one event per payment row.
    # Resolve their shared operation so an upgrade also combines pending mail.
    payment = active.filter(public_id=payload.get("paymentId")).first()
    if payment:
        return list(active.filter(operation_id=payment.operation_id).order_by("id"))
    if payload["kind"] == "debt_paid":
        pending_ids = _pending_payment_ids(debt)
        if pending_ids:
            return list(active.filter(public_id__in=pending_ids).order_by("id"))
        latest = active.order_by("-created_at", "-id").first()
        return list(active.filter(operation_id=latest.operation_id).order_by("id")) if latest else []
    return []


def _pending_payment_ids(debt):
    receipts = PushDelivery.objects.filter(owner=debt.client.owner, payload__kind="payment_received",
        payload__debtId=str(debt.public_id), payload__emailStatus="pending")
    ids = set()
    for receipt in receipts:
        ids.update(receipt.payload.get("paymentIds", [receipt.payload.get("paymentId")]))
    return sorted(str(item) for item in ids if item)


def _deliver(event, today):
    payload = event.payload
    client = Client.objects.filter(public_id=payload["clientId"], owner=event.owner).first()
    if not client or not client.email:
        return None
    kind = payload["kind"]
    if kind == "client_added":
        return notify_client_added(client)
    debt = Debt.objects.filter(public_id=payload.get("debtId"), client=client).first()
    if not debt:
        return None
    if kind == "debt_created":
        return notify_debt_created(debt, payload.get("originalTotal"))
    if kind == "debt_paid":
        if debt.status != Debt.Status.PAID or debt.outstanding > 0 or _settlement(debt) != payload["settlement"]:
            return None
        return notify_debt_paid(debt, _receipt_payments(debt, payload))
    if kind == "payment_received":
        payments = _receipt_payments(debt, payload)
        # The settlement email replaces individual receipts for a fully paid
        # loan, including payments allocated across multiple installments.
        if not payments or debt.status == Debt.Status.PAID:
            return None
        if payload.get("paymentId") and str(payments[0].public_id) != payload["paymentId"]:
            return None
        return notify_payment_received(debt, payments)
    if kind == "due_tomorrow":
        installment = debt.installments.filter(public_id=payload["installmentId"], due_date=today + timedelta(days=1),
                                              paid_amount__lt=F("amount")).first()
        return notify_installment_due_tomorrow(debt, installment) if installment else None
    if kind == "overdue":
        return notify_debt_overdue(debt, today)
    return None


def send_client_emails(today=None, limit=100):
    today = today or timezone.localdate()
    sent = 0
    now = timezone.now()
    pending = PushDelivery.objects.filter(Q(payload__emailStatus="pending") | Q(payload__emailStatus="sending")).order_by("id")
    for event in list(pending[:limit]):
        original = event.payload
        if original["emailStatus"] == "sending":
            attempted = parse_datetime(original.get("attemptedAt", ""))
            if attempted and now - attempted < timedelta(minutes=5):
                continue
        claimed = {**original, "emailStatus": "sending", "attemptedAt": now.isoformat()}
        # One SQL compare-and-set claims the event without holding a database
        # transaction open across SMTP. A crashed worker's lease expires.
        if not PushDelivery.objects.filter(pk=event.pk, payload=original).update(payload=claimed):
            continue
        try:
            result = _deliver(event, today)
        except Exception:
            logger.exception("Failed to process client email event %s", event.public_id)
            result = False
        status = "sent" if result else "cancelled" if result is None else "pending"
        finished = {**claimed, "emailStatus": status}
        if result:
            finished["sentOn"] = today.isoformat()
            sent += 1
        PushDelivery.objects.filter(pk=event.pk, payload=claimed).update(payload=finished)
    return sent


def queue_overdue_reminder(debt, today):
    if not debt.client.email:
        return
    owner = debt.client.owner
    prefix = f"client:debt:{debt.public_id}:overdue:"
    events = list(PushDelivery.objects.filter(owner=owner, event_key__startswith=prefix))
    if any(event.payload.get("emailStatus") in {"pending", "sending"} for event in events):
        return
    sent_dates = [parse_date(event.payload.get("sentOn", "")) for event in events
                  if event.payload.get("emailStatus") == "sent"]
    # Respect client reminders delivered by the previous daily email worker,
    # so upgrading it does not immediately send another overdue email.
    legacy = PushDelivery.objects.filter(owner=debt.owner, event_key__startswith="client:installment:",
                                        event_key__contains=":overdue:", payload__debtId=debt.reference)
    sent_dates.extend(parse_date(event.event_key.rsplit(":", 1)[-1]) for event in legacy)
    latest = max((day for day in sent_dates if day), default=None)
    if latest and (today - latest).days < 30:
        return
    queue_client_email(debt.client, "overdue", f"{prefix}{today.isoformat()}", debtId=str(debt.public_id))


def notify_installment_due_tomorrow(debt, installment):
    return send_to_client(
        debt.client,
        subject="Payment due tomorrow",
        message=(
            f"Hello {debt.client.name},\n\n"
            f"A payment of {installment.amount - installment.paid_amount:.2f} on loan {debt.reference} "
            f"is due tomorrow ({installment.due_date.isoformat()}).\n\n"
            "Please make sure your payment is settled on time."
        ),
    )
