import calendar
import secrets
import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (AmortizationPlan, BalanceNote, Client, CreditNote, Debt, DebtReferenceSequence, DebitNote, DocumentSequence, Installment, Invoice,
                     Organization, OrganizationMembership, Payment)

MONEY = Decimal("0.01")

DOCUMENT_MODELS = {
    DocumentSequence.DocumentType.INVOICE: Invoice,
    DocumentSequence.DocumentType.DEBIT_NOTE: DebitNote,
    DocumentSequence.DocumentType.CREDIT_NOTE: CreditNote,
    DocumentSequence.DocumentType.BALANCE_NOTE: BalanceNote,
}


@transaction.atomic
def delete_client(client):
    """Delete the client's complete ledger and issued documents together."""
    client = Client.objects.select_for_update().get(pk=client.pk)
    for model in DOCUMENT_MODELS.values():
        model.objects.filter(Q(client=client) | Q(debt__client=client) | Q(payment__client=client)).delete()
    # Explicit ordering satisfies PROTECT relationships while model signals
    # record the same deletion in the mobile/web change feed.
    Payment.objects.filter(client=client).delete()
    Debt.objects.filter(client=client).delete()
    client.delete()


def money(value):
    return Decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP)


def _amortization_rows(principal, loan_type, installments, capital_paid=Decimal("0")):
    """Present ledger instalments without changing their recorded amounts.

    Multi-period capital is allocated across the stored base amounts. The final
    row absorbs cent rounding, so the capital column sums to the loan principal.
    Single loans keep capital separate from their interest-only periods.
    """
    rows = []
    bases = [item["base"] for item in installments]
    base_total = sum(bases, Decimal("0"))
    capital_left = principal
    for index, item in enumerate(installments):
        base = item["base"]
        if loan_type == Debt.LoanType.MULTI and base_total:
            planned_capital = capital_left if index == len(installments) - 1 else money(principal * base / base_total)
            capital = min(base, planned_capital)
            capital_left = money(capital_left - capital)
        else:
            capital = Decimal("0")
        amount = item["amount"]
        paid = item["paid"]
        rows.append({
            "number": item["number"], "rowType": "installment", "dueDate": item["dueDate"],
            "principalAmount": capital, "interestAmount": money(base - capital),
            "penaltyAmount": item["penalty"], "amount": amount, "paidAmount": paid,
            "remainingAmount": money(max(amount - paid, 0)),
            "capitalBalance": capital_left if loan_type == Debt.LoanType.MULTI else money(principal - capital_paid),
        })
    if loan_type == Debt.LoanType.SINGLE:
        rows.append({
            "number": len(rows) + 1, "rowType": "capital", "dueDate": installments[-1]["dueDate"],
            "principalAmount": principal, "interestAmount": Decimal("0"),
            "penaltyAmount": Decimal("0"), "amount": principal,
            "paidAmount": capital_paid, "remainingAmount": money(max(principal - capital_paid, 0)),
            "capitalBalance": Decimal("0"),
        })
    return rows


def _amortization_totals(rows):
    return {
        "principalTotal": money(sum((row["principalAmount"] for row in rows), Decimal("0"))),
        "interestTotal": money(sum((row["interestAmount"] for row in rows), Decimal("0"))),
        "penaltyTotal": money(sum((row["penaltyAmount"] for row in rows), Decimal("0"))),
        "scheduleTotal": money(sum((row["amount"] for row in rows), Decimal("0"))),
    }


def amortization_schedule_for_debt(debt):
    installments = list(debt.installments.all())
    entries = [{
        "number": item.number, "dueDate": item.due_date,
        "base": item.base_amount or money(item.amount - item.penalty_amount),
        "penalty": item.penalty_amount, "amount": item.amount, "paid": item.paid_amount,
    } for item in installments]
    capital_paid = money(max(debt.principal - debt.capital_remaining, 0)) if debt.loan_type == Debt.LoanType.SINGLE else Decimal("0")
    rows = _amortization_rows(debt.principal, debt.loan_type, entries, capital_paid)
    return {
        "organization": debt.organization, "debtId": debt.reference, "clientName": debt.client.name,
        "loanType": debt.loan_type, "principal": debt.principal, "interestRate": debt.interest_rate,
        "startDate": debt.start_date, "dueDate": debt.due_date,
        **_amortization_totals(rows),
        "collected": debt.collected, "outstanding": debt.outstanding,
        "installments": rows,
    }


def loan_installment_terms(loan_type, principal, rate, duration, due_date):
    """The instalments a new loan starts with: (number, due date, amount).

    The single source for create_debt, loan previews and simulations, so a
    preview is exactly the ledger that creating the loan produces."""
    interest = money(principal * rate / 100)
    amount = interest if loan_type == Debt.LoanType.SINGLE else money(money(principal + interest) / duration)
    return [(number, add_months(due_date, number - duration), amount) for number in range(1, duration + 1)]


def _projected_schedule(organization, data, due_date):
    principal, rate, duration, loan_type = data["principal"], data["interestRate"], data["durationMonths"], data["loanType"]
    entries = [{"number": number, "dueDate": due, "base": amount, "penalty": Decimal("0"), "amount": amount, "paid": Decimal("0")}
               for number, due, amount in loan_installment_terms(loan_type, principal, rate, duration, due_date)]
    rows = _amortization_rows(principal, loan_type, entries)
    totals = _amortization_totals(rows)
    return {
        "organization": organization, "debtId": "", "clientName": data.get("clientName", ""),
        "loanType": loan_type, "principal": principal, "interestRate": rate,
        "startDate": data["startDate"], "dueDate": due_date,
        **totals, "collected": Decimal("0"), "outstanding": totals["scheduleTotal"],
        "installments": rows,
    }


def simulate_amortization_schedule(organization, data):
    return _projected_schedule(organization, data, add_months(data["startDate"], data["durationMonths"]))


def loan_due_date(data):
    """Multi-period loans end one term after the start; single loans use the chosen date."""
    if data["loanType"] == Debt.LoanType.MULTI:
        return add_months(data["startDate"], data["durationMonths"])
    return data["dueDate"]


def preview_loan_schedule(organization, data):
    """Schedule of a corporate loan before it is created, built like create_debt builds it."""
    return _projected_schedule(organization, data, loan_due_date(data))


def save_amortization_plan(user, organization, data):
    try:
        client = Client.objects.get(pk=data["clientId"], organization=organization)
    except Client.DoesNotExist as exc:
        raise ValidationError({"clientId": "Client not found."}) from exc
    schedule = simulate_amortization_schedule(organization, {**data, "clientName": client.name})
    plan = AmortizationPlan.objects.create(
        organization=organization, client=client, created_by=user, loan_type=data["loanType"],
        principal=data["principal"], interest_rate=data["interestRate"], duration_months=data["durationMonths"],
        start_date=data["startDate"], schedule_total=schedule["scheduleTotal"])
    return plan, schedule


def amortization_schedule_for_plan(plan):
    """A loan's plan shows that loan's live ledger schedule; a simulation is recomputed from its terms."""
    if plan.debt_id:
        assess_overdue_penalties({"pk": plan.debt_id})
        return amortization_schedule_for_debt(Debt.objects.select_related("client", "organization").get(pk=plan.debt_id))
    data = {"clientName": plan.client.name, "loanType": plan.loan_type, "principal": plan.principal,
            "interestRate": plan.interest_rate, "durationMonths": plan.duration_months, "startDate": plan.start_date}
    return _projected_schedule(plan.organization, data, plan.due_date or add_months(plan.start_date, plan.duration_months))


def get_membership(user):
    """Returns the user's OrganizationMembership, or None for a personal
    account. A single small helper so callers never inline the reverse
    accessor / exception handling for the OneToOneField."""
    return OrganizationMembership.objects.filter(user=user).select_related("organization").first()


def resolve_scope(user):
    """Returns the filter kwargs that scope a Client/Debt/Payment queryset to
    the caller: {"organization": ...} for a corporate account (shared ledger
    across Owner+Staff), or {"owner": user} for a personal account -- the
    exact filter every existing view already used before this helper existed.
    Personal accounts get a queryset identical to before this refactor."""
    membership = get_membership(user)
    if membership:
        return {"organization": membership.organization}
    return {"owner": user}


def generate_share_token(client):
    """Assign a fresh, unguessable share token to a client, invalidating any
    previous link. Retries a handful of times on the astronomically unlikely
    event of a collision instead of using a locked counter (unlike sequential
    document numbers, this value has no ordering requirement)."""
    for _ in range(5):
        token = secrets.token_urlsafe(32)
        client.share_token = token
        try:
            client.save(update_fields=["share_token", "updated_at"])
            return token
        except IntegrityError:
            continue
    raise ValidationError("Could not generate a unique share link. Try again.")


def add_months(value, months):
    index = value.month - 1 + months
    year, month = value.year + index // 12, index % 12 + 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def next_reference():
    # SQLite's IMMEDIATE transaction serializes writers before this read. The
    # counter also avoids a ledger-wide scan as the number of debts grows.
    with transaction.atomic():
        sequence, _ = DebtReferenceSequence.objects.get_or_create(name="PNG")
        sequence.last_number += 1
        sequence.save(update_fields=["last_number"])
        return f"PNG-{sequence.last_number}"


@transaction.atomic
def create_debt(owner, data):
    scope = resolve_scope(owner)
    organization = scope.get("organization")
    client_id = data.get("clientId")
    if client_id:
        try:
            client = Client.objects.get(pk=client_id, **scope)
        except Client.DoesNotExist as exc:
            raise ValidationError({"clientId": "Client not found."}) from exc
    else:
        client, _ = Client.objects.get_or_create(name=data["name"], defaults={"notes": "Created with a debt record.", "owner": owner, "organization": organization}, **scope)
    principal = money(data["principal"])
    rate = Decimal(data["interestRate"])
    duration = data["durationMonths"]
    interest = money(principal * rate / 100)
    total = money(principal + interest)
    debt = Debt.objects.create(owner=owner, organization=organization, client=client, reference=next_reference(), loan_type=data["loanType"],
        principal=principal, capital_remaining=principal, interest_rate=rate, penalty_rate=data.get("penaltyRate", 0),
        duration_months=duration, total=total, outstanding=total, collected=Decimal("0"), start_date=data["startDate"], due_date=data["dueDate"])
    for number, due, amount in loan_installment_terms(debt.loan_type, principal, rate, duration, debt.due_date):
        Installment.objects.create(debt=debt, number=number, due_date=due, base_amount=amount, amount=amount)
    debt.set_status()
    debt.save(update_fields=["status", "updated_at"])
    if organization:
        # Corporate loans always carry their amortization plan; personal ledgers are unchanged.
        schedule = amortization_schedule_for_debt(debt)
        AmortizationPlan.objects.create(
            organization=organization, client=client, debt=debt, created_by=owner, loan_type=debt.loan_type,
            principal=principal, interest_rate=rate, duration_months=duration, start_date=debt.start_date,
            due_date=debt.due_date, schedule_total=schedule["scheduleTotal"])
    return debt


@transaction.atomic
def reconcile_debt(debt, *, today=None):
    """Derive balances and one-time overdue penalties from active payment facts.

    Call inside the write transaction after all structural edits are complete.
    This is also safe to call repeatedly when reading an overdue ledger.
    """
    debt.refresh_from_db()
    today = today or timezone.localdate()
    installments = list(debt.installments.order_by("number"))
    payments = list(debt.payments.filter(reversed_at__isnull=True))
    paid_by_installment = {}
    for payment in payments:
        if payment.installment_id:
            paid_by_installment[payment.installment_id] = paid_by_installment.get(payment.installment_id, Decimal("0")) + payment.amount

    for installment in installments:
        base = installment.base_amount or money(installment.amount - installment.penalty_amount)
        paid = money(paid_by_installment.get(installment.pk, Decimal("0")))
        # Once a period was paid, its assessed penalty remains part of its
        # historical amount. Open periods follow the current due date and rate.
        penalty = installment.penalty_amount if paid >= installment.amount and installment.amount > 0 else (
            money(base * debt.penalty_rate / 100) if installment.due_date < today and paid < installment.amount else Decimal("0")
        )
        amount = money(base + penalty)
        changed = []
        for field, value in (("base_amount", base), ("penalty_amount", penalty), ("amount", amount), ("paid_amount", paid)):
            if getattr(installment, field) != value:
                setattr(installment, field, value)
                changed.append(field)
        if changed:
            installment.save(update_fields=[*changed, "updated_at"])

    collected = money(sum((payment.amount for payment in payments), Decimal("0")))
    if debt.loan_type == Debt.LoanType.MULTI:
        total = money(sum((item.amount for item in installments), Decimal("0")))
        outstanding = money(sum((max(item.amount - item.paid_amount, 0) for item in installments), Decimal("0")))
        capital = debt.capital_remaining
    else:
        principal_paid = sum((payment.amount for payment in payments if payment.payment_type == Payment.PaymentType.PRINCIPAL and payment.installment_id is None), Decimal("0"))
        capital = money(max(debt.principal - principal_paid, 0))
        outstanding = money(capital + sum((max(item.amount - item.paid_amount, 0) for item in installments), Decimal("0")))
        total = outstanding
    changed = []
    for field, value in (("capital_remaining", capital), ("total", total), ("collected", collected), ("outstanding", outstanding)):
        if getattr(debt, field) != value:
            setattr(debt, field, value)
            changed.append(field)
    previous_status = debt.status
    debt.set_status()
    if debt.status != previous_status:
        changed.append("status")
    if changed:
        debt.save(update_fields=[*changed, "updated_at"])
    return debt


def assess_overdue_penalties(scope=None, *, today=None):
    """Reconcile only the debts whose day-dependent values are now stale.

    This runs on reads (bootstrap, dashboard, sync hello), so it must not touch
    every debt: only open debts that just passed their due date, or that have an
    overdue, unpaid period still waiting for its one-time penalty. All other
    balances are already reconciled on every write."""
    today = today or timezone.localdate()
    unpenalised = Installment.objects.filter(debt=OuterRef("pk"), due_date__lt=today,
                                             paid_amount__lt=F("amount"), penalty_amount=0)
    debts = Debt.objects.filter(outstanding__gt=0).filter(
        (Q(due_date__lt=today) & ~Q(status=Debt.Status.OVERDUE))
        | (Q(penalty_rate__gt=0) & Exists(unpenalised)))
    if scope:
        debts = debts.filter(**scope)
    # Materialise first: each reconcile commits, and on SQLite a commit
    # invalidates an open server-side cursor.
    for debt in list(debts):
        reconcile_debt(debt, today=today)


@transaction.atomic
def record_payment(owner, reference, data):
    scope = resolve_scope(owner)
    debt = Debt.objects.select_for_update().prefetch_related("installments").get(reference=reference, **scope)
    reconcile_debt(debt)
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
        created.append(Payment.objects.create(owner=owner, organization=debt.organization, debt=debt, client=debt.client, installment=installment,
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
                open_installment = Installment.objects.create(debt=debt, number=len(installments) + 1, due_date=debt.due_date, base_amount=interest_due, amount=interest_due)
                installments.append(open_installment)
            open_installment.paid_amount = open_installment.amount
            open_installment.save(update_fields=["paid_amount", "updated_at"])
            add_payment(interest_due, Payment.PaymentType.INTEREST, open_installment)
            next_due = add_months(open_installment.due_date, 1)
            next_interest = money(capital * debt.interest_rate / 100)
            Installment.objects.create(debt=debt, number=max(item.number for item in installments) + 1, due_date=next_due, base_amount=next_interest, amount=next_interest)
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
                open_installment.base_amount = new_interest
                open_installment.paid_amount = min(open_installment.paid_amount, new_interest)
                open_installment.save(update_fields=["amount", "base_amount", "paid_amount", "updated_at"])
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
    debt.save()
    reconcile_debt(debt)
    return debt, created


@transaction.atomic
def revert_installment(owner, reference, number):
    scope = resolve_scope(owner)
    debt = Debt.objects.select_for_update().prefetch_related("installments").get(reference=reference, **scope)
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
    debt.save()
    reconcile_debt(debt)
    return debt, payments


# ---------------------------------------------------------------------------
# Corporate accounts: organizations, staff, fiscal documents
# ---------------------------------------------------------------------------

def require_owner_role(user):
    """Raises if the caller is not the Owner of their organization. Used by
    every staff-management endpoint; Staff can read their org's ledger but
    cannot manage membership."""
    membership = get_membership(user)
    if not membership or membership.role != OrganizationMembership.Role.OWNER:
        raise ValidationError("Only the organization owner can do this.")
    return membership


@transaction.atomic
def create_organization_with_owner(user, org_data):
    """Attaches a brand-new Organization to a just-registered user as its
    Owner. Called only from the registration flow (register_view /
    mobile_register_view), never as a standalone AllowAny endpoint, so a
    stray unauthenticated request can never attach an organization to an
    arbitrary existing account."""
    organization = Organization.objects.create(
        name=org_data["name"], nuit=org_data.get("nuit", ""), address=org_data.get("address", ""),
        iva_rate=org_data.get("ivaRate", 0), logo=org_data.get("logo", ""), created_by=user,
    )
    OrganizationMembership.objects.create(user=user, organization=organization, role=OrganizationMembership.Role.OWNER)
    return organization


def create_staff_account(owner_user, name, email, password, role):
    membership = require_owner_role(owner_user)
    email = email.strip().lower()
    if len(password) < 8:
        raise ValidationError({"password": "Use at least 8 characters."})
    if User.objects.filter(username=email).exists():
        raise ValidationError({"email": "An account with this email already exists."})
    if role not in OrganizationMembership.Role.values:
        raise ValidationError({"role": "Invalid role."})
    first_name, _, last_name = name.strip().partition(" ")
    with transaction.atomic():
        user = User.objects.create_user(username=email, email=email, password=password, first_name=first_name, last_name=last_name)
        OrganizationMembership.objects.create(user=user, organization=membership.organization, role=role)
    return user


def update_staff_account(owner_user, staff_user_id, name=None, email=None, password=None, role=None):
    membership = require_owner_role(owner_user)
    staff_membership = OrganizationMembership.objects.filter(
        user_id=staff_user_id, organization=membership.organization,
    ).select_related("user").first()
    if not staff_membership:
        raise ValidationError("Staff member not found.")
    staff_user = staff_membership.user
    if name is not None:
        first_name, _, last_name = name.strip().partition(" ")
        staff_user.first_name, staff_user.last_name = first_name, last_name
    if email is not None:
        email = email.strip().lower()
        if User.objects.filter(username=email).exclude(pk=staff_user.pk).exists():
            raise ValidationError({"email": "An account with this email already exists."})
        staff_user.username = staff_user.email = email
    if password is not None:
        if len(password) < 8:
            raise ValidationError({"password": "Use at least 8 characters."})
        staff_user.set_password(password)
    staff_user.save()
    if role is not None:
        if role not in OrganizationMembership.Role.values:
            raise ValidationError({"role": "Invalid role."})
        staff_membership.role = role
        staff_membership.save(update_fields=["role", "updated_at"])
    return staff_user, staff_membership


def remove_staff_account(owner_user, staff_user_id):
    membership = require_owner_role(owner_user)
    if str(owner_user.pk) == str(staff_user_id):
        raise ValidationError("The organization owner cannot remove their own membership.")
    deleted, _ = OrganizationMembership.objects.filter(user_id=staff_user_id, organization=membership.organization).delete()
    if not deleted:
        raise ValidationError("Staff member not found.")


@transaction.atomic
def next_document_number(organization, document_type):
    """Row-locked allocation, unlike next_reference()'s scan-and-max -- each
    company's numbering is independent and gapless per document type."""
    sequence, _ = DocumentSequence.objects.select_for_update().get_or_create(organization=organization, document_type=document_type)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number", "updated_at"])
    return sequence.last_number


@transaction.atomic
def issue_document(user, document_type, client_id, debt_reference=None, payment_id=None, amount=None, description="", **extra):
    membership = get_membership(user)
    if not membership:
        raise ValidationError("Only corporate accounts can issue documents.")
    organization = membership.organization
    model = DOCUMENT_MODELS.get(document_type)
    if model is None:
        raise ValidationError({"documentType": "Unknown document type."})
    try:
        client = Client.objects.get(pk=client_id, organization=organization)
    except Client.DoesNotExist as exc:
        raise ValidationError({"clientId": "Client not found."}) from exc
    debt = None
    if debt_reference:
        debt = Debt.objects.filter(reference=debt_reference, organization=organization).first()
        if not debt:
            raise ValidationError({"debtReference": "Debt not found."})
    payment = None
    if payment_id:
        payment = Payment.objects.filter(public_id=payment_id, organization=organization).first()
        if not payment:
            raise ValidationError({"paymentId": "Payment not found."})
    number = next_document_number(organization, document_type)
    fields = dict(organization=organization, issued_by=user, client=client, debt=debt, payment=payment,
                  number=number, amount=money(amount or 0), description=description)
    fields.update(extra)
    return model.objects.create(**fields)
