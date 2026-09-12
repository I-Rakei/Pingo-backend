import base64
import json
from datetime import timedelta
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from django.conf import settings
from django.db.models import F
from django.utils import timezone
from pywebpush import WebPushException, webpush

from .models import Installment, Preference, PushDelivery, WebPushSubscription


def vapid_private_key_path():
    path = Path(settings.VAPID_PRIVATE_KEY)
    return path if path.is_absolute() else settings.BASE_DIR / path


def get_vapid_public_key():
    path = vapid_private_key_path()
    if not path.is_file():
        return ""
    private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")


def send_push_to_user(user, payload):
    private_key = vapid_private_key_path()
    if not private_key.is_file():
        return {"sent": 0, "failed": 0, "stale": 0, "configured": False}

    result = {"sent": 0, "failed": 0, "stale": 0, "configured": True}
    for subscription in list(WebPushSubscription.objects.filter(owner=user)):
        try:
            webpush(
                subscription_info={
                    "endpoint": subscription.endpoint,
                    "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
                },
                data=json.dumps(payload),
                vapid_private_key=str(private_key),
                vapid_claims={"sub": settings.VAPID_SUBJECT},
                ttl=3600,
                timeout=10,
            )
            result["sent"] += 1
        except WebPushException as exc:
            status_code = getattr(exc.response, "status_code", None)
            if status_code in (404, 410):
                subscription.delete()
                result["stale"] += 1
            else:
                result["failed"] += 1
        except Exception:
            result["failed"] += 1
    return result


def send_due_notifications(today=None):
    today = today or timezone.localdate()
    tomorrow = today + timedelta(days=1)
    totals = {"events": 0, "sent": 0, "failed": 0, "stale": 0}
    installments = Installment.objects.filter(paid_amount__lt=F("amount")).select_related(
        "debt__owner", "debt__client"
    )

    for installment in installments:
        debt = installment.debt
        user = debt.owner
        if not WebPushSubscription.objects.filter(owner=user).exists():
            continue
        preference, _ = Preference.objects.get_or_create(owner=user)
        event_key = None
        title = None
        body = None
        if installment.due_date == tomorrow and preference.reminders:
            event_key = f"installment:{installment.public_id}:tomorrow"
            title = "Payment due tomorrow"
            body = f"{debt.client.name} owes {installment.amount - installment.paid_amount:.2f} {preference.currency} tomorrow."
        elif installment.due_date < today and preference.overdue_alerts:
            event_key = f"installment:{installment.public_id}:overdue:{today.isoformat()}"
            title = "Payment overdue"
            body = f"{debt.client.name} has an overdue balance of {installment.amount - installment.paid_amount:.2f} {preference.currency}."
        if not event_key or PushDelivery.objects.filter(owner=user, event_key=event_key).exists():
            continue

        payload = {
            "title": title,
            "body": body,
            "url": f"/?debt={debt.reference}",
            "tag": event_key,
            "debtId": debt.reference,
        }
        result = send_push_to_user(user, payload)
        totals["sent"] += result["sent"]
        totals["failed"] += result["failed"]
        totals["stale"] += result["stale"]
        if result["sent"]:
            PushDelivery.objects.create(owner=user, event_key=event_key, payload=payload)
            totals["events"] += 1
    return totals
