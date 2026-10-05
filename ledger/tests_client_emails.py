import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from .client_notifications import queue_client_email, send_client_emails
from .mobile_sync import snapshot_for_user, sync_snapshot
from .models import Client, Debt, MobileDevice, Preference, PushDelivery
from .push import send_due_notifications
from .services import create_debt, record_payment, revert_installment
from .sync_v2 import apply_mutation, bind_device


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ClientEmailTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="email-owner", email="owner@example.test")
        self.today = timezone.localdate()
        self.client_row = Client.objects.create(owner=self.user, name="Ana", email="ana@example.test")

    def debt(self, **changes):
        values = {"clientId": self.client_row.pk, "loanType": "multi", "principal": Decimal("100"),
                  "interestRate": Decimal("0"), "penaltyRate": Decimal("0"), "durationMonths": 1,
                  "startDate": self.today, "dueDate": self.today + timedelta(days=30)}
        values.update(changes)
        return create_debt(self.user, values)

    def drain(self):
        send_client_emails()
        mail.outbox.clear()

    def test_welcome_is_queued_once_and_sent_to_the_client(self):
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(send_client_emails(), 1)
        self.assertEqual(mail.outbox[0].subject, "Welcome to Pingo")
        self.assertEqual(mail.outbox[0].to, ["ana@example.test"])
        self.client_row.name = "Ana Updated"
        self.client_row.save()
        self.assertEqual(send_client_emails(), 0)

    def test_debt_created_partial_payment_and_full_settlement(self):
        self.drain()
        debt = self.debt(durationMonths=2)
        self.assertEqual(send_client_emails(), 1)
        self.assertIn("new loan", mail.outbox[0].subject.lower())
        self.assertIn(debt.reference, mail.outbox[0].body)
        mail.outbox.clear()
        record_payment(self.user, debt.reference, {"action": "balance", "amount": "25"})
        self.assertEqual(send_client_emails(), 1)
        self.assertEqual(mail.outbox[0].subject, "Payment received")
        self.assertIn("75.00", mail.outbox[0].body)
        mail.outbox.clear()
        record_payment(self.user, debt.reference, {"action": "balance", "amount": "75"})
        self.assertEqual(send_client_emails(), 1, "one full-payment email, not one per installment allocation")
        self.assertEqual(mail.outbox[0].subject, "Loan fully paid")
        self.assertEqual(send_client_emails(), 0)

    def test_repaid_loan_gets_a_new_settlement_email(self):
        debt = self.debt()
        self.drain()
        record_payment(self.user, debt.reference, {"action": "balance", "amount": "100"})
        self.drain()
        revert_installment(self.user, debt.reference, 1)
        record_payment(self.user, debt.reference, {"action": "balance", "amount": "100"})
        self.assertEqual(send_client_emails(), 1)
        self.assertEqual(mail.outbox[0].subject, "Loan fully paid")

    def test_overdue_reminder_is_per_debt_every_30_days_and_stops_when_paid(self):
        Preference.objects.create(owner=self.user, reminders=False, overdue_alerts=False)
        debt = self.debt(durationMonths=2, startDate=self.today - timedelta(days=90),
                         dueDate=self.today - timedelta(days=1))
        self.drain()
        self.assertEqual(send_due_notifications(today=self.today)["clientEmails"], 1)
        self.assertEqual(mail.outbox[0].subject, "Payment overdue")
        self.assertIn("100.00", mail.outbox[0].body)
        mail.outbox.clear()
        for days in (0, 1, 29):
            self.assertEqual(send_due_notifications(today=self.today + timedelta(days=days))["clientEmails"], 0)
        self.assertEqual(send_due_notifications(today=self.today + timedelta(days=30))["clientEmails"], 1)
        self.assertEqual(send_due_notifications(today=self.today + timedelta(days=31))["clientEmails"], 0)
        record_payment(self.user, debt.reference, {"action": "balance", "amount": "100"})
        self.drain()
        self.assertEqual(send_due_notifications(today=self.today + timedelta(days=60))["clientEmails"], 0)

    def test_failed_email_remains_pending_and_can_be_retried(self):
        with patch("ledger.client_notifications.send_mail", return_value=0):
            self.assertEqual(send_client_emails(), 0)
        self.assertEqual(PushDelivery.objects.get().payload["emailStatus"], "pending")
        self.assertEqual(send_client_emails(), 1)
        self.assertEqual(PushDelivery.objects.get().payload["emailStatus"], "sent")
        self.assertEqual(send_client_emails(), 0)

    def test_smtp_failure_does_not_fail_ledger_writes_or_consume_the_monthly_reminder(self):
        debt = self.debt(startDate=self.today - timedelta(days=10), dueDate=self.today - timedelta(days=1))
        self.drain()
        with self.assertLogs("ledger.client_notifications", level="ERROR"), \
                patch("ledger.client_notifications.send_mail", side_effect=OSError("test relay unavailable")):
            self.assertEqual(send_due_notifications(today=self.today)["clientEmails"], 0)
        self.assertTrue(Debt.objects.filter(pk=debt.pk).exists())
        self.assertEqual(send_due_notifications(today=self.today + timedelta(days=1))["clientEmails"], 1)
        self.assertEqual(send_due_notifications(today=self.today + timedelta(days=30))["clientEmails"], 0)
        self.assertEqual(send_due_notifications(today=self.today + timedelta(days=31))["clientEmails"], 1)

    def test_rollback_and_deleted_records_do_not_send_email(self):
        self.drain()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                Client.objects.create(owner=self.user, name="Rolled back", email="rollback@example.test")
                raise RuntimeError("rollback")
        self.assertEqual(send_client_emails(), 0)
        another = Client.objects.create(owner=self.user, name="Deleted", email="deleted@example.test")
        another.delete()
        self.assertEqual(send_client_emails(), 0)
        self.assertFalse(mail.outbox)

    def test_no_email_address_and_current_worker_claim_are_respected(self):
        blank = Client.objects.create(owner=self.user, name="No address")
        self.assertIsNone(queue_client_email(blank, "client_added", "blank-client"))
        event = PushDelivery.objects.get()
        event.payload = {**event.payload, "emailStatus": "sending", "attemptedAt": timezone.now().isoformat()}
        event.save()
        self.assertEqual(send_client_emails(), 0)
        event.payload["attemptedAt"] = (timezone.now() - timedelta(minutes=6)).isoformat()
        event.save()
        self.assertEqual(send_client_emails(), 1)

    def test_legacy_daily_reminder_respects_the_new_30_day_window(self):
        debt = self.debt(startDate=self.today - timedelta(days=10), dueDate=self.today - timedelta(days=1))
        self.drain()
        old_date = self.today - timedelta(days=1)
        PushDelivery.objects.create(owner=self.user,
            event_key=f"client:installment:{debt.installments.get().public_id}:overdue:{old_date.isoformat()}",
            payload={"clientEmail": self.client_row.email, "debtId": debt.reference})
        self.assertEqual(send_due_notifications(today=self.today)["clientEmails"], 0)
        self.assertEqual(send_due_notifications(today=old_date + timedelta(days=30))["clientEmails"], 1)

    def test_mobile_v1_imports_and_repeated_snapshots_do_not_duplicate_emails(self):
        device = MobileDevice.objects.create(owner=self.user, device_id="email-v1-phone")
        snapshot = {"clients": [{"localId": "1", "name": "Mobile Ana", "email": "mobile@example.test"}],
                    "debts": [{"localId": "1", "clientLocalId": "1", "loanType": "multi", "amount": "100",
                    "interestRate": "0", "penaltyRate": "0", "durationMonths": 1, "totalAmount": "100",
                    "startDate": "2099-01-01", "dueDate": "2099-02-01"}],
                    "installments": [{"localId": "1", "debtLocalId": "1", "installmentNumber": 1,
                    "baseAmount": "100", "totalAmount": "100", "dueDate": "2099-02-01"}], "payments": []}
        self.drain()
        sync_snapshot(user=self.user, device_id=device.device_id, device_label="", batch_id="one", snapshot=snapshot)
        self.assertEqual(send_client_emails(), 2)
        self.assertEqual({m.subject for m in mail.outbox}, {"Welcome to Pingo", "A new loan has been recorded for you"})
        mail.outbox.clear()

        def phone_snapshot():
            result = snapshot_for_user(self.user, device)
            result["clients"] = [row for row in result["clients"] if row["localId"]]
            for row in result["debts"]:
                row["clientLocalId"] = "1"
            for row in result["installments"]:
                row["debtLocalId"] = "1"
            for row in result["payments"]:
                row["debtLocalId"], row["installmentLocalId"] = "1", "1"
            # Older phones may omit timestamps and upsert unchanged facts.
            for rows in result.values():
                for row in rows:
                    row.pop("updatedAt", None)
            return result

        snapshot = phone_snapshot()
        snapshot["payments"] = [{"localId": "1", "debtLocalId": "1", "installmentLocalId": "1",
                                 "amount": "100", "createdAt": "2099-01-05", "type": "principal"}]
        sync_snapshot(user=self.user, device_id=device.device_id, device_label="", batch_id="two", snapshot=snapshot)
        self.assertEqual(send_client_emails(), 1)
        self.assertEqual(mail.outbox[0].subject, "Loan fully paid")
        snapshot = phone_snapshot()
        for batch in ("three", "four"):
            sync_snapshot(user=self.user, device_id=device.device_id, device_label="", batch_id=batch, snapshot=snapshot)
            self.assertEqual(send_client_emails(), 0)

    def test_mobile_v2_client_and_payment_events_use_the_same_queue(self):
        self.drain()
        device = bind_device(self.user, str(uuid.uuid4()), "Email test")
        payload = {"type": "push", "mutationId": str(uuid.uuid4()), "action": "addClient", "changes": [
            {"entity": "client", "id": str(uuid.uuid4()), "op": "insert", "fields": {"name": "V2 Ana", "email": "v2@example.test"}}]}
        self.assertEqual(apply_mutation(self.user, device, payload)["status"], "applied")
        self.assertEqual(send_client_emails(), 1)
        self.assertEqual(mail.outbox[0].to, ["v2@example.test"])
        self.assertEqual(apply_mutation(self.user, device, payload)["status"], "duplicate")
        self.assertEqual(send_client_emails(), 0)
        debt = self.debt()
        self.drain()
        payload = {"type": "push", "mutationId": str(uuid.uuid4()), "action": "payDebtBalance", "changes": [
            {"entity": "payment", "id": str(uuid.uuid4()), "op": "insert", "fields": {
            "debtId": str(debt.public_id), "installmentId": str(debt.installments.get().public_id),
            "operationId": str(uuid.uuid4()), "amount": "100", "type": "principal", "date": self.today.isoformat(), "note": ""}}]}
        result = apply_mutation(self.user, device, payload)
        self.assertEqual(result["status"], "applied", result)
        self.assertEqual(send_client_emails(), 1)
        self.assertEqual(mail.outbox[0].subject, "Loan fully paid")
