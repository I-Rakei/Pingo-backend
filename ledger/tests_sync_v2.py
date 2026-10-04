import uuid
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import OperationalError, close_old_connections, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from .models import (Client, Debt, Installment, Organization, OrganizationMembership,
                     Payment, SyncChange, SyncConflict, SyncCursorFloor)
from .services import create_debt, record_payment
from .sync_v2 import apply_mutation, bind_device, bootstrap, feed, latest_cursor


class SyncV2Tests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="v2@example.com", password="secret-pass")
        self.other = User.objects.create_user(username="other-v2@example.com", password="secret-pass")
        self.device_a = bind_device(self.user, str(uuid.uuid4()), "A")
        self.device_b = bind_device(self.user, str(uuid.uuid4()), "B")
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.user).key}")

    def make_client(self, name="Ana"):
        return Client.objects.create(owner=self.user, name=name)

    def debt(self, *, loan_type="multi"):
        client = self.make_client()
        return create_debt(self.user, {"clientId": client.pk, "loanType": loan_type,
            "principal": Decimal("100.00"), "interestRate": Decimal("10.00"),
            "penaltyRate": Decimal("0.00"), "durationMonths": 1,
            "startDate": timezone.localdate(), "dueDate": timezone.localdate() + timedelta(days=30)})

    def push(self, device, changes, *, mutation_id=None, action="test"):
        payload = {"type": "push", "mutationId": str(mutation_id or uuid.uuid4()),
                   "createdAt": timezone.now().isoformat(), "action": action, "changes": changes}
        return payload, apply_mutation(self.user, device, payload)

    def payment_change(self, debt, amount):
        return {"entity": "payment", "id": str(uuid.uuid4()), "op": "insert", "fields": {
            "debtId": str(debt.public_id), "installmentId": str(debt.installments.get().public_id),
            "operationId": str(uuid.uuid4()), "amount": amount, "type": "principal",
            "date": timezone.localdate().isoformat(), "note": ""}}

    def test_change_log_tracks_create_update_delete_and_cascade_in_scope(self):
        client = self.make_client()
        first = SyncChange.objects.get(entity="client", entity_id=client.public_id)
        self.assertEqual((first.owner_id, first.organization_id, first.op, first.fields["name"]),
                         (self.user.pk, None, "upsert", "Ana"))
        self.assertEqual(client.revision, first.pk)
        client.phone = "123"
        client.save(update_fields=["phone", "updated_at"])
        second = SyncChange.objects.filter(entity="client", entity_id=client.public_id).latest("pk")
        self.assertEqual(second.changed_fields, ["phone"])
        debt = create_debt(self.user, {"clientId": client.pk, "loanType": "multi",
            "principal": Decimal("100"), "interestRate": Decimal("10"), "penaltyRate": 0,
            "durationMonths": 1, "startDate": timezone.localdate(),
            "dueDate": timezone.localdate() + timedelta(days=30)})
        record_payment(self.user, debt.reference, {"action": "balance", "amount": "20"})
        ids = {"debt": debt.public_id, "installment": debt.installments.get().public_id,
               "payment": debt.payments.get().public_id}
        debt.delete()
        for entity, row_id in ids.items():
            self.assertTrue(SyncChange.objects.filter(entity=entity, entity_id=row_id, op="delete", owner=self.user).exists())
        client.delete()
        self.assertTrue(SyncChange.objects.filter(entity="client", entity_id=client.public_id, op="delete").exists())

    def test_v1_snapshot_writes_change_rows(self):
        snapshot = {"clients": [{"localId": "1", "name": "From phone", "createdAt": "2099-01-01"}],
                    "debts": [], "installments": [], "payments": []}
        response = self.api.post("/api/mobile/sync/", {"deviceId": "legacy-phone", "batchId": "one",
                                 "snapshot": snapshot}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(SyncChange.objects.filter(owner=self.user, entity="client", fields__name="From phone").exists())

    def test_http_hello_feed_bootstrap_auth_and_device_binding(self):
        client = self.make_client()
        device_id = str(uuid.uuid4())
        hello = {"type": "hello", "v": 2, "deviceId": device_id, "schemaVersion": 4,
                 "app": "mobile", "cursor": 0}
        response = self.api.post("/api/sync/v2/hello/", hello, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertGreaterEqual(response.data["latestCursor"], client.revision)
        feed_response = self.api.get(f"/api/sync/v2/changes/?deviceId={device_id}&cursor=0")
        self.assertEqual(feed_response.status_code, 200)
        self.assertEqual(feed_response.data["items"][0]["fields"]["name"], "Ana")
        boot = self.api.get(f"/api/sync/v2/bootstrap/?deviceId={device_id}")
        self.assertEqual(boot.data["items"][0]["id"], str(client.public_id))
        self.assertEqual(APIClient().get(f"/api/sync/v2/bootstrap/?deviceId={device_id}").status_code, 403)
        other_api = APIClient()
        other_api.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.other).key}")
        self.assertEqual(other_api.post("/api/sync/v2/hello/", hello, format="json").status_code, 400)
        self.assertEqual(other_api.get(f"/api/sync/v2/bootstrap/?deviceId={device_id}").status_code, 400)

    def test_payment_facts_from_two_devices_merge_and_retry_is_idempotent(self):
        debt = self.debt()
        first, a = self.push(self.device_a, [self.payment_change(debt, "20.00")])
        self.assertEqual(a["status"], "applied", a)
        before = SyncChange.objects.count()
        _, b = self.push(self.device_b, [self.payment_change(debt, "30.00")])
        self.assertEqual(b["status"], "applied", b)
        debt.refresh_from_db()
        self.assertEqual((debt.collected, debt.outstanding), (Decimal("50.00"), Decimal("60.00")))
        self.assertGreater(SyncChange.objects.count(), before)
        duplicate = apply_mutation(self.user, self.device_a, first)
        self.assertEqual(duplicate["status"], "duplicate")
        count = SyncChange.objects.count()
        altered = {**first, "action": "different"}
        self.assertEqual(apply_mutation(self.user, self.device_a, altered)["status"], "rejected")
        self.assertEqual(SyncChange.objects.count(), count)

    def test_http_push_uses_bound_device_and_returns_canonical_rows(self):
        device_id = str(uuid.uuid4())
        self.assertEqual(self.api.post("/api/sync/v2/hello/", {"type": "hello", "v": 2,
            "app": "mobile", "schemaVersion": 4, "deviceId": device_id, "cursor": 0}, format="json").status_code, 200)
        row_id = str(uuid.uuid4())
        payload = {"type": "push", "mutationId": str(uuid.uuid4()), "action": "add_client",
                   "changes": [{"entity": "client", "id": row_id, "op": "insert", "fields": {"name": "Mobile"}}]}
        response = self.api.post(f"/api/sync/v2/push/?deviceId={device_id}", payload, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "applied")
        self.assertEqual(response.data["rows"][0]["id"], row_id)
        retry = self.api.post(f"/api/sync/v2/push/?deviceId={device_id}", payload, format="json")
        self.assertEqual(retry.data["status"], "duplicate")
        self.assertEqual(self.api.post("/api/sync/v2/push/", payload, format="json").status_code, 400)
        same_row_new_mutation = {**payload, "mutationId": str(uuid.uuid4())}
        self.assertEqual(self.api.post(f"/api/sync/v2/push/?deviceId={device_id}",
                                       same_row_new_mutation, format="json").data["status"], "duplicate")

    def test_session_push_requires_csrf(self):
        api = APIClient(enforce_csrf_checks=True)
        api.force_login(self.user)
        response = api.post("/api/sync/v2/hello/", {"type": "hello", "v": 2,
            "app": "web", "deviceId": str(uuid.uuid4()), "cursor": 0}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_grouped_insert_assigns_reference_and_ignores_derived_fields(self):
        client_id, debt_id, installment_id, payment_id = (str(uuid.uuid4()) for _ in range(4))
        changes = [
            {"entity": "payment", "id": payment_id, "op": "insert", "fields": {
                "debtId": debt_id, "installmentId": installment_id, "operationId": str(uuid.uuid4()),
                "amount": "20.00", "type": "principal", "date": timezone.localdate().isoformat()}},
            {"entity": "installment", "id": installment_id, "op": "insert", "fields": {
                "debtId": debt_id, "number": 1, "baseAmount": "110.00", "amount": "9999.00",
                "dueDate": (timezone.localdate() + timedelta(days=30)).isoformat()}},
            {"entity": "debt", "id": debt_id, "op": "insert", "fields": {
                "clientId": client_id, "loanType": "multi", "principal": "100.00", "interestRate": "10.00",
                "penaltyRate": "0.00", "durationMonths": 1, "startDate": timezone.localdate().isoformat(),
                "dueDate": (timezone.localdate() + timedelta(days=30)).isoformat(),
                "collected": "9999.00"}},
            {"entity": "client", "id": client_id, "op": "insert", "fields": {"name": "New"}},
        ]
        _, result = self.push(self.device_a, changes)
        self.assertEqual(result["status"], "applied", result)
        debt = Debt.objects.get(public_id=debt_id)
        self.assertTrue(debt.reference.startswith("PNG-"))
        self.assertEqual((debt.collected, debt.outstanding), (Decimal("20.00"), Decimal("90.00")))
        self.assertEqual(Installment.objects.get(public_id=installment_id).amount, Decimal("110.00"))
        self.assertEqual(Payment.objects.get(public_id=payment_id).amount, Decimal("20.00"))

    def test_overpayment_is_kept_as_a_fact_and_flagged(self):
        debt = self.debt()
        _, result = self.push(self.device_a, [self.payment_change(debt, "120.00")])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["overpaid"], [str(debt.public_id)])
        debt.refresh_from_db()
        self.assertEqual((debt.collected, debt.outstanding), (Decimal("120.00"), Decimal("0.00")))

    def test_client_fields_merge_and_same_field_reports_overwrite(self):
        client = self.make_client()
        base = client.revision
        client.phone = "web phone"
        client.save(update_fields=["phone", "updated_at"])
        _, result = self.push(self.device_a, [{"entity": "client", "id": str(client.public_id),
            "op": "update", "baseRevision": base, "changedFields": {"email": "ana@example.com"}}])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["conflicts"], [])
        client.refresh_from_db()
        self.assertEqual((client.phone, client.email), ("web phone", "ana@example.com"))
        _, result = self.push(self.device_b, [{"entity": "client", "id": str(client.public_id),
            "op": "update", "baseRevision": base, "changedFields": {"phone": "mobile phone"}}])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["conflicts"][0]["reason"], "field_overwritten")
        client.refresh_from_db()
        self.assertEqual(client.phone, "mobile phone")

    def test_structural_conflict_and_protected_client_delete(self):
        debt = self.debt(loan_type="single")
        base = debt.revision
        def period(number):
            return {"entity": "installment", "id": str(uuid.uuid4()), "op": "insert",
                    "baseRevision": base, "fields": {"debtId": str(debt.public_id), "number": number,
                    "dueDate": (timezone.localdate() + timedelta(days=60)).isoformat(), "baseAmount": "10.00"}}
        _, first = self.push(self.device_a, [period(2)])
        self.assertEqual(first["status"], "applied", first)
        _, second = self.push(self.device_b, [period(3)])
        self.assertEqual((second["status"], second["conflicts"][0]["reason"]), ("conflict", "stale_base_revision"))
        self.assertEqual(debt.installments.count(), 2)
        _, deletion = self.push(self.device_b, [{"entity": "client", "id": str(debt.client.public_id),
            "op": "delete", "baseRevision": debt.client.revision}])
        self.assertEqual((deletion["status"], deletion["conflicts"][0]["reason"]), ("conflict", "has_dependents"))
        self.assertTrue(SyncConflict.objects.filter(reason="has_dependents").exists())

    def test_debt_delete_cascades_and_emits_child_tombstones(self):
        debt = self.debt()
        record_payment(self.user, debt.reference, {"action": "balance", "amount": "20.00"})
        installment_id = debt.installments.get().public_id
        payment_id = debt.payments.get().public_id
        debt.refresh_from_db()
        _, result = self.push(self.device_a, [{"entity": "debt", "id": str(debt.public_id),
            "op": "delete", "baseRevision": debt.revision}])
        self.assertEqual(result["status"], "applied", result)
        for entity, row_id in (("debt", debt.public_id), ("installment", installment_id), ("payment", payment_id)):
            self.assertTrue(SyncChange.objects.filter(entity=entity, entity_id=row_id, op="delete").exists())

    def test_direct_installment_delete_logs_payment_unlink(self):
        debt = self.debt()
        record_payment(self.user, debt.reference, {"action": "balance", "amount": "20.00"})
        installment = debt.installments.get()
        payment = debt.payments.get()
        installment.delete()
        payment.refresh_from_db()
        self.assertIsNone(payment.installment_id)
        self.assertTrue(any(change.fields.get("installmentId") is None
                            for change in SyncChange.objects.filter(entity="payment", entity_id=payment.public_id)))

    def test_cross_scope_uuid_is_rejected_and_feed_is_isolated(self):
        private = Client.objects.create(owner=self.other, name="Other")
        self.make_client()
        _, result = self.push(self.device_a, [{"entity": "client", "id": str(private.public_id),
            "op": "update", "baseRevision": private.revision, "changedFields": {"phone": "stolen"}}])
        self.assertEqual((result["status"], result["error"]), ("rejected", "not_found"))
        private.refresh_from_db()
        self.assertEqual(private.phone, "")
        self.assertNotIn(str(private.public_id), [item["id"] for item in feed(self.user, 0)["items"]])

    def test_invalid_group_rolls_back_every_row_and_persists_conflict(self):
        client_id = str(uuid.uuid4())
        _, result = self.push(self.device_a, [
            {"entity": "client", "id": client_id, "op": "insert", "fields": {"name": "Transient"}},
            {"entity": "debt", "id": str(uuid.uuid4()), "op": "insert", "fields": {
                "clientId": str(uuid.uuid4()), "loanType": "multi", "principal": "100.00",
                "interestRate": "10.00", "startDate": timezone.localdate().isoformat(),
                "dueDate": (timezone.localdate() + timedelta(days=30)).isoformat()}},
        ])
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(Client.objects.filter(public_id=client_id).exists())
        self.assertTrue(SyncConflict.objects.filter(reason="not_found").exists())

    def test_feed_pagination_and_expired_cursor(self):
        for index in range(5):
            self.make_client(f"Client {index}")
        all_ids = [item["cursor"] for item in feed(self.user, 0, 500)["items"]]
        cursor, seen = 0, []
        while True:
            page = feed(self.user, cursor, 2)
            seen.extend(item["cursor"] for item in page["items"])
            cursor = page["toCursor"]
            if not page["hasMore"]:
                break
        self.assertEqual(seen, all_ids)
        SyncCursorFloor.objects.create(owner=self.user, floor=seen[1])
        self.assertEqual(feed(self.user, 0)["type"], "resync_required")

    def test_corporate_scope_isolated_from_other_organization(self):
        org = Organization.objects.create(name="One", created_by=self.user)
        OrganizationMembership.objects.create(user=self.user, organization=org, role="owner")
        other_org = Organization.objects.create(name="Two", created_by=self.other)
        OrganizationMembership.objects.create(user=self.other, organization=other_org, role="owner")
        own = Client.objects.create(owner=self.user, organization=org, name="Shared")
        foreign = Client.objects.create(owner=self.other, organization=other_org, name="Private")
        ids = [item["id"] for item in bootstrap(self.user)["items"]]
        self.assertIn(str(own.public_id), ids)
        self.assertNotIn(str(foreign.public_id), ids)
        self.assertTrue(SyncChange.objects.filter(entity_id=own.public_id, organization=org, owner__isnull=True).exists())
        _, rejected = self.push(self.device_a, [{"entity": "client", "id": str(foreign.public_id),
            "op": "update", "baseRevision": foreign.revision, "changedFields": {"name": "No"}}])
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.api.get(f"/api/sync/v2/bootstrap/?deviceId={self.device_a.device_id}").status_code, 403)

    def test_compaction_preserves_latest_change_and_sets_floor(self):
        client = self.make_client()
        client.phone = "1"
        client.save(update_fields=["phone", "updated_at"])
        old = timezone.now() - timedelta(days=100)
        SyncChange.objects.filter(entity="client", entity_id=client.public_id).update(created_at=old)
        first = SyncChange.objects.filter(entity="client", entity_id=client.public_id).order_by("pk").first().pk
        call_command("compact_sync_log", days=90)
        self.assertFalse(SyncChange.objects.filter(pk=first).exists())
        self.assertEqual(SyncChange.objects.filter(entity_id=client.public_id).count(), 1)
        self.assertEqual(feed(self.user, 0)["type"], "resync_required")
        self.assertEqual(latest_cursor(self.user), client.revision)

    def test_synced_models_are_not_bulk_updated_outside_sync_log(self):
        root = Path(__file__).parent
        forbidden = re.compile(r"\b(?:Client|Debt|Installment|Payment)\.objects[^\n]*\.(?:update|bulk_create|bulk_update)\(")
        for path in root.glob("*.py"):
            if path.name.startswith("tests") or path.name == "sync_log.py":
                continue
            self.assertNotRegex(path.read_text(encoding="utf-8"), forbidden, path.name)


class ReferenceConcurrencyTests(TransactionTestCase):
    def test_concurrent_debts_get_distinct_references(self):
        user = User.objects.create_user(username="reference@example.com", password="secret-pass")
        client = Client.objects.create(owner=user, name="Borrower")

        def create(index):
            close_old_connections()
            try:
                for attempt in range(20):
                    try:
                        debt = create_debt(user, {"clientId": client.pk, "loanType": "multi",
                            "principal": Decimal("100.00"), "interestRate": Decimal("10.00"),
                            "penaltyRate": Decimal("0.00"), "durationMonths": 1,
                            "startDate": timezone.localdate(), "dueDate": timezone.localdate() + timedelta(days=30)})
                        return debt.reference
                    except OperationalError:
                        # Django's shared-memory test SQLite returns SQLITE_LOCKED
                        # immediately; a file-backed WAL database waits instead.
                        time.sleep(0.01 * (attempt + 1))
                self.fail("The test database stayed locked after retries")
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=3) as pool:
            references = list(pool.map(create, range(6)))
        self.assertEqual(len(set(references)), 6)
        self.assertEqual(Debt.objects.filter(owner=user).count(), 6)
