"""Equal-period schedules use whole-number instalments and still add up to the debt total to the cent."""

from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from ledger import pdf_text
from ledger.models import Debt, Installment, Payment
from ledger.pdf_documents import amounts_due
from ledger.serializers import _public_debt_statement
from ledger.services import (
    amortization_schedule_for_debt, capital_portions, create_debt, equal_period_amounts,
    loan_installment_terms, money, reconcile_debt,
)


def amounts(loan_type, principal, rate, duration):
    return [amount for _, _, amount in loan_installment_terms(loan_type, Decimal(principal), Decimal(rate), duration, date(2099, 6, 30))]


def new_debt(owner, name="Estrela David"):
    return create_debt(owner, {
        "name": name, "loanType": Debt.LoanType.MULTI, "principal": "7000", "interestRate": "30",
        "durationMonths": 3, "startDate": date(2099, 1, 1), "dueDate": date(2099, 4, 1),
    })


class WholeNumberInstalmentTests(TestCase):
    def test_seven_thousand_at_thirty_percent_over_three_months(self):
        # 9100.00 / 3 = 3033.333...: whole-number instalments, the last one takes the remainder.
        self.assertEqual(amounts(Debt.LoanType.MULTI, "7000", "30", 3), [Decimal("3033.00"), Decimal("3033.00"), Decimal("3034.00")])

    def test_split_examples(self):
        self.assertEqual(equal_period_amounts(Decimal("110.00"), 3), [Decimal("36.00"), Decimal("36.00"), Decimal("38.00")])
        self.assertEqual(equal_period_amounts(Decimal("1000"), 4), [Decimal("250.00")] * 4)
        self.assertEqual(equal_period_amounts(Decimal("100"), 1), [Decimal("100.00")])
        # A total with cents keeps them, on the last instalment only.
        self.assertEqual(equal_period_amounts(Decimal("9100.50"), 3), [Decimal("3033.00"), Decimal("3033.00"), Decimal("3034.50")])

    def test_tiny_totals_fall_back_to_cents_instead_of_zero_instalments(self):
        schedule = equal_period_amounts(Decimal("2.00"), 3)
        self.assertEqual(schedule, [Decimal("0.67"), Decimal("0.67"), Decimal("0.66")])
        self.assertEqual(sum(schedule), Decimal("2.00"))

    def test_schedule_always_sums_to_principal_plus_interest(self):
        for principal, rate, duration in [("7000", "30", 3), ("100", "10", 3), ("1000", "7", 7), ("250.50", "12.5", 6),
                                          ("99.99", "3", 11), ("100", "10", 1), ("5", "0", 3), ("12345.67", "17.5", 9)]:
            total = money(Decimal(principal) + money(Decimal(principal) * Decimal(rate) / 100))
            schedule = amounts(Debt.LoanType.MULTI, principal, rate, duration)
            self.assertEqual(len(schedule), duration)
            self.assertEqual(sum(schedule), total, (principal, rate, duration, schedule))
            self.assertTrue(all(amount == schedule[0] for amount in schedule[:-1]))
            if total >= duration:
                self.assertEqual(schedule[0], schedule[0].to_integral_value(), "instalments are whole numbers")
                self.assertGreaterEqual(schedule[-1], schedule[0])
                self.assertLess(schedule[-1] - schedule[0], duration + 1)

    def test_single_loans_keep_one_exact_interest_amount_per_period(self):
        self.assertEqual(amounts(Debt.LoanType.SINGLE, "18500", "8", 1), [Decimal("1480.00")])

    def test_created_debt_keeps_its_total_after_reconciling(self):
        owner = User.objects.create_user("rounding@example.com", "rounding@example.com", "password-123")
        debt = new_debt(owner)
        reconcile_debt(debt)
        debt.refresh_from_db()
        self.assertEqual(debt.total, Decimal("9100.00"))
        self.assertEqual(debt.outstanding, Decimal("9100.00"))
        self.assertEqual([item.amount for item in debt.installments.order_by("number")], [Decimal("3033.00"), Decimal("3033.00"), Decimal("3034.00")])


class CapitalInterestSplitTests(TestCase):
    """Capital and interest inside each instalment are whole numbers too, and still add up exactly."""

    def portions(self, principal, rate, duration):
        total = money(Decimal(principal) + money(Decimal(principal) * Decimal(rate) / 100))
        bases = equal_period_amounts(total, duration)
        capital = capital_portions(Decimal(principal), bases)
        return bases, capital, [base - share for base, share in zip(bases, capital)]

    def test_the_seven_thousand_example(self):
        bases, capital, interest = self.portions("7000", "30", 3)
        self.assertEqual(capital, [Decimal("2334.00"), Decimal("2334.00"), Decimal("2332.00")])
        self.assertEqual(interest, [Decimal("699.00"), Decimal("699.00"), Decimal("702.00")])

    def test_always_whole_when_the_instalments_are_and_always_exact(self):
        for principal, rate, duration in [("7000", "30", 3), ("12000", "10", 7), ("50000", "15", 6), ("1000", "7", 7),
                                          ("250.50", "12.5", 6), ("12345.67", "17.5", 9), ("100", "1", 12), ("100", "0", 3), ("19980", "8", 12)]:
            bases, capital, interest = self.portions(principal, rate, duration)
            case = (principal, rate, duration, capital, interest)
            self.assertEqual(sum(capital), money(Decimal(principal)), case)
            self.assertEqual(sum(interest), money(sum(bases) - Decimal(principal)), case)
            self.assertTrue(all(Decimal("0") <= share <= base for share, base in zip(capital, bases)), case)
            self.assertTrue(all(share >= 0 for share in interest), case)
            if Decimal(principal) == Decimal(principal).to_integral_value() and Decimal(rate) * Decimal(principal) % 100 == 0:
                self.assertTrue(all(share == share.to_integral_value() for share in capital + interest), case)

    def test_the_amortization_schedule_shows_whole_capital_and_interest(self):
        owner = User.objects.create_user("split@example.com", "split@example.com", "password-123")
        debt = new_debt(owner)
        rows = amortization_schedule_for_debt(debt)["installments"]
        self.assertEqual([row["principalAmount"] for row in rows], [Decimal("2334.00"), Decimal("2334.00"), Decimal("2332.00")])
        self.assertEqual([row["interestAmount"] for row in rows], [Decimal("699.00"), Decimal("699.00"), Decimal("702.00")])
        self.assertEqual(rows[-1]["capitalBalance"], Decimal("0.00"))

    def test_the_clients_statement_and_the_invoice_split_what_is_still_owed_in_whole_numbers(self):
        owner = User.objects.create_user("statement@example.com", "statement@example.com", "password-123")
        debt = new_debt(owner)
        summary, _ = _public_debt_statement(debt)
        self.assertEqual((summary["capital"], summary["interest"], summary["total"]), (Decimal("7000.00"), Decimal("2100.00"), Decimal("9100.00")))

        first = debt.installments.order_by("number").first()
        Payment.objects.create(owner=owner, debt=debt, client=debt.client, installment=first, amount=first.amount,
                               payment_type="principal", payment_date=date(2099, 1, 2))
        reconcile_debt(debt)
        debt.refresh_from_db()
        summary, _ = _public_debt_statement(debt)
        # Periods 2 and 3 remain: capital 2334 + 2332, interest 699 + 702 (the old ratio gave 4666.92 / 1400.08).
        self.assertEqual((summary["capital"], summary["interest"], summary["total"]), (Decimal("4666.00"), Decimal("1401.00"), Decimal("6067.00")))
        owed = amounts_due(debt, pdf_text.EN)
        self.assertEqual((owed["capital"], owed["interest"], owed["total"]), (Decimal("4666.00"), Decimal("1401.00"), Decimal("6067.00")))


class RepairInstallmentRoundingTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("repair@example.com", "repair@example.com", "password-123")

    def old_style_debt(self, name="Estrela David"):
        """A debt as created before the change: three instalments of 3033.33 against 9100.00."""
        debt = new_debt(self.owner, name)
        for item in debt.installments.all():
            item.base_amount = item.amount = Decimal("3033.33")
            item.save(update_fields=["base_amount", "amount", "updated_at"])
        reconcile_debt(debt)
        return debt

    def run_command(self, *args):
        out = StringIO()
        call_command("repair_installment_rounding", *args, stdout=out)
        return out.getvalue()

    def amounts_of(self, debt):
        return [item.amount for item in debt.installments.order_by("number")]

    def test_dry_run_changes_nothing(self):
        debt = self.old_style_debt()
        output = self.run_command()
        self.assertIn("Would repair 1 debt(s)", output)
        self.assertEqual(self.amounts_of(debt), [Decimal("3033.33")] * 3)

    def test_a_debt_without_payments_gets_whole_number_instalments(self):
        debt = self.old_style_debt()
        self.assertIn("Repaired 1 debt(s)", self.run_command("--apply"))
        debt.refresh_from_db()
        self.assertEqual(self.amounts_of(debt), [Decimal("3033.00"), Decimal("3033.00"), Decimal("3034.00")])
        self.assertEqual(debt.total, Decimal("9100.00"))
        self.assertEqual(debt.outstanding, Decimal("9100.00"))
        self.assertIn("Repaired 0 debt(s)", self.run_command("--apply"))

    def test_a_debt_with_payments_only_gets_the_rounding_gap_fixed(self):
        debt = self.old_style_debt()
        first = debt.installments.order_by("number").first()
        Payment.objects.create(owner=self.owner, debt=debt, client=debt.client, installment=first, amount=Decimal("3033.33"),
                               payment_type="principal", payment_date=date(2099, 1, 2))
        reconcile_debt(debt)
        self.assertIn("Repaired 1 debt(s)", self.run_command("--apply"))
        debt.refresh_from_db()
        self.assertEqual(self.amounts_of(debt), [Decimal("3033.33"), Decimal("3033.33"), Decimal("3033.34")])
        self.assertEqual(debt.outstanding, Decimal("6066.67"))

    def test_a_debt_whose_last_period_is_paid_is_left_alone(self):
        debt = self.old_style_debt()
        for item in debt.installments.all():
            Payment.objects.create(owner=self.owner, debt=debt, client=debt.client, installment=item, amount=Decimal("3033.33"),
                                   payment_type="principal", payment_date=date(2099, 1, 2))
        reconcile_debt(debt)
        output = self.run_command("--apply")
        self.assertIn("last period already paid", output)
        self.assertEqual(Installment.objects.filter(debt=debt, amount=Decimal("3033.33")).count(), 3)
