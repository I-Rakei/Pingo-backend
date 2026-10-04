from django.core.management.base import BaseCommand
from django.db import transaction

from ledger.models import Debt
from ledger.services import reconcile_debt

DEBT_FIELDS = ("capital_remaining", "total", "collected", "outstanding", "status")
PERIOD_FIELDS = ("amount", "penalty_amount", "paid_amount")


def snapshot(debt):
    periods = {item.number: tuple(getattr(item, field) for field in PERIOD_FIELDS)
               for item in debt.installments.order_by("number")}
    return {field: getattr(debt, field) for field in DEBT_FIELDS}, periods


class Command(BaseCommand):
    help = ("List debts whose stored balances differ from what reconcile_debt() derives from "
            "payment records. Read-only unless --apply is given. Run before deploying.")

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Save the reconciled values.")

    def handle(self, *args, **options):
        changed = 0
        total = 0
        for debt in Debt.objects.select_related("client").order_by("pk").iterator():
            total += 1
            before, before_periods = snapshot(debt)
            with transaction.atomic():
                reconcile_debt(debt)
                after, after_periods = snapshot(debt)
                if not options["apply"]:
                    transaction.set_rollback(True)
            lines = [f"    {field}: {before[field]} -> {after[field]}"
                     for field in DEBT_FIELDS if before[field] != after[field]]
            for number in sorted(set(before_periods) | set(after_periods)):
                old, new = before_periods.get(number), after_periods.get(number)
                if old != new:
                    lines.append(f"    period {number} (amount, penalty, paid): {old} -> {new}")
            if lines:
                changed += 1
                self.stdout.write(f"{debt.reference} ({debt.client.name}, owner {debt.owner_id}):")
                self.stdout.write("\n".join(lines))
        verb = "Updated" if options["apply"] else "Would update"
        self.stdout.write(self.style.SUCCESS(f"{verb} {changed} of {total} debts."))
