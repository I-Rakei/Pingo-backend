from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from ledger.models import Debt
from ledger.services import money, reconcile_debt


def base_of(installment):
    return installment.base_amount or money(installment.amount - installment.penalty_amount)


class Command(BaseCommand):
    help = ("Equal-period loans created before the rounding fix have instalments that add up to a few cents "
            "less (or more) than the debt total. Move that difference onto the last instalment, as new loans do. "
            "Read-only unless --apply is given.")

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
            gap = money(expected - sum((base_of(period) for period in periods), Decimal("0")))
            if gap == 0:
                continue
            label = f"{debt.reference} ({debt.client.name}, owner {debt.owner_id})"
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
            before = (debt.outstanding, last.amount)
            with transaction.atomic():
                last.base_amount = base_of(last) + gap
                last.amount = money(last.amount + gap)
                last.save(update_fields=["base_amount", "amount", "updated_at"])
                reconcile_debt(debt)
                debt.refresh_from_db()
                if not options["apply"]:
                    transaction.set_rollback(True)
            fixed += 1
            self.stdout.write(f"{label}: period {last.number} {before[1]} -> {last.amount}; outstanding {before[0]} -> {debt.outstanding if options['apply'] else money(before[0] + gap)}")
        verb = "Repaired" if options["apply"] else "Would repair"
        self.stdout.write(self.style.SUCCESS(f"{verb} {fixed} debt(s); {skipped} left alone."))
