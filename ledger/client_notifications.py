"""Email notifications sent to a Client (not the account owner) about their
own debts. Kept separate from push.py, which delivers owner-facing browser
push, and from services.py, which is pure financial logic.

v1 scope: fires automatically whenever Client.email is set -- no separate
opt-in field. The account owner already controls whether a client's email is
recorded at all, so recording it is treated as consent to use it for these
notifications. If that needs to become an explicit toggle later, add a
Client.email_notifications_enabled field and gate send_to_client() on it.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def send_to_client(client, subject, message):
    """Every client notification funnels through here so the "has an email"
    gate and error handling live in exactly one place."""
    if not client.email:
        return False
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[client.email],
            fail_silently=True,  # a broken relay must never break debt/payment recording
        )
        return True
    except Exception:  # pragma: no cover - defensive: fail_silently should already catch send errors
        logger.exception("Failed to send client notification to %s", client.email)
        return False


def notify_debt_created(debt):
    return send_to_client(
        debt.client,
        subject="A new loan has been recorded for you",
        message=(
            f"Hello {debt.client.name},\n\n"
            f"A new loan of {debt.principal:.2f} has been recorded in your name "
            f"(reference {debt.reference}).\n"
            f"Total to repay: {debt.total:.2f}, due by {debt.due_date.isoformat()}.\n\n"
            "If you have questions about this, please contact us directly."
        ),
    )


def notify_payment_received(debt, payment):
    return send_to_client(
        debt.client,
        subject="Payment received",
        message=(
            f"Hello {debt.client.name},\n\n"
            f"We've recorded a payment of {payment.amount:.2f} on your loan {debt.reference}.\n"
            f"Remaining balance: {debt.outstanding:.2f}.\n\n"
            "Thank you."
        ),
    )


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


def notify_installment_overdue(debt, installment):
    return send_to_client(
        debt.client,
        subject="Payment overdue",
        message=(
            f"Hello {debt.client.name},\n\n"
            f"Your payment of {installment.amount - installment.paid_amount:.2f} on loan {debt.reference} "
            f"was due on {installment.due_date.isoformat()} and is now overdue.\n\n"
            "Please settle this as soon as possible."
        ),
    )
