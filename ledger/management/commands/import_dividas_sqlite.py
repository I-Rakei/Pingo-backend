import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ledger.models import Client, Debt, Installment, Payment
from ledger.services import money, next_reference


def as_date(value):
    text = str(value or date.today().isoformat())
    return date.fromisoformat(text[:10])


class Command(BaseCommand):
    help = "Import a Dividas Expo SQLite database into one Pingo user account. Safe to rerun."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to dividas_v3.db")
        parser.add_argument("--user", required=True, help="Existing Pingo user email")

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.is_file():
            raise CommandError(f"SQLite file not found: {path}")
        user = get_user_model().objects.filter(email__iexact=options["user"]).first()
        if not user:
            raise CommandError(f"No Django user exists with email {options['user']!r}.")
        source = sqlite3.connect(path)
        source.row_factory = sqlite3.Row
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"debts", "installments", "payments"}
        missing = required - tables
        if missing:
            raise CommandError(f"Not a supported Dividas database; missing tables: {', '.join(sorted(missing))}")
        warnings, counts = [], {"clients": 0, "debts": 0, "installments": 0, "payments": 0}
        rows = lambda table: source.execute(f"SELECT * FROM {table}").fetchall()  # Table names are fixed above.
        client_map, debt_map, installment_map = {}, {}, {}
        with transaction.atomic():
            if "clients" in tables:
                for row in rows("clients"):
                    legacy_id = str(row["id"])
                    client, created = Client.objects.update_or_create(owner=user, legacy_id=legacy_id, defaults={
                        "name": row["name"], "phone": row["phone"] or "", "email": row["email"] or "", "notes": row["notes"] or "",
                    })
                    client_map[row["id"]] = client
                    counts["clients"] += int(created)
            for row in rows("debts"):
                legacy_id = str(row["id"])
                client = client_map.get(row["client_id"])
                if not client:
                    client, _ = Client.objects.get_or_create(owner=user, name=row["debtor_name"], defaults={"notes": "Imported from Dividas."})
                    warnings.append(f"Debt {legacy_id}: client was missing; matched/created by debtor_name.")
                existing = Debt.objects.filter(owner=user, legacy_id=legacy_id).first()
                reference = existing.reference if existing else f"PNG-{1000 + int(row['id'])}"
                if not existing and Debt.objects.filter(reference=reference).exists():
                    reference = next_reference()
                    warnings.append(f"Debt {legacy_id}: preferred reference already existed; allocated {reference}.")
                values = {
                    "client": client, "reference": reference, "loan_type": row["loan_type"] or "multi",
                    "principal": money(row["amount"]), "capital_remaining": money(row["capital_remaining"] if row["capital_remaining"] is not None else row["amount"]),
                    "interest_rate": Decimal(str(row["interest_rate"])), "penalty_rate": Decimal(str(row["penalty_rate"] or 0)),
                    "duration_months": int(row["duration_months"] or 1), "total": money(row["total_amount"]), "outstanding": money(row["total_amount"]),
                    "collected": Decimal("0"), "start_date": as_date(row["start_date"]), "due_date": as_date(row["due_date"]),
                    "status": Debt.Status.PAID if row["is_paid"] else Debt.Status.UNPAID,
                }
                if existing:
                    for key, value in values.items(): setattr(existing, key, value)
                    existing.save()
                    debt = existing
                else:
                    debt = Debt.objects.create(owner=user, legacy_id=legacy_id, **values)
                    counts["debts"] += 1
                debt_map[row["id"]] = debt
            for row in rows("installments"):
                debt = debt_map.get(row["debt_id"])
                if not debt:
                    warnings.append(f"Installment {row['id']}: referenced an unavailable debt {row['debt_id']}.")
                    continue
                amount, paid = money(row["total_amount"]), money(row["paid_amount"] or (row["total_amount"] if row["is_paid"] else 0))
                installment, created = Installment.objects.update_or_create(debt=debt, number=int(row["installment_number"]), defaults={
                    "due_date": as_date(row["due_date"]), "amount": amount, "paid_amount": min(paid, amount),
                })
                installment_map[row["id"]] = installment
                counts["installments"] += int(created)
            for debt in debt_map.values():
                paid = sum((item.paid_amount for item in debt.installments.all()), Decimal("0"))
                if debt.loan_type == Debt.LoanType.SINGLE:
                    open_interest = sum((item.amount - item.paid_amount for item in debt.installments.all()), Decimal("0"))
                    debt.outstanding = money(0 if debt.status == Debt.Status.PAID else debt.capital_remaining + open_interest)
                else:
                    debt.outstanding = money(0 if debt.status == Debt.Status.PAID else sum((item.amount - item.paid_amount for item in debt.installments.all()), Decimal("0")))
                debt.collected = paid
                debt.set_status()
                debt.save()
                if debt.loan_type == Debt.LoanType.MULTI and abs((debt.total - debt.outstanding) - debt.collected) > Decimal("0.02"):
                    warnings.append(f"Debt {debt.legacy_id}: installment totals do not reconcile to legacy total_amount.")
            for row in rows("payments"):
                debt = debt_map.get(row["debt_id"])
                if not debt:
                    warnings.append(f"Payment {row['id']}: referenced an unavailable debt {row['debt_id']}.")
                    continue
                legacy_id = str(row["id"])
                kind = row["type"] if "type" in row.keys() and row["type"] in {"principal", "interest"} else "principal"
                payment, created = Payment.objects.update_or_create(owner=user, legacy_id=legacy_id, defaults={
                    "debt": debt, "client": debt.client, "installment": installment_map.get(row["installment_id"]),
                    "amount": money(row["amount"]), "payment_type": kind, "payment_date": as_date(row["created_at"]),
                })
                counts["payments"] += int(created)
            for debt in debt_map.values():
                if debt.loan_type != Debt.LoanType.SINGLE:
                    continue
                paid_interest = sum((item.paid_amount for item in debt.installments.all()), Decimal("0"))
                paid_principal = sum((
                    item.amount for item in debt.payments.filter(
                        installment__isnull=True,
                        payment_type=Payment.PaymentType.PRINCIPAL,
                        reversed_at__isnull=True,
                    )
                ), Decimal("0"))
                debt.collected = money(paid_interest + paid_principal)
                debt.save(update_fields=["collected", "updated_at"])
        source.close()
        self.stdout.write(self.style.SUCCESS("Imported Dividas SQLite data (new rows): " + ", ".join(f"{key}={value}" for key, value in counts.items())))
        for warning in warnings:
            self.stdout.write(self.style.WARNING("WARNING: " + warning))
