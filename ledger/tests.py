import sqlite3
import os
from datetime import timedelta
from decimal import Decimal
from tempfile import mkstemp
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from .models import (Client, Debt, Installment, MobileSyncBatch, Payment, PushDelivery, WebPushSubscription)
from .push import send_due_notifications


class LedgerApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner@example.com", email="owner@example.com", password="very-secret")
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def create_client(self, name="Ana"):
        response = self.api.post("/api/clients/", {"name": name, "phone": "+258 84"}, format="json")
        self.assertEqual(response.status_code, 201)
        return response.data["id"]

    def create_debt(self, **overrides):
        payload = {"clientId": self.create_client(), "loanType": "multi", "principal": "100.00", "interestRate": "10", "penaltyRate": "5", "durationMonths": 2, "startDate": "2099-01-01", "dueDate": "2099-03-01"}
        payload.update(overrides)
        response = self.api.post("/api/debts/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_multi_payment_allocates_oldest_installment_then_reverts(self):
        debt = self.create_debt()
        self.assertEqual(debt["id"][:4], "PNG-")
        self.assertEqual(Decimal(str(debt["total"])), Decimal("110.00"))
        response = self.api.post(f"/api/debts/{debt['id']}/payments/", {"action": "balance", "amount": "60"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        updated = response.data["debt"]
        self.assertEqual(Decimal(str(updated["installments"][0]["paidAmount"])), Decimal("55.00"))
        self.assertEqual(Decimal(str(updated["installments"][1]["paidAmount"])), Decimal("5.00"))
        self.assertEqual(Decimal(str(updated["outstanding"])), Decimal("50.00"))
        response = self.api.post(f"/api/debts/{debt['id']}/installments/1/revert/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Decimal(str(response.data["debt"]["installments"][0]["paidAmount"])), Decimal("0"))
        self.assertEqual(Decimal(str(response.data["debt"]["outstanding"])), Decimal("105.00"))

    def test_single_principal_recalculates_interest_then_settles(self):
        debt = self.create_debt(loanType="single", durationMonths=1, penaltyRate="0", principal="100", interestRate="10", dueDate="2099-02-01")
        response = self.api.post(f"/api/debts/{debt['id']}/payments/", {"action": "principal", "amount": "40"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Decimal(str(response.data["debt"]["capitalRemaining"])), Decimal("60.00"))
        self.assertEqual(Decimal(str(response.data["debt"]["outstanding"])), Decimal("66.00"))
        response = self.api.post(f"/api/debts/{debt['id']}/payments/", {"action": "interest"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["debt"]["installments"]), 2)
        response = self.api.post(f"/api/debts/{debt['id']}/payments/", {"action": "settle"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["debt"]["status"], "paid")
        self.assertEqual(Decimal(str(response.data["debt"]["outstanding"])), Decimal("0"))

    def test_single_interest_rolls_forward_and_reverts_latest_period(self):
        debt = self.create_debt(loanType="single", durationMonths=1, penaltyRate="0", principal="100", interestRate="10", dueDate="2099-02-01")
        response = self.api.post(f"/api/debts/{debt['id']}/payments/", {"action": "interest"}, format="json")
        self.assertEqual(len(response.data["debt"]["installments"]), 2)
        response = self.api.post(f"/api/debts/{debt['id']}/installments/1/revert/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["debt"]["installments"]), 1)
        self.assertEqual(Decimal(str(response.data["debt"]["outstanding"])), Decimal("110.00"))

    def test_bootstrap_is_scoped_to_authenticated_user(self):
        self.create_debt()
        other = User.objects.create_user(username="other@example.com", email="other@example.com", password="very-secret")
        self.api.force_authenticate(other)
        response = self.api.get("/api/bootstrap/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["clients"], [])
        self.assertEqual(response.data["debts"], [])

    def test_editing_due_date_updates_final_installment(self):
        debt = self.create_debt()
        response = self.api.patch(f"/api/debts/{debt['id']}/", {"dueDate": "2099-04-15"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["dueDate"], "2099-04-15")
        self.assertEqual(response.data["installments"][-1]["dueDate"], "2099-04-15")

    def test_register_creates_a_session_user(self):
        anonymous = APIClient(enforce_csrf_checks=True)
        csrf = anonymous.get("/api/auth/csrf/")
        response = anonymous.post("/api/auth/register/", {"email": "new@example.com", "password": "long-enough", "name": "New User"}, format="json", HTTP_X_CSRFTOKEN=csrf.data["csrfToken"])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["user"]["email"], "new@example.com")

    def test_dividas_import_is_idempotent(self):
        file_descriptor, source_path = mkstemp(suffix=".db")
        os.close(file_descriptor)
        try:
            source = sqlite3.connect(source_path)
            source.executescript("""
                CREATE TABLE clients (id INTEGER PRIMARY KEY, name TEXT, phone TEXT, email TEXT, notes TEXT);
                CREATE TABLE debts (id INTEGER PRIMARY KEY, debtor_name TEXT, amount REAL, interest_rate REAL, duration_months INTEGER, penalty_rate REAL, total_amount REAL, due_date TEXT, start_date TEXT, is_paid INTEGER, client_id INTEGER, loan_type TEXT, capital_remaining REAL);
                CREATE TABLE installments (id INTEGER PRIMARY KEY, debt_id INTEGER, installment_number INTEGER, total_amount REAL, due_date TEXT, is_paid INTEGER, paid_amount REAL);
                CREATE TABLE payments (id INTEGER PRIMARY KEY, debt_id INTEGER, installment_id INTEGER, amount REAL, created_at TEXT, type TEXT);
                INSERT INTO clients VALUES (1, 'Imported Ana', '+258', '', 'legacy');
                INSERT INTO clients VALUES (2, 'Imported Carlos', '+258', '', 'legacy');
                INSERT INTO debts VALUES (9, 'Imported Ana', 100, 10, 1, 0, 110, '2099-02-01', '2099-01-01', 0, 1, 'multi', 100);
                INSERT INTO debts VALUES (10, 'Imported Carlos', 100, 10, 1, 0, 66, '2099-02-01', '2099-01-01', 0, 2, 'single', 60);
                INSERT INTO installments VALUES (12, 9, 1, 110, '2099-02-01', 0, 20);
                INSERT INTO installments VALUES (13, 10, 1, 6, '2099-02-01', 0, 0);
                INSERT INTO payments VALUES (14, 9, 12, 20, '2099-01-15', 'principal');
                INSERT INTO payments VALUES (15, 10, NULL, 40, '2099-01-15', 'principal');
            """)
            source.commit()
            source.close()
            call_command("import_dividas_sqlite", source_path, user=self.user.email)
            call_command("import_dividas_sqlite", source_path, user=self.user.email)
        finally:
            os.unlink(source_path)
        imported = Debt.objects.get(owner=self.user, legacy_id="9")
        self.assertEqual(imported.reference, "PNG-1009")
        self.assertEqual(Debt.objects.filter(owner=self.user, legacy_id="9").count(), 1)
        imported_single = Debt.objects.get(owner=self.user, legacy_id="10")
        self.assertEqual(imported_single.collected, Decimal("40.00"))
        self.assertEqual(imported_single.outstanding, Decimal("66.00"))


class MobileMigrationApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="mobile@example.com", email="mobile@example.com", password="very-secret")
        from rest_framework.authtoken.models import Token
        self.token = Token.objects.create(user=self.user)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Token {self.token.key}")

    def snapshot(self):
        return {
            "clients": [{"localId": "client-1", "name": "Ana Mobile", "phone": "+258 84", "email": "ana@example.com", "address": "Maputo", "notes": "offline note", "createdAt": "2099-01-01"}],
            "debts": [{"localId": "debt-1", "clientLocalId": "client-1", "debtorName": "Ana Mobile", "amount": "100", "interestRate": "10", "durationMonths": 1, "penaltyRate": "0", "totalAmount": "110", "dueDate": "2099-02-01", "startDate": "2099-01-01", "isPaid": False, "loanType": "multi", "capitalRemaining": "100", "createdAt": "2099-01-01"}],
            "installments": [{"localId": "installment-1", "debtLocalId": "debt-1", "installmentNumber": 1, "baseAmount": "110", "penaltyAmount": "0", "totalAmount": "110", "dueDate": "2099-02-01", "isPaid": False, "paidAmount": "20"}],
            "payments": [{"localId": "payment-1", "debtLocalId": "debt-1", "installmentLocalId": "installment-1", "amount": "20", "note": "first collection", "createdAt": "2099-01-15", "type": "principal"}],
        }

    def sync(self, batch="batch-1", device="phone-abc"):
        return self.api.post("/api/mobile/sync/", {"deviceId": device, "deviceLabel": "Ana phone", "batchId": batch, "snapshot": self.snapshot()}, format="json")

    def test_mobile_register_and_login_issue_a_token_without_a_session(self):
        anonymous = APIClient()
        response = anonymous.post("/api/mobile/register/", {"email": "new-mobile@example.com", "password": "long-enough", "name": "New Mobile"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["token"])
        self.assertNotIn("sessionid", response.cookies)
        login = anonymous.post("/api/mobile/login/", {"email": "new-mobile@example.com", "password": "long-enough"}, format="json")
        self.assertEqual(login.status_code, 200, login.data)
        self.assertEqual(login.data["token"], response.data["token"])

    def test_snapshot_sync_preserves_data_and_retries_are_idempotent(self):
        response = self.sync()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "completed")
        self.assertEqual(response.data["counts"]["clients"]["inserted"], 1)
        self.assertEqual(Client.objects.get(owner=self.user).address, "Maputo")
        debt = Debt.objects.get(owner=self.user)
        self.assertEqual(debt.outstanding, Decimal("90.00"))
        self.assertEqual(debt.collected, Decimal("20.00"))
        self.assertEqual(Payment.objects.get(owner=self.user).note, "first collection")

        retry = self.sync()
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(retry.data["status"], "already_processed")
        self.assertEqual(Client.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(Payment.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(MobileSyncBatch.objects.count(), 1)

    def test_device_local_ids_do_not_cross_user_boundaries(self):
        self.assertEqual(self.sync().status_code, 200)
        other = User.objects.create_user(username="other-mobile@example.com", email="other-mobile@example.com", password="very-secret")
        from rest_framework.authtoken.models import Token
        other_api = APIClient()
        other_api.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=other).key}")
        response = other_api.post("/api/mobile/sync/", {"deviceId": "other-phone", "batchId": "batch-1", "snapshot": self.snapshot()}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Client.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(Client.objects.filter(owner=other).count(), 1)

        hijack = other_api.post("/api/mobile/sync/", {"deviceId": "phone-abc", "batchId": "batch-2", "snapshot": self.snapshot()}, format="json")
        self.assertEqual(hijack.status_code, 400)
        self.assertEqual(Client.objects.filter(owner=self.user).count(), 1)

    def test_web_records_round_trip_to_mobile_without_duplicates(self):
        client = Client.objects.create(owner=self.user, name="Web Client", address="Maputo")
        debt = Debt.objects.create(
            owner=self.user,
            client=client,
            reference="PNG-8801",
            loan_type=Debt.LoanType.MULTI,
            principal=Decimal("100.00"),
            capital_remaining=Decimal("100.00"),
            interest_rate=Decimal("10.00"),
            penalty_rate=Decimal("0.00"),
            duration_months=1,
            total=Decimal("110.00"),
            outstanding=Decimal("110.00"),
            collected=Decimal("0.00"),
            start_date="2099-01-01",
            due_date="2099-02-01",
        )
        installment = debt.installments.create(
            number=1,
            due_date="2099-02-01",
            amount=Decimal("110.00"),
            base_amount=Decimal("110.00"),
            paid_amount=Decimal("0.00"),
        )

        pull = self.api.get("/api/mobile/sync/?deviceId=phone-web")
        self.assertEqual(pull.status_code, 200, pull.data)
        snapshot = pull.data["snapshot"]
        self.assertEqual(snapshot["clients"][0]["name"], "Web Client")

        snapshot["clients"][0]["localId"] = "client-web"
        snapshot["debts"][0]["localId"] = "debt-web"
        snapshot["debts"][0]["clientLocalId"] = "client-web"
        snapshot["installments"][0]["localId"] = "installment-web"
        snapshot["installments"][0]["debtLocalId"] = "debt-web"

        push = self.api.post("/api/mobile/sync/", {
            "deviceId": "phone-web",
            "batchId": "round-trip-1",
            "snapshot": snapshot,
        }, format="json")
        self.assertEqual(push.status_code, 200, push.data)
        self.assertEqual(Client.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(Debt.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(Installment.objects.filter(debt__owner=self.user).count(), 1)
        self.assertEqual(str(push.data["snapshot"]["debts"][0]["serverId"]), str(debt.public_id))
        self.assertEqual(str(push.data["snapshot"]["installments"][0]["serverId"]), str(installment.public_id))

    def test_mobile_pull_is_scoped_and_deletions_are_applied(self):
        owned = Client.objects.create(owner=self.user, name="Owned")
        other = User.objects.create_user(username="private@example.com", email="private@example.com", password="very-secret")
        Client.objects.create(owner=other, name="Private")

        pull = self.api.get("/api/mobile/sync/")
        self.assertEqual([item["name"] for item in pull.data["snapshot"]["clients"]], ["Owned"])

        response = self.api.post("/api/mobile/sync/", {
            "deviceId": "delete-phone",
            "batchId": "delete-1",
            "snapshot": {
                "clients": [], "debts": [], "installments": [], "payments": [],
                "deletions": [{"entity": "client", "serverId": str(owned.public_id)}],
            },
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(Client.objects.filter(owner=self.user).exists())
        self.assertTrue(Client.objects.filter(owner=other, name="Private").exists())

    def test_offline_debt_deletion_wins_over_the_same_sync_snapshot(self):
        first = self.sync()
        self.assertEqual(first.status_code, 200, first.data)
        snapshot = first.data["snapshot"]
        snapshot["debts"][0]["clientLocalId"] = snapshot["clients"][0]["localId"]
        snapshot["installments"][0]["debtLocalId"] = snapshot["debts"][0]["localId"]
        snapshot["payments"][0]["debtLocalId"] = snapshot["debts"][0]["localId"]
        snapshot["payments"][0]["installmentLocalId"] = snapshot["installments"][0]["localId"]
        snapshot["deletions"] = [{
            "entity": "debt",
            "serverId": snapshot["debts"][0]["serverId"],
        }]

        response = self.api.post("/api/mobile/sync/", {
            "deviceId": "phone-abc",
            "batchId": "delete-debt-1",
            "snapshot": snapshot,
        }, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(Debt.objects.filter(owner=self.user).exists())
        self.assertFalse(Payment.objects.filter(owner=self.user).exists())
        self.assertEqual(response.data["snapshot"]["debts"], [])


class WebPushApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="push@example.com", email="push@example.com", password="very-secret")
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.subscription = {
            "endpoint": "https://push.example.com/subscription/abc",
            "expirationTime": None,
            "keys": {"p256dh": "browser-public-key", "auth": "browser-auth-secret"},
        }

    def test_subscription_can_be_enabled_tested_and_disabled(self):
        response = self.api.post("/api/push/subscription/", self.subscription, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(self.api.get("/api/push/subscription/").data["enabled"])
        self.assertEqual(WebPushSubscription.objects.get().owner, self.user)

        with patch("ledger.views.send_push_to_user", return_value={"sent": 1, "failed": 0, "stale": 0, "configured": True}):
            response = self.api.post("/api/push/test/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["sent"], 1)

        response = self.api.delete("/api/push/subscription/", {"endpoint": self.subscription["endpoint"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(WebPushSubscription.objects.exists())

    def test_due_notification_is_sent_once_for_an_event(self):
        client = Client.objects.create(owner=self.user, name="Ana")
        tomorrow = timezone.localdate() + timedelta(days=1)
        debt = Debt.objects.create(
            owner=self.user, client=client, reference="PNG-9001", loan_type=Debt.LoanType.MULTI,
            principal=Decimal("100"), capital_remaining=Decimal("100"), interest_rate=Decimal("10"),
            duration_months=1, total=Decimal("110"), outstanding=Decimal("110"),
            start_date=timezone.localdate(), due_date=tomorrow,
        )
        debt.installments.create(number=1, due_date=tomorrow, amount=Decimal("110"), base_amount=Decimal("110"))
        WebPushSubscription.objects.create(
            owner=self.user, endpoint=self.subscription["endpoint"], p256dh="key", auth="auth"
        )

        delivery_result = {"sent": 1, "failed": 0, "stale": 0, "configured": True}
        with patch("ledger.push.send_push_to_user", return_value=delivery_result) as sender:
            first = send_due_notifications()
            second = send_due_notifications()
        self.assertEqual(first["events"], 1)
        self.assertEqual(second["events"], 0)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(PushDelivery.objects.filter(owner=self.user).count(), 1)
