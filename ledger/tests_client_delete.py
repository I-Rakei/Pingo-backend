from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from .models import Client, Debt, Invoice, Organization, OrganizationMembership, Payment, SyncChange


class ClientDeleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="delete-owner", password="test-password")
        self.client_row = Client.objects.create(owner=self.user, name="Ana", share_token="delete-share-link")
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.url = f"/api/clients/{self.client_row.pk}/"

    def test_delete_removes_client_revokes_share_and_emits_scoped_tombstone(self):
        self.assertEqual(self.api.delete(self.url).status_code, 204)
        self.assertFalse(Client.objects.filter(pk=self.client_row.pk).exists())
        self.assertEqual(APIClient().get("/api/public/clients/delete-share-link/").status_code, 404)
        self.assertTrue(SyncChange.objects.filter(owner=self.user, entity="client",
            entity_id=self.client_row.public_id, op="delete").exists())
        self.assertEqual(self.api.delete(self.url).status_code, 404)

    def test_other_account_and_anonymous_cannot_delete(self):
        other = User.objects.create_user(username="delete-other")
        self.api.force_authenticate(other)
        self.assertEqual(self.api.delete(self.url).status_code, 404)
        self.assertIn(APIClient().delete(self.url).status_code, (401, 403))
        self.assertTrue(Client.objects.filter(pk=self.client_row.pk).exists())

    def test_session_delete_requires_csrf(self):
        session = APIClient(enforce_csrf_checks=True)
        session.force_login(self.user)
        self.assertEqual(session.delete(self.url).status_code, 403)
        csrf = session.get("/api/auth/csrf/").data["csrfToken"]
        self.assertEqual(session.delete(self.url, HTTP_X_CSRFTOKEN=csrf).status_code, 204)

    def test_debts_and_payments_are_preserved(self):
        debt = self.api.post("/api/debts/", {"clientId": self.client_row.pk, "loanType": "multi",
            "principal": "100", "interestRate": "0", "penaltyRate": "0", "durationMonths": 1,
            "startDate": "2099-01-01", "dueDate": "2099-02-01"}, format="json")
        self.assertEqual(debt.status_code, 201, debt.data)
        payment = self.api.post(f"/api/debts/{debt.data['id']}/payments/",
            {"action": "balance", "amount": "100"}, format="json")
        self.assertEqual(payment.status_code, 200, payment.data)
        response = self.api.delete(self.url)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "client_has_records")
        self.assertTrue(Client.objects.filter(pk=self.client_row.pk).exists())
        self.assertEqual(Debt.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertFalse(SyncChange.objects.filter(entity_id=self.client_row.public_id, op="delete").exists())

    def test_corporate_scope_and_protected_documents(self):
        organization = Organization.objects.create(name="Delete test org", created_by=self.user)
        OrganizationMembership.objects.create(user=self.user, organization=organization, role="owner")
        staff = User.objects.create_user(username="delete-staff")
        OrganizationMembership.objects.create(user=staff, organization=organization, role="staff")
        corporate = Client.objects.create(owner=staff, organization=organization, name="Company client")
        url = f"/api/clients/{corporate.pk}/"
        # A corporate ledger must not expose the caller's old personal records.
        self.assertEqual(self.api.delete(self.url).status_code, 404)
        document = Invoice.objects.create(organization=organization, issued_by=staff,
            client=corporate, number=1, amount="50")
        self.assertEqual(self.api.delete(url).status_code, 409)
        self.assertTrue(Invoice.objects.filter(pk=document.pk).exists())
        empty = Client.objects.create(owner=staff, organization=organization, name="Unused")
        self.assertEqual(self.api.delete(f"/api/clients/{empty.pk}/").status_code, 204)
        self.assertTrue(SyncChange.objects.filter(organization=organization, entity_id=empty.public_id, op="delete").exists())

    def test_legacy_phone_accepts_deleted_client_without_resurrection(self):
        self.assertEqual(self.api.delete(self.url).status_code, 204)
        stale = {"clients": [{"localId": 1, "serverId": str(self.client_row.public_id), "name": "Ana"}],
                 "debts": [], "installments": [], "payments": []}
        response = self.api.post("/api/mobile/sync/", {"deviceId": "delete-phone", "batchId": "stale",
            "snapshot": stale}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["snapshot"]["clients"], [])
        self.assertEqual(Client.objects.count(), 0)
        stale["debts"] = [{"localId": 1, "clientLocalId": 1}]
        response = self.api.post("/api/mobile/sync/", {"deviceId": "delete-phone", "batchId": "offline-debt",
            "snapshot": stale}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("still has debts", str(response.data))

    def test_foreign_tombstone_does_not_validate_a_legacy_client(self):
        self.assertEqual(self.api.delete(self.url).status_code, 204)
        self.api.force_authenticate(User.objects.create_user(username="foreign-phone"))
        response = self.api.post("/api/mobile/sync/", {"deviceId": "foreign-phone", "batchId": "one",
            "snapshot": {"clients": [{"localId": 1, "serverId": str(self.client_row.public_id), "name": "Ana"}]}}, format="json")
        self.assertEqual(response.status_code, 400)
