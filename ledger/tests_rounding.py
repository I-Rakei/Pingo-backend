"""Equal-period schedules must add up to the debt total to the cent."""

from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from ledger.models import Debt, Installment, Payment
from ledger.services import create_debt, loan_installment_terms, money, reconcile_debt


def amounts(loan_type, principal, rate, duration):
    return [amount for _, _, amount in loan_installment_terms(loan_type, Decimal(principal), Decimal(rate), duration, date(2099, 6, 30))]


def new_debt(owner, name="Estrela David"):
    return create_debt(owner, {
        "name": name, "loanType": Debt.LoanType.MULTI, "principal": "7000", "interestRate": "30",
        "durationMonths": 3, "startDate": date(2099, 1, 1), "dueDate": date(2099, 4, 1),
    })


class InstallmentRoundingTests(TestCase):
    def test_seven_thousand_at_thirty_percent_over_three_months(self):
        # 9100.00 / 3 = 3033.333...: the invoice used to show 3 x 3033.33 = 9099.99.
        self.assertEqual(amounts(Debt.LoanType.MULTI, "7000", "30", 3), [Decimal("3033.33"), Decimal("3033.33"), Decimal("3033.34")])

    def test_schedule_always_sums_to_principal_plus_interest(self):
        for principal, rate, duration in [("7000", "30", 3), ("100", "10", 3), ("1000", "7", 7), ("250.50", "12.5", 6),
                                          ("99.99", "3", 11), ("100", "10", 1), ("5", "0", 3)]:
            total = money(Decimal(principal) + money(Decimal(principal) * Decimal(rate) / 100))
            schedule = amounts(Debt.LoanType.MULTI, principal, rate, duration)
            self.assertEqual(len(schedule), duration)
            self.assertEqual(sum(schedule), total, (principal, rate, duration, schedule))
            # Only the last period differs, and only by a few cents.
            self.assertTrue(all(amount == schedule[0] for amount in schedule[:-1]))
            self.assertLessEqual(abs(schedule[-1] - schedule[0]), Decimal("0.05"))

    def test_single_loans_keep_one_interest_amount_per_period(self):
        self.assertEqual(amounts(Debt.LoanType.SINGLE, "18500", "8", 1), [Decimal("1480.00")])

    def test_created_debt_keeps_its_total_after_reconciling(self):
        owner = User.objects.create_user("rounding@example.com", "rounding@example.com", "password-123")
        debt = new_debt(owner)
        reconcile_debt(debt)
        debt.refresh_from_db()
        self.assertEqual(debt.total, Decimal("9100.00"))
        self.assertEqual(debt.outstanding, Decimal("9100.00"))
        self.assertEqual(sum(item.amount for item in debt.installments.all()), Decimal("9100.00"))


class RepairInstallmentRoundingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("repair@example.com", "repair@example.com", "password-123")

    def short_debt(self, name="Estrela David"):
        """A debt as created before the fix: three instalments of 3033.33 against a total of 9100.00."""
        debt = new_debt(self.owner, name)
        last = debt.installments.order_by("number").last()
        last.base_amount = last.amount = Decimal("3033.33")
        last.save(update_fields=["base_amount", "amount", "updated_at"])
        return debt

    def run_command(self, *args):
        out = StringIO()
        call_command("repair_installment_rounding", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        debt = self.short_debt()
        output = self.run_command()
        self.assertIn("Would repair 1 debt(s)", output)
        self.assertEqual(debt.installments.order_by("number").last().amount, Decimal("3033.33"))

    def test_apply_moves_the_missing_cent_onto_the_last_period(self):
        debt = self.short_debt()
        output = self.run_command("--apply")
        self.assertIn("Repaired 1 debt(s)", output)
        debt.refresh_from_db()
        self.assertEqual([item.amount for item in debt.installments.order_by("number")],
                         [Decimal("3033.33"), Decimal("3033.33"), Decimal("3033.34")])
        self.assertEqual(debt.total, Decimal("9100.00"))
        self.assertEqual(debt.outstanding, Decimal("9100.00"))
        # Running it again finds nothing left to do.
        self.assertIn("Repaired 0 debt(s)", self.run_command("--apply"))

    def test_a_debt_whose_last_period_is_paid_is_left_alone(self):
        debt = self.short_debt()
        last = debt.installments.order_by("number").last()
        Payment.objects.create(owner=self.owner, debt=debt, client=debt.client, installment=last, amount=Decimal("3033.33"),
                               payment_type="principal", payment_date=date(2099, 1, 2))
        reconcile_debt(debt)
        output = self.run_command("--apply")
        self.assertIn("last period already paid", output)
        self.assertEqual(Installment.objects.get(pk=last.pk).amount, Decimal("3033.33"))
