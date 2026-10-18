"""Invoices, receipts, fiscal notes and amortization schedules as PDF bytes.

Every figure comes from the ledger rows themselves (never from the browser), scoped to the caller the same
way the API is, so a document can never disagree with what the system shows. The web app only asks for a
document and downloads the file.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from . import pdf_text
from .models import BalanceNote, CreditNote, DebitNote, Debt, Invoice, Preference
from .pdf_render import BRAND, CONTENT_WIDTH, INK, MARGIN, MUTED, RIGHT, STATUS, PdfPage
from .pdf_store import DocumentNotFound  # noqa: F401  (re-exported for callers of this module)
from .services import (
    amortization_schedule_for_debt, amortization_schedule_for_plan, assess_overdue_penalties,
    get_membership, money, resolve_scope, simulate_amortization_schedule,
)

FISCAL_MODELS = {"invoice": Invoice, "debit_note": DebitNote, "credit_note": CreditNote, "balance_note": BalanceNote}
FISCAL_PREFIX = {"invoice": "FT", "debit_note": "ND", "credit_note": "NC", "balance_note": "NS"}


@dataclass
class GeneratedPdf:
    filename: str
    data: bytes


class _Context:
    """Who is asking, in which language and currency, and the formatters that follow from that."""

    def __init__(self, user, language):
        preference = Preference.objects.filter(owner=user).first()
        self.user = user
        self.membership = get_membership(user)
        self.scope = resolve_scope(user)
        self.language = pdf_text.normalize_language(language or (preference.language if preference else "en"))
        self.currency = (preference.currency if preference and preference.currency else "MZN")
        self.copy = pdf_text.get_copy(self.language)
        self.today = timezone.localdate()
        if self.membership:
            organization = self.membership.organization
            self.issuer = {"name": organization.name, "nuit": organization.nuit, "address": organization.address}
        else:
            self.issuer = {"name": user.get_full_name() or user.email or user.username, "email": user.email}

    def money(self, value, currency=None):
        return pdf_text.format_money(value, currency or self.currency, self.language)

    def number(self, value):
        return pdf_text.format_number(value, self.language)

    def day(self, value):
        return pdf_text.format_date(value, self.language)

    def page(self, title, number, meta, footer_note=None):
        return PdfPage(
            title=title, number=number, meta=meta, issuer=self.issuer, copy=self.copy,
            money=lambda value, currency: self.money(value, currency), day=self.day,
            footer_note=footer_note or self.copy["footerNote"], generated=self.day(self.today),
            compress=getattr(settings, "PINGO_PDF_COMPRESS", True),
        )


def _plain(value):
    """30.00 -> '30', 7.50 -> '7.5'."""
    text = f"{Decimal(value).normalize():f}"
    return text


def amounts_due(debt, copy):
    """What is owed on a debt right now: each open item with its due date, split into capital and interest.

    Paid periods and repaid capital are left out, matching the client's share link."""
    multi = debt.loan_type == Debt.LoanType.MULTI
    # Multi-period instalments mix capital and interest; split each open amount in the loan's own ratio.
    capital_share = 1 / (1 + Decimal(debt.interest_rate) / 100)
    items = []
    capital = Decimal("0")
    for period in debt.installments.order_by("number"):
        remaining = money(period.amount - period.paid_amount)
        if remaining <= 0:
            continue
        if multi:
            capital += remaining * capital_share
        description = copy["instalment"](period.number) if multi else copy["interestPeriod"](period.number)
        items.append({"description": description, "dueDate": period.due_date, "amount": remaining})
    if not multi:
        capital = money(max(debt.capital_remaining, Decimal("0")))
        if capital > 0:
            items.append({"description": copy["capital"], "dueDate": debt.due_date, "amount": capital})
    items.sort(key=lambda item: item["dueDate"])
    total = money(sum((item["amount"] for item in items), Decimal("0")))
    capital = min(money(capital), total)
    return {"items": items, "capital": capital, "interest": money(total - capital), "total": total}


def _payment_description(payment, debt, copy):
    if payment.payment_type == "interest":
        return copy["interestPeriod"](payment.installment.number) if payment.installment_id else copy["interest"]
    if debt.loan_type == Debt.LoanType.SINGLE:
        return copy["capitalRepayment"]
    return copy["instalmentPeriod"](payment.installment.number) if payment.installment_id else copy["instalmentPayment"]


def _owed_section(page, ctx, owed, title):
    copy = ctx.copy
    page.section(title)
    rows = []
    for item in owed["items"]:
        overdue = item["dueDate"] < ctx.today
        rows.append([item["description"], ctx.day(item["dueDate"]),
                     (copy["overdue"] if overdue else copy["due"], STATUS["overdue"] if overdue else STATUS["unpaid"]),
                     ctx.money(item["amount"])])
    page.table([copy["description"], copy["dueDate"], copy["status"], copy["amount"]], rows,
               widths=[72, 32, 32, 38], aligns=["left", "left", "right", "right"])
    page.totals([(copy["capitalDue"], owed["capital"]), (copy["interestDue"], owed["interest"])],
                copy["totalDue"], owed["total"], ctx.currency)


def debt_document(user, reference, *, kind=None, payment_id=None, installment_number=None, language=None):
    """Invoice (what is owed now) or receipt (the payment(s) received, then anything still owed) for one debt.

    payment_id: a receipt for that one payment. installment_number: a receipt for one period.
    With neither, a receipt covers every active payment on the debt."""
    ctx = _Context(user, language)
    copy = ctx.copy
    debt = Debt.objects.filter(reference=reference, **ctx.scope).select_related("client").first()
    if debt is None:
        raise DocumentNotFound("Debt not found.")
    assess_overdue_penalties({"pk": debt.pk})
    debt.refresh_from_db()

    owed = amounts_due(debt, copy)
    status_key = "paid" if owed["total"] <= 0 else (debt.current_status if debt.current_status in STATUS else "unpaid")
    if kind not in ("invoice", "receipt"):
        kind = "receipt" if debt.status == Debt.Status.PAID else "invoice"
    is_receipt = kind == "receipt"

    payments = list(debt.payments.filter(reversed_at__isnull=True).select_related("installment").order_by("payment_date", "pk"))
    short_id = reference.removeprefix("PNG-")
    receipt = None  # (date, description, amount, reference)
    if is_receipt and payment_id:
        payment = next((item for item in payments if str(item.public_id) == str(payment_id)), None)
        if payment is None:
            raise DocumentNotFound("Payment not found.")
        if payment.installment_id:
            description = copy["periodPayment"](payment.installment.number)
        elif payment.payment_type == "interest":
            description = copy["interestPayment"]
        elif payment.payment_type == "principal":
            description = copy["capitalPayment"]
        else:
            description = copy["payment"]
        receipt = (payment.payment_date, description, payment.amount, str(payment.public_id)[:6].upper())
    elif is_receipt and installment_number:
        period = debt.installments.filter(number=installment_number).first()
        if period is None:
            raise DocumentNotFound("Period not found.")
        paid_on = max((item.payment_date for item in payments if item.installment_id == period.pk), default=period.due_date)
        receipt = (paid_on, copy["periodPayment"](period.number), period.paid_amount, f"P{period.number}")

    reference_suffix = f"-{receipt[3]}" if receipt else ""
    number = f"RCT-{short_id}{reference_suffix}" if is_receipt else f"INV-{short_id}"
    next_due = owed["items"][0]["dueDate"] if owed["items"] else None
    if is_receipt:
        last_payment = payments[-1].payment_date if payments else None
        second = (copy["paymentDate"], ctx.day(receipt[0] if receipt else last_payment))
    else:
        second = (copy["payBy"], ctx.day(next_due))
    page = ctx.page(copy["receipt"] if is_receipt else copy["invoice"], number,
                    [(copy["issueDate"], ctx.day(ctx.today)), second, (copy["loanReference"], reference)])

    client = debt.client
    page.parties(
        copy["receivedFrom"] if is_receipt else copy["billedTo"],
        {"name": client.name, "phone": client.phone, "email": client.email, "address": client.address},
        copy["loanDetails"],
        [
            (copy["loanType"], copy["single"] if debt.loan_type == Debt.LoanType.SINGLE else copy["multi"](debt.duration_months)),
            (copy["interestRate"], f"{_plain(debt.interest_rate)}%" + (f" {copy['perPeriod']}" if debt.loan_type == Debt.LoanType.SINGLE else "")),
            (copy["status"], copy[status_key], STATUS[status_key]),
        ],
    )

    if not is_receipt:
        if owed["items"]:
            _owed_section(page, ctx, owed, copy["amountsDue"])
            page.paragraph(copy["paymentInstructions"](ctx.day(next_due)), MARGIN, page.y, CONTENT_WIDTH, 8.5, MUTED)
        else:
            page.text(MARGIN, page.y + 4, copy["nothingDue"], 10, False, STATUS["paid"])
        return GeneratedPdf(f"pingo-invoice-{reference}.pdf", page.finish())

    # Receipt: the amount received leads.
    if receipt:
        received = [(receipt[0], receipt[1], receipt[2])]
    else:
        received = [(item.payment_date, _payment_description(item, debt, copy), item.amount) for item in payments]
    received_total = money(sum((item[2] for item in received), Decimal("0")))
    y = page.y
    page.box(MARGIN, y, CONTENT_WIDTH, 24)
    page.label(copy["amountReceived"], MARGIN + 6, y + 8)
    page.text(MARGIN + 6, y + 17.5, ctx.money(received_total), 20, True, BRAND)
    page.label(copy["stillOwed"], RIGHT - 6, y + 8, "right")
    page.text(RIGHT - 6, y + 17, ctx.money(owed["total"]), 13, True, INK if owed["total"] > 0 else STATUS["paid"], "right")
    page.y = y + 32

    page.section(copy["paymentReceived"] if len(received) == 1 else copy["paymentsReceived"])
    page.table([copy["date"], copy["description"], copy["amount"]],
               [[ctx.day(when), description, ctx.money(amount)] for when, description, amount in received],
               widths=[32, 104, 38], aligns=["left", "left", "right"])
    if owed["items"]:
        _owed_section(page, ctx, owed, copy["stillOwed"])
    else:
        page.text(MARGIN, page.y, copy["settled"], 10, False, STATUS["paid"])
    return GeneratedPdf(f"pingo-receipt-{reference}{reference_suffix}.pdf", page.finish())


def fiscal_document(user, document_type, number, *, language=None):
    """PDF for an issued fiscal document (invoice, debit, credit or balance note)."""
    ctx = _Context(user, language)
    copy = ctx.copy
    model = FISCAL_MODELS.get(document_type)
    if ctx.membership is None or model is None:
        raise DocumentNotFound("Document not found.")
    record = (model.objects.filter(organization=ctx.membership.organization, number=number)
              .select_related("client", "debt", "issued_by").first())
    if record is None:
        raise DocumentNotFound("Document not found.")

    title = copy["fiscalTitles"].get(document_type, copy["document"])
    label = f"{FISCAL_PREFIX.get(document_type, 'DOC')} {record.number:04d}"
    meta = [(copy["issueDate"], ctx.day(record.issue_date))]
    if record.debt_id:
        meta.append((copy["loanReference"], record.debt.reference))
    page = ctx.page(title, label, meta)

    client = record.client
    issued_by = record.issued_by.get_full_name() or record.issued_by.email
    page.parties(
        copy["creditedTo"] if document_type == "credit_note" else copy["billedTo"],
        {"name": client.name, "phone": client.phone, "email": client.email, "address": client.address},
        copy["documentDetails"],
        [(copy["document"], title), (copy["issuedBy"], issued_by or "-"), (copy["issueDate"], ctx.day(record.issue_date))],
    )

    is_balance = document_type == "balance_note"
    description = record.description or (copy["balanceStatement"] if is_balance else title)
    page.section(copy["statement"] if is_balance else copy["details"])
    page.table([copy["description"], copy["amount"]], [[description, ctx.money(record.amount)]], widths=[129, 45], aligns=["left", "right"])

    reason = getattr(record, "reason", "")
    if document_type == "credit_note" and reason:
        page.section(copy["reason"])
        lines = page.wrap(reason, CONTENT_WIDTH, 9)
        page.paragraph(reason, MARGIN, page.y + 3, CONTENT_WIDTH, 9, INK)
        page.y += len(lines) * 4.2 + 8

    page.totals([], copy["balanceOutstanding"] if is_balance else copy["total"], record.amount, ctx.currency)
    return GeneratedPdf(f"pingo-{document_type.replace('_', '-')}-{record.number}.pdf", page.finish())


def schedule_document(user, source, *, reference=None, plan=None, terms=None, language=None):
    """Amortization schedule: a recorded loan's live schedule ('recorded'), a saved plan ('plan'), or terms ('simulation')."""
    ctx = _Context(user, language)
    copy = ctx.copy
    if ctx.membership is None:
        raise DocumentNotFound("Only corporate accounts can use amortization schedules.")
    organization = ctx.membership.organization
    if source == "recorded":
        debt = Debt.objects.filter(reference=reference, organization=organization).select_related("client", "organization").first()
        if debt is None:
            raise DocumentNotFound("Debt not found.")
        assess_overdue_penalties({"pk": debt.pk})
        debt.refresh_from_db()
        schedule = amortization_schedule_for_debt(debt)
    elif source == "plan":
        if plan is None or plan.organization_id != organization.pk:
            raise DocumentNotFound("Plan not found.")
        schedule = amortization_schedule_for_plan(plan)
    else:
        schedule = simulate_amortization_schedule(organization, terms)

    is_simulation = source != "recorded"
    rows = schedule["installments"]
    page = ctx.page(
        copy["amortization"], copy["simulation"] if is_simulation else schedule["debtId"],
        [(copy["prepared"], ctx.day(ctx.today)), (copy["startDate"], ctx.day(schedule["startDate"])), (copy["finalDue"], ctx.day(schedule["dueDate"]))],
        footer_note=copy["simulationFooter"] if is_simulation else copy["scheduleFooter"],
    )
    single = schedule["loanType"] == Debt.LoanType.SINGLE
    page.parties(
        copy["preparedFor"] if is_simulation else copy["client"],
        {"name": schedule.get("clientName") or copy["prospectiveClient"]},
        copy["creditTerms"],
        [
            (copy["principal"], ctx.money(schedule["principal"])),
            (copy["interest"], f"{_plain(schedule['interestRate'])}% {copy['perPeriod'] if single else copy['flatTerm']}"),
            (copy["repayment"], copy["interestOnly"] if single else copy["equalPeriods"]),
        ],
    )
    page.section(copy["paymentPlan"](ctx.currency))
    page.table(
        ["#", copy["dueDate"], copy["principal"], copy["interest"], copy["penalty"], copy["payment"], copy["capitalLeft"]],
        [[copy["capital"] if row["rowType"] == "capital" else str(row["number"]), ctx.day(row["dueDate"]),
          ctx.number(row["principalAmount"]), ctx.number(row["interestAmount"]), ctx.number(row["penaltyAmount"]),
          ctx.number(row["amount"]), ctx.number(row["capitalBalance"])] for row in rows],
        widths=[13, 23, 27.6, 27.6, 27.6, 27.6, 27.6], aligns=["left", "left"] + ["right"] * 5,
        size=7, head_size=6.8, pad_y=2.2, pad_x=1.6,
    )
    page.totals(
        [(copy["principal"], schedule["principalTotal"]), (copy["interest"], schedule["interestTotal"]), (copy["penalties"], schedule["penaltyTotal"])],
        copy["scheduledTotal"], schedule["scheduleTotal"], ctx.currency,
    )
    page.ensure_room(48 - 24)  # keep the notes with the totals when the page is nearly full
    note = copy["projectionNote"] if is_simulation else copy["ledgerNote"](ctx.money(schedule["collected"]), ctx.money(schedule["outstanding"]))
    below_note = page.paragraph(note, MARGIN, page.y + 2, CONTENT_WIDTH, 8, MUTED)
    page.paragraph(copy["scheduleExplanation"], MARGIN, below_note + 1, CONTENT_WIDTH, 8, MUTED)
    return GeneratedPdf(f"pingo-amortization-{'simulation' if is_simulation else schedule['debtId']}.pdf", page.finish())
