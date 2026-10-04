from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .mobile_sync import _apply_deletions, snapshot_for_user
from .models import Client, Debt, DebtReferenceSequence, Payment
from .services import create_debt, reconcile_debt, record_payment, revert_installment


class GroundworkTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="phase0@example.com", password="test-password")
        self.client = Client.objects.create(owner=self.user, name="Ana")

    def debt(self, *, loan_type="multi", due_date=None, penalty_rate="0"):
        due_date = due_date or timezone.localdate() + timedelta(days=60)
        return create_debt(self.user, {
            "clientId": self.client.pk, "loanType": loan_type, "principal": Decimal("100.00"),
            "interestRate": Decimal("10.00"), "penaltyRate": Decimal(penalty_rate),
            "durationMonths": 1, "startDate": due_date - timedelta(days=30), "dueDate": due_date,
        })

    def test_reconcile_multi_uses_active_payment_facts_and_is_idempotent(self):
        debt = self.debt()
        record_payment(self.user, debt.reference, {"action": "balance", "amount": "40.00"})
        debt.refresh_from_db()
        reconcile_debt(debt)
        debt.refresh_from_db()
        self.assertEqual((debt.collected, debt.outstanding), (Decimal("40.00"), Decimal("70.00")))
        self.assertEqual(debt.installments.get().paid_amount, Decimal("40.00"))
        revert_installment(self.user, debt.reference, 1)
        debt.refresh_from_db()
        reconcile_debt(debt)
        debt.refresh_from_db()
        self.assertEqual((debt.collected, debt.outstanding), (Decimal("0.00"), Decimal("110.00")))
        self.assertEqual(Payment.objects.filter(debt=debt, reversed_at__isnull=False).count(), 1)

    def test_reconcile_single_principal_interest_and_settle(self):
        debt = self.debt(loan_type="single")
        record_payment(self.user, debt.reference, {"action": "principal", "amount": "40.00"})
        debt.refresh_from_db()
        self.assertEqual((debt.capital_remaining, debt.outstanding), (Decimal("60.00"), Decimal("66.00")))
        record_payment(self.user, debt.reference, {"action": "interest"})
        debt.refresh_from_db()
        self.assertEqual((debt.collected, debt.outstanding), (Decimal("46.00"), Decimal("66.00")))
        record_payment(self.user, debt.reference, {"action": "settle"})
        debt.refresh_from_db()
        reconcile_debt(debt)
        debt.refresh_from_db()
        self.assertEqual((debt.collected, debt.outstanding, debt.status), (Decimal("112.00"), Decimal("0.00"), Debt.Status.PAID))

    def test_penalty_is_server_assessed_once_from_base_amount(self):
        debt = self.debt(due_date=timezone.localdate() - timedelta(days=1), penalty_rate="5")
        reconcile_debt(debt)
        debt.refresh_from_db()
        installment = debt.installments.get()
        self.assertEqual((installment.base_amount, installment.penalty_amount, installment.amount),
                         (Decimal("110.00"), Decimal("5.50"), Decimal("115.50")))
        self.assertEqual(debt.outstanding, Decimal("115.50"))
        reconcile_debt(debt)
        self.assertEqual(debt.installments.get().amount, Decimal("115.50"))

    def test_debt_read_assesses_overdue_penalty(self):
        debt = self.debt(due_date=timezone.localdate() - timedelta(days=1), penalty_rate="5")
        api = APIClient()
        api.force_authenticate(self.user)
        response = api.get("/api/debts/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Decimal(str(response.data[0]["outstanding"])), Decimal("115.50"))
        debt.refresh_from_db()
        self.assertEqual(debt.installments.get().penalty_amount, Decimal("5.50"))

    def test_payment_assesses_penalty_before_allocation(self):
        debt = self.debt(due_date=timezone.localdate() - timedelta(days=1), penalty_rate="5")
        record_payment(self.user, debt.reference, {"action": "balance", "amount": "110.00"})
        debt.refresh_from_db()
        self.assertEqual((debt.collected, debt.outstanding), (Decimal("110.00"), Decimal("5.50")))

    def test_v1_payment_deletion_is_a_soft_reversal(self):
        debt = self.debt()
        _, payments = record_payment(self.user, debt.reference, {"action": "balance", "amount": "20.00"})
        payment = payments[0]
        _apply_deletions(self.user, [{"entity": "payment", "serverId": str(payment.public_id)}])
        payment.refresh_from_db()
        self.assertIsNotNone(payment.reversed_at)
        self.assertEqual(snapshot_for_user(self.user)["payments"], [])
        reconcile_debt(debt)
        debt.refresh_from_db()
        self.assertEqual(debt.outstanding, Decimal("110.00"))

    def test_reference_counter_allocates_unique_numbers(self):
        references = [self.debt().reference for _ in range(5)]
        self.assertEqual(references, [f"PNG-{number}" for number in range(1001, 1006)])
        self.assertEqual(DebtReferenceSequence.objects.get(name="PNG").last_number, 1005)
