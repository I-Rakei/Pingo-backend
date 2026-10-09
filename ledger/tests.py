import sqlite3
import os
from datetime import timedelta
from decimal import Decimal
from tempfile import mkstemp
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.core.management import call_command
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from .models import (AmortizationPlan, BalanceNote, Client, CreditNote, Debt, DebitNote, Installment, Invoice, MobileSyncBatch,
                     Organization, OrganizationMembership, Payment, PushDelivery, WebPushSubscription)
from .password_reset import token_generator
from .push import send_due_notifications
from .client_notifications import send_client_emails
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


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

    def test_client_update_is_scoped_to_authenticated_user(self):
        client_id = self.create_client()
        response = self.api.patch(f"/api/clients/{client_id}/", {
            "name": "Ana Matola",
            "phone": "+258 85",
            "email": "ana@example.com",
            "address": "Maputo",
            "notes": "Updated on desktop",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["name"], "Ana Matola")
        self.assertEqual(response.data["address"], "Maputo")

        other = User.objects.create_user(username="client-other@example.com", password="very-secret")
        self.api.force_authenticate(other)
        forbidden = self.api.patch(f"/api/clients/{client_id}/", {"name": "Taken over"}, format="json")
        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(Client.objects.get(pk=client_id).name, "Ana Matola")

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


class ClientShareApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner@example.com", email="owner@example.com", password="very-secret")
        self.other = User.objects.create_user(username="other@example.com", email="other@example.com", password="very-secret")
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client_obj = Client.objects.create(owner=self.user, name="Ana", phone="+258 84", notes="private note")
        self.debt = Debt.objects.create(
            owner=self.user, client=self.client_obj, reference="PNG-5001", loan_type=Debt.LoanType.MULTI,
            principal=Decimal("100"), capital_remaining=Decimal("100"), interest_rate=Decimal("10"),
            duration_months=1, total=Decimal("110"), outstanding=Decimal("55"), collected=Decimal("55"),
            start_date=timezone.localdate(), due_date=timezone.localdate() + timedelta(days=30),
        )
        self.installment = self.debt.installments.create(number=1, due_date=self.debt.due_date, amount=Decimal("110"), paid_amount=Decimal("55"))
        self.payment = Payment.objects.create(
            owner=self.user, debt=self.debt, client=self.client_obj, installment=self.installment,
            amount=Decimal("55"), payment_type=Payment.PaymentType.PRINCIPAL,
        )

    def test_share_link_is_null_until_generated(self):
        response = self.api.get(f"/api/clients/{self.client_obj.pk}/share/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["shareToken"])
        self.assertIsNone(response.data["shareUrl"])

    def test_owner_can_generate_and_regenerate_share_link(self):
        first = self.api.post(f"/api/clients/{self.client_obj.pk}/share/", {}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        first_token = first.data["shareToken"]
        self.assertTrue(first_token)
        self.assertIn(first_token, first.data["shareUrl"])

        second = self.api.post(f"/api/clients/{self.client_obj.pk}/share/", {}, format="json")
        second_token = second.data["shareToken"]
        self.assertNotEqual(first_token, second_token)

        # The old token must 404 immediately after regeneration.
        stale = self.api.get(f"/api/public/clients/{first_token}/")
        self.assertEqual(stale.status_code, 404)
        fresh = self.api.get(f"/api/public/clients/{second_token}/")
        self.assertEqual(fresh.status_code, 200, fresh.data)

    def test_share_link_by_public_id_matches_by_pk_and_is_owner_scoped(self):
        """Mobile only knows a synced client's public_id (server_id), never
        the integer PK, so it must be able to reach the same share flow."""
        response = self.api.post(f"/api/clients/by-public-id/{self.client_obj.public_id}/share/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["shareToken"])

        self.api.force_authenticate(self.other)
        forbidden = self.api.get(f"/api/clients/by-public-id/{self.client_obj.public_id}/share/")
        self.assertEqual(forbidden.status_code, 404)

    def test_share_management_is_scoped_to_owner(self):
        self.api.force_authenticate(self.other)
        response = self.api.get(f"/api/clients/{self.client_obj.pk}/share/")
        self.assertEqual(response.status_code, 404)
        response = self.api.post(f"/api/clients/{self.client_obj.pk}/share/", {}, format="json")
        self.assertEqual(response.status_code, 404)
        self.client_obj.refresh_from_db()
        self.assertIsNone(self.client_obj.share_token)

    def test_public_endpoint_returns_debts_and_payments_without_private_fields(self):
        generate_response = self.api.post(f"/api/clients/{self.client_obj.pk}/share/", {}, format="json")
        token = generate_response.data["shareToken"]

        anonymous = APIClient()
        response = anonymous.get(f"/api/public/clients/{token}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["name"], "Ana")
        self.assertEqual(len(response.data["debts"]), 1)
        debt = response.data["debts"][0]
        self.assertEqual(debt["id"], "PNG-5001")
        # Only what is owed now: half of the 110 instalment is unpaid, split in
        # the loan's 100:10 capital-to-interest ratio.
        self.assertEqual({key: Decimal(str(debt[key])) for key in ("capital", "interest", "total")},
                         {"capital": Decimal("50"), "interest": Decimal("5"), "total": Decimal("55")})
        self.assertNotIn("paid", debt)
        self.assertEqual(len(response.data["paymentsMade"]), 1)
        self.assertEqual(Decimal(str(response.data["paymentsMade"][0]["amount"])), Decimal("55.00"))
        self.assertEqual([(item["number"], Decimal(str(item["amount"]))) for item in response.data["paymentsDue"]],
                         [(1, Decimal("55"))])
        self.assertEqual(Decimal(str(response.data["summary"]["total"])), Decimal("55"))

        # Private/owner-only and internal ledger fields never appear in the public payload.
        payload_text = str(response.data)
        self.assertNotIn("private note", payload_text)
        self.assertNotIn("+258 84", payload_text)
        self.assertNotIn("owner", response.data)
        for hidden in ("outstanding", "collected", "interestRate", "installments"):
            self.assertNotIn(hidden, debt)

    def test_public_endpoint_hides_reversed_payments_and_unknown_token(self):
        self.payment.reversed_at = timezone.now()
        self.payment.save(update_fields=["reversed_at"])
        generate_response = self.api.post(f"/api/clients/{self.client_obj.pk}/share/", {}, format="json")
        token = generate_response.data["shareToken"]

        anonymous = APIClient()
        response = anonymous.get(f"/api/public/clients/{token}/")
        self.assertEqual(response.data["paymentsMade"], [])

        missing = anonymous.get("/api/public/clients/not-a-real-token/")
        self.assertEqual(missing.status_code, 404)

    def test_public_statement_shows_only_what_is_owed_now(self):
        # Single loan: capital 3700, interest period 1 (1110) paid, period 2 (1110) open.
        single = Debt.objects.create(
            owner=self.user, client=self.client_obj, reference="PNG-5010", loan_type=Debt.LoanType.SINGLE,
            principal=Decimal("3700"), capital_remaining=Decimal("3700"), interest_rate=Decimal("30"),
            duration_months=1, total=Decimal("4810"), outstanding=Decimal("4810"), collected=Decimal("1110"),
            start_date=timezone.localdate(), due_date=timezone.localdate() + timedelta(days=30),
        )
        single.installments.create(number=1, due_date=timezone.localdate(), amount=Decimal("1110"), paid_amount=Decimal("1110"))
        single.installments.create(number=2, due_date=single.due_date, amount=Decimal("1110"))
        paid = Debt.objects.create(
            owner=self.user, client=self.client_obj, reference="PNG-5011", loan_type=Debt.LoanType.MULTI,
            principal=Decimal("300"), capital_remaining=Decimal("300"), interest_rate=Decimal("0"),
            duration_months=1, total=Decimal("300"), outstanding=Decimal("0"), collected=Decimal("300"),
            start_date=timezone.localdate(), due_date=timezone.localdate(),
        )
        paid.installments.create(number=1, due_date=paid.due_date, amount=Decimal("300"), paid_amount=Decimal("300"))
        token = self.api.post(f"/api/clients/{self.client_obj.pk}/share/", {}, format="json").data["shareToken"]
        data = APIClient().get(f"/api/public/clients/{token}/").data
        debts = {item["id"]: item for item in data["debts"]}
        self.assertNotIn("PNG-5011", debts)  # fully paid debts are not listed
        self.assertEqual({key: Decimal(str(debts["PNG-5010"][key])) for key in ("capital", "interest", "total")},
                         {"capital": Decimal("3700"), "interest": Decimal("1110"), "total": Decimal("4810")})

    def test_public_endpoint_never_exposes_another_clients_data(self):
        other_client = Client.objects.create(owner=self.other, name="Carlos")
        Debt.objects.create(
            owner=self.other, client=other_client, reference="PNG-5002", loan_type=Debt.LoanType.MULTI,
            principal=Decimal("50"), capital_remaining=Decimal("50"), interest_rate=Decimal("5"),
            duration_months=1, total=Decimal("55"), outstanding=Decimal("55"),
            start_date=timezone.localdate(), due_date=timezone.localdate() + timedelta(days=30),
        )
        generate_response = self.api.post(f"/api/clients/{self.client_obj.pk}/share/", {}, format="json")
        token = generate_response.data["shareToken"]

        anonymous = APIClient()
        response = anonymous.get(f"/api/public/clients/{token}/")
        debt_ids = [item["id"] for item in response.data["debts"]]
        self.assertNotIn("PNG-5002", debt_ids)


class PasswordResetApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner@example.com", email="owner@example.com", password="original-pass")
        self.api = APIClient()

    def valid_uid_token(self):
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = token_generator.make_token(self.user)
        return uid, token

    def test_request_reset_for_real_email_sends_one_email(self):
        response = self.api.post("/api/auth/password-reset/", {"email": "owner@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("owner@example.com", mail.outbox[0].to)
        self.assertIn("reset_uid=", mail.outbox[0].body)
        self.assertIn("reset_token=", mail.outbox[0].body)

    def test_request_reset_for_unknown_email_still_returns_200_but_sends_a_no_account_hint(self):
        # The HTTP response stays generic (no enumeration leak to a caller),
        # but the mailbox owner themself is told no account exists.
        response = self.api.post("/api/auth/password-reset/", {"email": "nobody@example.com"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("nobody@example.com", mail.outbox[0].to)
        self.assertNotIn("reset_uid=", mail.outbox[0].body)
        self.assertIn("no Pingo account", mail.outbox[0].body)

    def test_confirm_reset_sets_new_password_and_rotates_mobile_token(self):
        old_token = Token.objects.create(user=self.user)
        uid, token = self.valid_uid_token()
        response = self.api.post("/api/auth/password-reset/confirm/", {"uid": uid, "token": token, "password": "brand-new-pass"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("brand-new-pass"))
        self.assertFalse(self.user.check_password("original-pass"))
        self.assertFalse(Token.objects.filter(pk=old_token.pk).exists())

    def test_confirm_reset_rejects_tampered_token(self):
        uid, _ = self.valid_uid_token()
        response = self.api.post("/api/auth/password-reset/confirm/", {"uid": uid, "token": "not-a-real-token", "password": "brand-new-pass"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("original-pass"))

    def test_confirm_reset_token_cannot_be_replayed_against_a_different_user(self):
        other = User.objects.create_user(username="other@example.com", email="other@example.com", password="other-pass")
        _, token = self.valid_uid_token()
        other_uid = urlsafe_base64_encode(force_bytes(other.pk))
        response = self.api.post("/api/auth/password-reset/confirm/", {"uid": other_uid, "token": token, "password": "brand-new-pass"}, format="json")
        self.assertEqual(response.status_code, 400)
        other.refresh_from_db()
        self.assertTrue(other.check_password("other-pass"))

    def test_confirm_reset_rejects_weak_password(self):
        uid, token = self.valid_uid_token()
        response = self.api.post("/api/auth/password-reset/confirm/", {"uid": uid, "token": token, "password": "short"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("original-pass"))


class OrganizationApiTests(TestCase):
    def register_corporate(self, email, company_name, password="very-secret"):
        anonymous = APIClient(enforce_csrf_checks=True)
        csrf = anonymous.get("/api/auth/csrf/")
        response = anonymous.post("/api/auth/register/", {
            "email": email, "password": password, "name": "Owner",
            "accountType": "corporate", "organization": {"name": company_name, "nuit": "123456789", "ivaRate": "16"},
        }, format="json", HTTP_X_CSRFTOKEN=csrf.data["csrfToken"])
        self.assertEqual(response.status_code, 201, response.data)
        api = APIClient()
        api.force_authenticate(User.objects.get(username=email))
        return api

    def setUp(self):
        self.owner_email = "owner@corp.example.com"
        self.owner_api = self.register_corporate(self.owner_email, "Acme Microcredito")
        self.owner = User.objects.get(username=self.owner_email)
        self.organization = OrganizationMembership.objects.get(user=self.owner).organization

    def test_personal_registration_creates_no_organization(self):
        anonymous = APIClient(enforce_csrf_checks=True)
        csrf = anonymous.get("/api/auth/csrf/")
        response = anonymous.post("/api/auth/register/", {"email": "solo@example.com", "password": "very-secret", "name": "Solo"}, format="json", HTTP_X_CSRFTOKEN=csrf.data["csrfToken"])
        self.assertEqual(response.status_code, 201, response.data)
        user = User.objects.get(username="solo@example.com")
        self.assertFalse(OrganizationMembership.objects.filter(user=user).exists())

    def test_corporate_bootstrap_reports_organization_and_role(self):
        response = self.owner_api.get("/api/bootstrap/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["organization"]["name"], "Acme Microcredito")
        self.assertEqual(response.data["organization"]["role"], "owner")

    def test_owner_can_create_and_manage_staff(self):
        response = self.owner_api.post("/api/organizations/staff/", {
            "name": "Staff One", "email": "staff1@corp.example.com", "password": "staff-secret", "role": "staff",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        staff_user_id = response.data["id"]
        self.assertEqual(response.data["role"], "staff")

        listing = self.owner_api.get("/api/organizations/staff/")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.data), 2)  # owner + staff

        updated = self.owner_api.patch(f"/api/organizations/staff/{staff_user_id}/", {"name": "Staff Renamed", "role": "staff"}, format="json")
        self.assertEqual(updated.status_code, 200, updated.data)
        self.assertEqual(updated.data["name"], "Staff Renamed")

        removed = self.owner_api.delete(f"/api/organizations/staff/{staff_user_id}/")
        self.assertEqual(removed.status_code, 204)
        self.assertFalse(OrganizationMembership.objects.filter(user_id=staff_user_id).exists())

    def test_staff_cannot_manage_other_staff(self):
        self.owner_api.post("/api/organizations/staff/", {
            "name": "Staff One", "email": "staff2@corp.example.com", "password": "staff-secret", "role": "staff",
        }, format="json")
        staff_user = User.objects.get(username="staff2@corp.example.com")
        staff_api = APIClient()
        staff_api.force_authenticate(staff_user)

        forbidden = staff_api.post("/api/organizations/staff/", {
            "name": "Another", "email": "staff3@corp.example.com", "password": "staff-secret", "role": "staff",
        }, format="json")
        self.assertEqual(forbidden.status_code, 400)

    def test_owner_and_staff_share_the_same_organization_ledger(self):
        self.owner_api.post("/api/organizations/staff/", {
            "name": "Staff One", "email": "staff4@corp.example.com", "password": "staff-secret", "role": "staff",
        }, format="json")
        staff_api = APIClient()
        staff_api.force_authenticate(User.objects.get(username="staff4@corp.example.com"))

        created = self.owner_api.post("/api/clients/", {"name": "Shared Client"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)

        seen_by_staff = staff_api.get("/api/clients/")
        self.assertEqual(len(seen_by_staff.data), 1)
        self.assertEqual(seen_by_staff.data[0]["name"], "Shared Client")

    def test_organizations_are_isolated_from_each_other(self):
        other_api = self.register_corporate("owner2@corp.example.com", "Other Corp")
        self.owner_api.post("/api/clients/", {"name": "Org A Client"}, format="json")
        other_api.post("/api/clients/", {"name": "Org B Client"}, format="json")

        org_a_clients = self.owner_api.get("/api/clients/")
        self.assertEqual([c["name"] for c in org_a_clients.data], ["Org A Client"])

        org_b_staff_list = self.owner_api.get("/api/organizations/staff/")
        names = {member["email"] for member in org_b_staff_list.data}
        self.assertNotIn("owner2@corp.example.com", names)

    def test_mobile_login_blocks_corporate_accounts(self):
        response = APIClient().post("/api/mobile/login/", {"email": self.owner_email, "password": "very-secret"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_mobile_register_blocks_corporate_account_type(self):
        response = APIClient().post("/api/mobile/register/", {
            "email": "mobilecorp@example.com", "password": "very-secret", "name": "Mobile", "accountType": "corporate",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("accountType", response.data)


class FiscalDocumentApiTests(TestCase):
    def setUp(self):
        anonymous = APIClient(enforce_csrf_checks=True)
        csrf = anonymous.get("/api/auth/csrf/")
        response = anonymous.post("/api/auth/register/", {
            "email": "docs@corp.example.com", "password": "very-secret", "name": "Owner",
            "accountType": "corporate", "organization": {"name": "Doc Corp"},
        }, format="json", HTTP_X_CSRFTOKEN=csrf.data["csrfToken"])
        self.assertEqual(response.status_code, 201, response.data)
        self.owner = User.objects.get(username="docs@corp.example.com")
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.organization = OrganizationMembership.objects.get(user=self.owner).organization
        client_response = self.api.post("/api/clients/", {"name": "Fatima"}, format="json")
        self.client_id = client_response.data["id"]

    def test_documents_are_numbered_independently_per_organization(self):
        first = self.api.post("/api/organizations/documents/invoice/", {"clientId": self.client_id, "amount": "100.00", "description": "First"}, format="json")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["number"], 1)
        second = self.api.post("/api/organizations/documents/invoice/", {"clientId": self.client_id, "amount": "50.00", "description": "Second"}, format="json")
        self.assertEqual(second.data["number"], 2)

        other_api_owner = User.objects.create_user(username="other-corp@example.com", email="other-corp@example.com", password="very-secret")
        other_org = Organization.objects.create(name="Other Corp", created_by=other_api_owner)
        OrganizationMembership.objects.create(user=other_api_owner, organization=other_org, role=OrganizationMembership.Role.OWNER)
        other_api = APIClient()
        other_api.force_authenticate(other_api_owner)
        other_client = other_api.post("/api/clients/", {"name": "Other Client"}, format="json")
        other_first = other_api.post("/api/organizations/documents/invoice/", {"clientId": other_client.data["id"], "amount": "10.00"}, format="json")
        self.assertEqual(other_first.data["number"], 1)

    def test_personal_account_cannot_issue_documents(self):
        personal = User.objects.create_user(username="solo-doc@example.com", email="solo-doc@example.com", password="very-secret")
        personal_client = Client.objects.create(owner=personal, name="Solo Client")
        api = APIClient()
        api.force_authenticate(personal)
        response = api.post("/api/organizations/documents/invoice/", {"clientId": personal_client.pk, "amount": "10.00"}, format="json")
        self.assertEqual(response.status_code, 404)

    def test_each_document_type_can_be_issued_and_listed(self):
        for doc_type in ("invoice", "debit_note", "credit_note", "balance_note"):
            response = self.api.post(f"/api/organizations/documents/{doc_type}/", {"clientId": self.client_id, "amount": "20.00"}, format="json")
            self.assertEqual(response.status_code, 201, (doc_type, response.data))
            listing = self.api.get(f"/api/organizations/documents/{doc_type}/")
            self.assertEqual(len(listing.data), 1)
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(DebitNote.objects.count(), 1)
        self.assertEqual(CreditNote.objects.count(), 1)
        self.assertEqual(BalanceNote.objects.count(), 1)

    def test_amortization_schedule_returns_installments_for_owned_debt(self):
        debt_response = self.api.post("/api/debts/", {
            "clientId": self.client_id, "loanType": "multi", "principal": "100.00", "interestRate": "10",
            "durationMonths": 2, "startDate": "2099-01-01", "dueDate": "2099-03-01",
        }, format="json")
        self.assertEqual(debt_response.status_code, 201, debt_response.data)
        reference = debt_response.data["id"]

        schedule = self.api.get(f"/api/organizations/documents/schedule/{reference}/")
        self.assertEqual(schedule.status_code, 200, schedule.data)
        self.assertEqual(schedule.data["organization"]["name"], "Doc Corp")
        self.assertEqual(len(schedule.data["installments"]), 2)
        self.assertEqual(schedule.data["principal"], "100.00")
        self.assertEqual(schedule.data["scheduleTotal"], "110.00")
        self.assertEqual([row["principalAmount"] for row in schedule.data["installments"]], ["50.00"] * 2)
        self.assertEqual([row["interestAmount"] for row in schedule.data["installments"]], ["5.00"] * 2)
        self.assertEqual([row["dueDate"] for row in schedule.data["installments"]], ["2099-02-01", "2099-03-01"])

    def test_amortization_simulation_uses_ledger_rounding_and_clamped_dates(self):
        response = self.api.post("/api/organizations/documents/schedule/simulate/", {
            "clientName": "Ana", "loanType": "multi", "principal": "100.00",
            "interestRate": "10", "durationMonths": 3, "startDate": "2099-01-31",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["installments"]
        self.assertEqual([row["dueDate"] for row in rows], ["2099-02-28", "2099-03-30", "2099-04-30"])
        self.assertEqual([row["amount"] for row in rows], ["36.67"] * 3)
        self.assertEqual(sum(Decimal(row["principalAmount"]) for row in rows), Decimal("100.00"))
        self.assertEqual(sum(Decimal(row["interestAmount"]) for row in rows), Decimal("10.01"))
        self.assertEqual(rows[-1]["capitalBalance"], "0.00")
        self.assertEqual(response.data["interestTotal"], "10.01")
        self.assertEqual(response.data["scheduleTotal"], "110.01")
        self.assertEqual(Debt.objects.filter(organization=self.organization).count(), 0)

    def test_simulation_totals_above_one_ledger_amount_are_returned(self):
        response = self.api.post("/api/organizations/documents/schedule/simulate/", {
            "loanType": "multi", "principal": "900000000000.00", "interestRate": "100",
            "durationMonths": 12, "startDate": "2099-01-01",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["scheduleTotal"], "1800000000000.00")
        self.assertEqual(response.data["outstanding"], "1800000000000.00")

    def test_single_loan_simulation_lists_interest_and_separate_capital(self):
        response = self.api.post("/api/organizations/documents/schedule/simulate/", {
            "loanType": "single", "principal": "1000.00", "interestRate": "5",
            "durationMonths": 2, "startDate": "2099-01-15",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["installments"]
        self.assertEqual([row["interestAmount"] for row in rows], ["50.00", "50.00", "0.00"])
        self.assertEqual([row["principalAmount"] for row in rows], ["0.00", "0.00", "1000.00"])
        self.assertEqual(response.data["scheduleTotal"], "1100.00")

    def test_saved_schedule_uses_recorded_payments_and_single_capital(self):
        debt = self.api.post("/api/debts/", {
            "clientId": self.client_id, "loanType": "single", "principal": "1000.00",
            "interestRate": "5", "durationMonths": 2,
            "startDate": "2099-01-15", "dueDate": "2099-02-15",
        }, format="json")
        self.assertEqual(debt.status_code, 201, debt.data)
        reference = debt.data["id"]
        payment = self.api.post(f"/api/debts/{reference}/payments/", {"action": "principal", "amount": "200.00"}, format="json")
        self.assertEqual(payment.status_code, 200, payment.data)
        schedule = self.api.get(f"/api/organizations/documents/schedule/{reference}/")
        self.assertEqual(schedule.status_code, 200, schedule.data)
        rows = schedule.data["installments"]
        self.assertEqual(rows[-1]["rowType"], "capital")
        self.assertEqual(rows[-1]["principalAmount"], "1000.00")
        self.assertEqual(rows[-1]["paidAmount"], "200.00")
        self.assertEqual(rows[-1]["remainingAmount"], "800.00")
        self.assertEqual(schedule.data["outstanding"], "840.00")

    def test_amortization_requires_corporate_membership_and_organization_scope(self):
        personal = User.objects.create_user(username="personal-schedule@example.com", password="very-secret")
        personal_api = APIClient()
        personal_api.force_authenticate(personal)
        simulation_path = "/api/organizations/documents/schedule/simulate/"
        self.assertEqual(personal_api.post(simulation_path, {}, format="json").status_code, 404)
        self.assertEqual(APIClient().post(simulation_path, {}, format="json").status_code, 403)
        invalid = self.api.post(simulation_path, {
            "loanType": "multi", "principal": "0", "interestRate": "1",
            "durationMonths": 2, "startDate": "2099-01-01",
        }, format="json")
        self.assertEqual(invalid.status_code, 400)
        debt = self.api.post("/api/debts/", {
            "clientId": self.client_id, "loanType": "multi", "principal": "100.00",
            "interestRate": "0", "durationMonths": 1,
            "startDate": "2099-01-01", "dueDate": "2099-02-01",
        }, format="json")
        reference = debt.data["id"]
        other_owner = User.objects.create_user(username="other-schedule@example.com", password="very-secret")
        other_org = Organization.objects.create(name="Other Corp", created_by=other_owner)
        OrganizationMembership.objects.create(user=other_owner, organization=other_org, role=OrganizationMembership.Role.OWNER)
        other_api = APIClient()
        other_api.force_authenticate(other_owner)
        path = f"/api/organizations/documents/schedule/{reference}/"
        self.assertEqual(personal_api.get(path).status_code, 404)
        self.assertEqual(other_api.get(path).status_code, 404)
        self.assertEqual(APIClient().get(path).status_code, 403)


class ClientNotificationApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="owner@example.com", email="owner@example.com", password="very-secret")
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def create_client_with_email(self, email="ana@example.com"):
        response = self.api.post("/api/clients/", {"name": "Ana", "email": email}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        send_client_emails()
        mail.outbox.clear()
        return response.data["id"]

    def create_debt(self, client_id, **overrides):
        payload = {"clientId": client_id, "loanType": "multi", "principal": "100.00", "interestRate": "10", "penaltyRate": "0", "durationMonths": 1, "startDate": "2099-01-01", "dueDate": "2099-02-01"}
        payload.update(overrides)
        response = self.api.post("/api/debts/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        send_client_emails()
        return response.data

    def test_client_with_email_is_notified_when_debt_is_created(self):
        client_id = self.create_client_with_email()
        self.create_debt(client_id)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ana@example.com", mail.outbox[0].to)
        self.assertIn("new loan", mail.outbox[0].subject.lower())

    def test_client_without_email_is_not_notified(self):
        response = self.api.post("/api/clients/", {"name": "No Email Client"}, format="json")
        self.create_debt(response.data["id"])
        self.assertEqual(len(mail.outbox), 0)

    def test_client_is_notified_when_payment_is_recorded(self):
        client_id = self.create_client_with_email()
        debt = self.create_debt(client_id)
        mail.outbox.clear()
        response = self.api.post(f"/api/debts/{debt['id']}/payments/", {"action": "balance", "amount": "50"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        send_client_emails()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ana@example.com", mail.outbox[0].to)
        self.assertIn("payment", mail.outbox[0].subject.lower())

    def test_client_is_emailed_due_tomorrow_and_overdue_reminders(self):
        client_id = self.create_client_with_email()
        today = timezone.localdate()
        tomorrow_debt = self.create_debt(client_id, startDate=today.isoformat(), dueDate=(today + timedelta(days=1)).isoformat())
        overdue_debt = self.create_debt(client_id, startDate=today.isoformat(), dueDate=(today + timedelta(days=5)).isoformat())
        Installment.objects.filter(debt__reference=overdue_debt["id"]).update(due_date=today - timedelta(days=2))
        mail.outbox.clear()

        totals = send_due_notifications()
        self.assertEqual(totals["clientEmails"], 2)
        self.assertEqual(len(mail.outbox), 2)
        subjects = sorted(message.subject for message in mail.outbox)
        self.assertEqual(subjects, ["Payment due tomorrow", "Payment overdue"])

        # Re-running the same day must not duplicate the client emails.
        mail.outbox.clear()
        second = send_due_notifications()
        self.assertEqual(second["clientEmails"], 0)
        self.assertEqual(len(mail.outbox), 0)


class ClientDetailsAndAmortizationPlanTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="plans@corp.example.com", email="plans@corp.example.com", password="very-secret")
        self.organization = Organization.objects.create(name="Plan Corp", created_by=self.owner)
        OrganizationMembership.objects.create(user=self.owner, organization=self.organization, role=OrganizationMembership.Role.OWNER)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.client_id = self.api.post("/api/clients/", {"name": "Fatima"}, format="json").data["id"]

    def plan_terms(self, **overrides):
        return {"clientId": self.client_id, "loanType": "multi", "principal": "100.00", "interestRate": "10",
                "durationMonths": 3, "startDate": "2099-01-31", **overrides}

    def test_client_details_are_optional_and_round_trip(self):
        bare = self.api.post("/api/clients/", {"name": "Only a name"}, format="json")
        self.assertEqual(bare.status_code, 201, bare.data)
        self.assertEqual((bare.data["contacts"], bare.data["bankAccounts"], bare.data["idNumber"]), ([], [], ""))
        response = self.api.post("/api/clients/", {
            "name": "Ana", "city": "Maputo", "countryOfBirth": "Mozambique", "idNumber": "110100123456A",
            "altPhone": "+258 84 000 0000", "nuit": "123456789",
            "contacts": [{"name": "Rui", "relationship": "Brother", "phone": "+258 82 1"}, {"name": " ", "phone": ""}],
            "bankAccounts": [{"bank": "BCI", "accountNumber": "123", "nib": "000800001234567890123"}],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        client = Client.objects.get(pk=response.data["id"])
        self.assertEqual((client.city, client.country_of_birth, client.id_number, client.nuit), ("Maputo", "Mozambique", "110100123456A", "123456789"))
        self.assertEqual(client.contacts, [{"name": "Rui", "relationship": "Brother", "phone": "+258 82 1", "email": ""}])
        self.assertEqual(response.data["bankAccounts"][0]["bank"], "BCI")
        patched = self.api.patch(f"/api/clients/{client.pk}/", {"bankAccounts": []}, format="json")
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(patched.data["bankAccounts"], [])
        self.assertEqual(patched.data["contacts"][0]["name"], "Rui")

    def test_client_detail_lists_are_validated(self):
        for payload in ({"contacts": "Rui"}, {"contacts": ["Rui"]}, {"bankAccounts": [{"bank": 5}]},
                        {"contacts": [{"name": f"P{index}"} for index in range(11)]}):
            response = self.api.post("/api/clients/", {"name": "Invalid", **payload}, format="json")
            self.assertEqual(response.status_code, 400, payload)

    def test_plans_are_saved_listed_reloaded_and_deleted(self):
        created = self.api.post("/api/organizations/amortization-plans/", self.plan_terms(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["plan"]["clientName"], "Fatima")
        self.assertEqual(created.data["plan"]["scheduleTotal"], "110.01")
        self.assertEqual(created.data["schedule"]["clientName"], "Fatima")
        self.assertEqual(len(created.data["schedule"]["installments"]), 3)
        other_client = self.api.post("/api/clients/", {"name": "Other"}, format="json").data["id"]
        self.api.post("/api/organizations/amortization-plans/", self.plan_terms(clientId=other_client), format="json")
        self.assertEqual(len(self.api.get("/api/organizations/amortization-plans/").data), 2)
        listed = self.api.get(f"/api/organizations/amortization-plans/?clientId={self.client_id}")
        self.assertEqual([plan["id"] for plan in listed.data], [created.data["plan"]["id"]])
        path = f"/api/organizations/amortization-plans/{created.data['plan']['id']}/"
        reloaded = self.api.get(path)
        self.assertEqual(reloaded.data["schedule"]["installments"], created.data["schedule"]["installments"])
        self.assertEqual(self.api.delete(path).status_code, 204)
        self.assertEqual(self.api.get(path).status_code, 404)
        self.assertEqual(Debt.objects.count(), 0)

    def test_plans_are_scoped_to_corporate_organizations(self):
        plan_id = self.api.post("/api/organizations/amortization-plans/", self.plan_terms(), format="json").data["plan"]["id"]
        personal = User.objects.create_user(username="solo-plans@example.com", password="very-secret")
        personal_api = APIClient(); personal_api.force_authenticate(personal)
        self.assertEqual(personal_api.get("/api/organizations/amortization-plans/").status_code, 404)
        other_owner = User.objects.create_user(username="rival@corp.example.com", password="very-secret")
        other_org = Organization.objects.create(name="Rival", created_by=other_owner)
        OrganizationMembership.objects.create(user=other_owner, organization=other_org, role=OrganizationMembership.Role.OWNER)
        other_api = APIClient(); other_api.force_authenticate(other_owner)
        self.assertEqual(other_api.get("/api/organizations/amortization-plans/").data, [])
        self.assertEqual(other_api.get(f"/api/organizations/amortization-plans/{plan_id}/").status_code, 404)
        self.assertEqual(other_api.delete(f"/api/organizations/amortization-plans/{plan_id}/").status_code, 404)
        foreign = other_api.post("/api/organizations/amortization-plans/", self.plan_terms(), format="json")
        self.assertEqual(foreign.status_code, 400)
        self.assertEqual(self.api.get("/api/organizations/amortization-plans/?clientId=abc").status_code, 400)

    def test_deleting_a_client_removes_its_plans(self):
        self.api.post("/api/organizations/amortization-plans/", self.plan_terms(), format="json")
        self.assertEqual(self.api.delete(f"/api/clients/{self.client_id}/").status_code, 204)
        self.assertEqual(self.api.get("/api/organizations/amortization-plans/").data, [])


class CorporateLoanAmortizationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="loans@corp.example.com", email="loans@corp.example.com", password="very-secret")
        self.organization = Organization.objects.create(name="Loan Corp", created_by=self.owner)
        OrganizationMembership.objects.create(user=self.owner, organization=self.organization, role=OrganizationMembership.Role.OWNER)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.client_id = self.api.post("/api/clients/", {"name": "Fatima"}, format="json").data["id"]

    def create_from_preview(self, terms):
        preview = self.api.post("/api/organizations/loans/preview/", terms, format="json")
        self.assertEqual(preview.status_code, 200, preview.data)
        created = self.api.post("/api/debts/", {**terms, "clientId": self.client_id, "dueDate": preview.data["dueDate"]}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        return preview.data, created.data

    def test_multi_period_preview_matches_the_created_loan_and_its_plan(self):
        preview, debt = self.create_from_preview({"loanType": "multi", "principal": "100.00", "interestRate": "10",
                                                  "penaltyRate": "5", "durationMonths": 3, "startDate": "2099-01-31"})
        self.assertEqual(preview["dueDate"], "2099-04-30")
        self.assertEqual([(item["dueDate"], str(item["amount"])) for item in debt["installments"]],
                         [(row["dueDate"], row["amount"]) for row in preview["installments"]])
        plans = self.api.get(f"/api/organizations/amortization-plans/?clientId={self.client_id}").data
        self.assertEqual(len(plans), 1)
        self.assertEqual((plans[0]["debtId"], plans[0]["scheduleTotal"], plans[0]["dueDate"]), (debt["id"], preview["scheduleTotal"], "2099-04-30"))
        schedule = self.api.get(f"/api/organizations/amortization-plans/{plans[0]['id']}/").data["schedule"]
        self.assertEqual(schedule["debtId"], debt["id"])
        self.assertEqual(schedule["installments"], preview["installments"])
        self.assertEqual(self.api.delete(f"/api/organizations/amortization-plans/{plans[0]['id']}/").status_code, 400)

    def test_single_loan_preview_matches_the_created_loan(self):
        preview, debt = self.create_from_preview({"loanType": "single", "principal": "1000.00", "interestRate": "5",
                                                  "startDate": "2099-01-15", "dueDate": "2099-03-10"})
        self.assertEqual([row["rowType"] for row in preview["installments"]], ["installment", "capital"])
        self.assertEqual(debt["installments"][0]["dueDate"], "2099-03-10")
        self.assertEqual(preview["scheduleTotal"], "1050.00")
        plan = self.api.get("/api/organizations/amortization-plans/").data[0]
        self.assertEqual((plan["debtId"], plan["durationMonths"]), (debt["id"], 1))
        missing_end = self.api.post("/api/organizations/loans/preview/", {"loanType": "single", "principal": "10", "interestRate": "5",
                                                                           "startDate": "2099-01-15"}, format="json")
        self.assertEqual(missing_end.status_code, 400)

    def test_plan_of_a_deleted_loan_remains_as_history(self):
        _, debt = self.create_from_preview({"loanType": "multi", "principal": "100.00", "interestRate": "10",
                                            "durationMonths": 2, "startDate": "2099-01-01"})
        self.assertEqual(self.api.delete(f"/api/debts/{debt['id']}/").status_code, 204)
        plan = self.api.get("/api/organizations/amortization-plans/").data[0]
        self.assertEqual(plan["debtId"], "")
        schedule = self.api.get(f"/api/organizations/amortization-plans/{plan['id']}/").data["schedule"]
        self.assertEqual([row["dueDate"] for row in schedule["installments"]], ["2099-02-01", "2099-03-01"])

    def test_personal_accounts_are_unchanged(self):
        personal = User.objects.create_user(username="solo-loans@example.com", password="very-secret")
        personal_api = APIClient(); personal_api.force_authenticate(personal)
        solo = personal_api.post("/api/clients/", {"name": "Solo", "idNumber": "123", "bankAccounts": [{"bank": "BCI"}]}, format="json")
        self.assertEqual(set(solo.data), {"id", "publicId", "name", "phone", "email", "address", "notes", "createdAt", "updatedAt"})
        self.assertEqual((Client.objects.get(pk=solo.data["id"]).id_number, Client.objects.get(pk=solo.data["id"]).bank_accounts), ("", []))
        self.assertNotIn("idNumber", personal_api.get("/api/bootstrap/").data["clients"][0])
        client_id = solo.data["id"]
        created = personal_api.post("/api/debts/", {"clientId": client_id, "loanType": "multi", "principal": "100.00", "interestRate": "10",
                                                    "durationMonths": 2, "startDate": "2099-01-01", "dueDate": "2099-03-01"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual([str(item["amount"]) for item in created.data["installments"]], ["55.00", "55.00"])
        self.assertEqual(AmortizationPlan.objects.count(), 0)
        preview = personal_api.post("/api/organizations/loans/preview/", {"loanType": "multi", "principal": "1", "interestRate": "1",
                                                                          "durationMonths": 1, "startDate": "2099-01-01"}, format="json")
        self.assertEqual(preview.status_code, 404)


class RouteAuthenticationTests(TestCase):
    """Every API route needs a signed-in user, except the deliberately public ones below."""

    PUBLIC_ROUTES = {
        "auth/csrf/", "auth/login/", "auth/register/", "auth/password-reset/", "auth/password-reset/confirm/",
        "mobile/register/", "mobile/login/", "push/config/", "public/clients/<str:token>/",
    }

    def test_anonymous_requests_are_rejected_on_every_private_route(self):
        import uuid
        from django.urls import URLPattern, get_resolver

        samples = {"int": "1", "uuid": str(uuid.uuid4()), "str": "PNG-1"}
        routes = [pattern for pattern in get_resolver("ledger.urls").url_patterns if isinstance(pattern, URLPattern)]
        self.assertGreaterEqual(len(routes), 40)
        anonymous = APIClient()
        for pattern in routes:
            route = str(pattern.pattern)
            if route in self.PUBLIC_ROUTES:
                continue
            path = "/api/" + route
            for converter, value in samples.items():
                path = __import__("re").sub(rf"<{converter}:\w+>", value, path)
            for method in ("get", "post", "patch", "delete"):
                response = getattr(anonymous, method)(path, {}, format="json")
                self.assertIn(response.status_code, (401, 403), f"{method.upper()} {path} answered {response.status_code} without sign-in")
