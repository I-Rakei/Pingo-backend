import calendar
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Client, Debt, Installment, Payment

MONEY = Decimal("0.01")


def money(value):
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def add_months(value, months):
    index = value.month - 1 + months
    year, month = value.year + index // 12, index % 12 + 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def next_reference():
    numbers = [int(item.reference[4:]) for item in Debt.objects.only("reference") if item.reference.startswith("PNG-") and item.reference[4:].isdigit()]
    return f"PNG-{max(numbers, default=1000) + 1}"


@transaction.atomic
def create_debt(owner, data):
    client_id = data.get("clientId")
    if client_id:
        try:
            client = Client.objects.get(owner=owner, pk=client_id)
        except Client.DoesNotExist as exc:
            raise ValidationError({"clientId": "Client not found."}) from exc
    else:
        client, _ = Client.objects.get_or_create(owner=owner, name=data["name"], defaults={"notes": "Created with a debt record."})
    principal = money(data["principal"])
    rate = Decimal(data["interestRate"])
    duration = data["durationMonths"]
    interest = money(principal * rate / 100)
    total = money(principal + interest)
    debt = Debt.objects.create(owner=owner, client=client, reference=next_reference(), loan_type=data["loanType"],
        principal=principal, capital_remaining=principal, interest_rate=rate, penalty_rate=data.get("penaltyRate", 0),
        duration_months=duration, total=total, outstanding=total, collected=Decimal("0"), start_date=data["startDate"], due_date=data["dueDate"])
    for number in range(1, duration + 1):
        due = add_months(debt.due_date, number - duration)
        amount = interest if debt.loan_type == Debt.LoanType.SINGLE else money(total / duration)
        Installment.objects.create(debt=debt, number=number, due_date=due, amount=amount)
    debt.set_status()
    debt.save(update_fields=["status", "updated_at"])
    return debt


def _save_debt(debt):
    debt.set_status()
    debt.save()


@transaction.atomic
def record_payment(owner, reference, data):
    debt = Debt.objects.select_for_update().prefetch_related("installments").get(owner=owner, reference=reference)
    if debt.status == Debt.Status.PAID:
        raise ValidationError("This debt is already paid.")
    action = data["action"]
    amount = money(data.get("amount") or 0)
    installments = list(debt.installments.all())
    operation_id = uuid.uuid4()
    created = []

    def add_payment(value, kind, installment=None):
        if value <= 0:
            return
        created.append(Payment.objects.create(owner=owner, debt=debt, client=debt.client, installment=installment,
            operation_id=operation_id, amount=money(value), payment_type=kind))

    if debt.loan_type == Debt.LoanType.MULTI:
        if action != "balance":
            raise ValidationError({"action": "Multi-period loans accept balance payments only."})
        remaining = amount
        selected_number = data.get("installmentNumber")
        choices = [item for item in installments if not selected_number or item.number == selected_number]
        for installment in choices:
            due = money(max(installment.amount - installment.paid_amount, 0))
            applied = min(remaining, due)
            if applied:
                installment.paid_amount = money(installment.paid_amount + applied)
                installment.save(update_fields=["paid_amount", "updated_at"])
                add_payment(applied, Payment.PaymentType.PRINCIPAL, installment)
                remaining = money(remaining - applied)
            if remaining <= 0:
                break
        if not created:
            raise ValidationError({"amount": "No outstanding installment balance is available."})
        applied_total = sum((item.amount for item in created), Decimal("0"))
        debt.collected = money(debt.collected + applied_total)
        debt.outstanding = money(sum((max(item.amount - item.paid_amount, 0) for item in installments), Decimal("0")))
    else:
        open_installment = next((item for item in installments if item.paid_amount < item.amount), None)
        capital = debt.capital_remaining
        interest_due = money((open_installment.amount - open_installment.paid_amount) if open_installment else capital * debt.interest_rate / 100)
        if action == "interest":
            if interest_due <= 0:
                raise ValidationError("No interest is currently due.")
            if open_installment is None:
                open_installment = Installment.objects.create(debt=debt, number=len(installments) + 1, due_date=debt.due_date, amount=interest_due)
                installments.append(open_installment)
            open_installment.paid_amount = open_installment.amount
            open_installment.save(update_fields=["paid_amount", "updated_at"])
            add_payment(interest_due, Payment.PaymentType.INTEREST, open_installment)
            next_due = add_months(open_installment.due_date, 1)
            next_interest = money(capital * debt.interest_rate / 100)
            Installment.objects.create(debt=debt, number=max(item.number for item in installments) + 1, due_date=next_due, amount=next_interest)
            debt.due_date, debt.total, debt.outstanding = next_due, money(capital + next_interest), money(capital + next_interest)
            debt.collected = money(debt.collected + interest_due)
        elif action == "principal":
            applied = min(amount, capital)
            if applied <= 0:
                raise ValidationError({"amount": "No capital remains."})
            debt.capital_remaining = money(capital - applied)
            new_interest = money(debt.capital_remaining * debt.interest_rate / 100)
            if open_installment:
                open_installment.amount = new_interest
                open_installment.paid_amount = min(open_installment.paid_amount, new_interest)
                open_installment.save(update_fields=["amount", "paid_amount", "updated_at"])
            debt.collected = money(debt.collected + applied)
            debt.total = money(debt.capital_remaining + new_interest) if debt.capital_remaining else Decimal("0")
            debt.outstanding = debt.total
            add_payment(applied, Payment.PaymentType.PRINCIPAL)
        elif action == "settle":
            settle_amount = money(capital + interest_due)
            if open_installment:
                open_installment.paid_amount = open_installment.amount
                open_installment.save(update_fields=["paid_amount", "updated_at"])
            add_payment(interest_due, Payment.PaymentType.INTEREST, open_installment)
            add_payment(capital, Payment.PaymentType.PRINCIPAL)
            debt.capital_remaining, debt.total, debt.outstanding = Decimal("0"), Decimal("0"), Decimal("0")
            debt.collected = money(debt.collected + settle_amount)
        else:
            raise ValidationError({"action": "Single loans require interest, principal, or settle."})
    _save_debt(debt)
    return debt, created


@transaction.atomic
def revert_installment(owner, reference, number):
    debt = Debt.objects.select_for_update().prefetch_related("installments").get(owner=owner, reference=reference)
    try:
        installment = debt.installments.get(number=number)
    except Installment.DoesNotExist as exc:
        raise ValidationError({"installmentNumber": "Installment not found."}) from exc
    payments = list(Payment.objects.select_for_update().filter(debt=debt, installment=installment, reversed_at__isnull=True))
    if not payments:
        raise ValidationError("There is no active payment for this installment.")
    if debt.loan_type == Debt.LoanType.SINGLE and debt.installments.filter(number__gt=number, paid_amount__gt=0).exists():
        raise ValidationError("Reverse later paid periods before reversing this one.")
    reversed_total = sum((item.amount for item in payments), Decimal("0"))
    now = timezone.now()
    for payment in payments:
        payment.reversed_at = now
        payment.save(update_fields=["reversed_at", "updated_at"])
    if debt.loan_type == Debt.LoanType.MULTI:
        installment.paid_amount = money(max(installment.paid_amount - reversed_total, 0))
        installment.save(update_fields=["paid_amount", "updated_at"])
        debt.collected = money(max(debt.collected - reversed_total, 0))
        current_installments = Installment.objects.filter(debt=debt)
        debt.outstanding = money(sum((max(row.amount - row.paid_amount, 0) for row in current_installments), Decimal("0")))
    else:
        # Paying interest creates the next period; rolling back removes that unearned period too.
        debt.installments.filter(number__gt=number, paid_amount=0).delete()
        installment.paid_amount = Decimal("0")
        installment.save(update_fields=["paid_amount", "updated_at"])
        debt.collected = money(max(debt.collected - reversed_total, 0))
        debt.outstanding = money(debt.capital_remaining + (installment.amount - installment.paid_amount))
        debt.total = debt.outstanding
        debt.due_date = installment.due_date
    _save_debt(debt)
    return debt, payments
