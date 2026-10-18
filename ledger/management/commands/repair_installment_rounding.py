from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from ledger.models import Debt
from ledger.services import equal_period_amounts, money, reconcile_debt


def base_of(installment):
    return installment.base_amount or money(installment.amount - installment.penalty_amount)


class Command(BaseCommand):
    help = ("Bring equal-period loans created before the whole-number rule in line with new loans. "
            "A debt with no payments yet gets the same whole-number instalments a new loan would. A debt that "
            "already has payments keeps its instalments, and only a rounding difference is moved onto its last "
            "unpaid instalment, so the schedule adds up to principal plus interest. Read-only unless --apply is given.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Save the corrections.")

    def handle(self, *args, **options):
        fixed = skipped = 0
        debts = Debt.objects.filter(loan_type=Debt.LoanType.MULTI).select_related("client").order_by("pk")
        # A list, not .iterator(): each rollback or commit resets SQLite cursors.
        for debt in list(debts):
            periods = list(debt.installments.order_by("number"))
            if not periods:
                continue
            # reconcile_debt() sets a multi-period total to the sum of its instalments, so the stored
            # total drifts along with a short schedule. Compare with what the terms say it should be.
            expected = money(debt.principal + money(debt.principal * debt.interest_rate / 100))
            label = f"{debt.reference} ({debt.client.name}, owner {debt.owner_id})"
            before = [period.amount for period in periods]

            if all(period.paid_amount == 0 for period in periods):
                target = equal_period_amounts(expected, len(periods))
                if [base_of(period) for period in periods] == target:
                    continue
                changes = [(period, new) for period, new in zip(periods, target) if base_of(period) != new]
            else:
                gap = money(expected - sum((base_of(period) for period in periods), Decimal("0")))
                if gap == 0:
                    continue
                last = periods[-1]
                # Rounding can only ever explain a fraction of a cent per period.
                if abs(gap) > Decimal("0.01") * len(periods):
                    skipped += 1
                    self.stdout.write(self.style.WARNING(f"{label}: gap {gap} is larger than rounding; left alone."))
                    continue
                if last.paid_amount >= last.amount:
                    skipped += 1
                    self.stdout.write(self.style.WARNING(f"{label}: last period already paid; gap {gap} left alone."))
                    continue
                changes = [(last, base_of(last) + gap)]

            with transaction.atomic():
                for period, new_base in changes:
                    period.base_amount = new_base
                    period.amount = money(new_base + period.penalty_amount)
                    period.save(update_fields=["base_amount", "amount", "updated_at"])
                reconcile_debt(debt)
                after = [period.amount for period in debt.installments.order_by("number")]
                if not options["apply"]:
                    transaction.set_rollback(True)
            fixed += 1
            self.stdout.write(f"{label}: instalments {[str(amount) for amount in before]} -> {[str(amount) for amount in after]}")
        verb = "Repaired" if options["apply"] else "Would repair"
        self.stdout.write(self.style.SUCCESS(f"{verb} {fixed} debt(s); {skipped} left alone."))
