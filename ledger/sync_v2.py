"""Transport-independent ledger protocol v2 service."""

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import ValidationError

from .models import (Client, Debt, Installment, MobileDevice, Payment, SyncChange,
                     SyncConflict, SyncCursorFloor, SyncMutation)
from .services import assess_overdue_penalties, money, next_reference, reconcile_debt, resolve_scope
from .sync_log import canonical_row, sync_origin, wire_fields

MODELS = {"client": Client, "debt": Debt, "installment": Installment, "payment": Payment}
INSERT_ORDER = {"client": 0, "debt": 1, "installment": 2, "payment": 3}
CLIENT_FIELDS = {"name": "name", "phone": "phone", "email": "email", "address": "address", "notes": "notes"}
DEBT_FIELDS = {"penaltyRate": "penalty_rate", "dueDate": "due_date", "clientId": "client"}
INSERT_FIELDS = {
    "client": set(CLIENT_FIELDS),
    "debt": {"clientId", "loanType", "principal", "interestRate", "penaltyRate", "durationMonths", "startDate", "dueDate"},
    "installment": {"debtId", "number", "dueDate", "baseAmount"},
    "payment": {"debtId", "installmentId", "operationId", "amount", "type", "date", "note"},
}
MONEY_FIELDS = {"principal", "interestRate", "penaltyRate", "baseAmount", "amount"}
INSERT_REQUIRED = {
    "client": {"name"},
    "debt": {"clientId", "loanType", "principal", "interestRate", "startDate", "dueDate"},
    "installment": {"debtId", "number", "baseAmount", "dueDate"},
    "payment": {"debtId", "operationId", "amount", "type", "date"},
}


class SyncApplyError(Exception):
    def __init__(self, reason, entity="", entity_id=None, server_row=None, *, rejected=False):
        self.reason = reason
        self.entity = entity
        self.entity_id = entity_id or uuid.UUID(int=0)
        self.server_row = server_row or {}
        self.rejected = rejected
        super().__init__(reason)


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError({"id": "A valid UUID is required."}) from exc


def _date(value):
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError({"date": "An ISO date is required."}) from exc


def _amount(value):
    try:
        result = money(value)
    except Exception as exc:
        raise ValidationError({"amount": "A decimal amount is required."}) from exc
    if result < 0:
        raise ValidationError({"amount": "Amount must be non-negative."})
    return result


def _queryset(entity, scope):
    model = MODELS[entity]
    if entity == "installment":
        return model.objects.filter(**{f"debt__{key}": value for key, value in scope.items()})
    return model.objects.filter(**scope)


def _get(entity, row_id, scope):
    row = _queryset(entity, scope).filter(public_id=row_id).first()
    if row is None:
        # The same reply covers a missing UUID and one belonging to another scope.
        raise SyncApplyError("not_found", entity, row_id, rejected=True)
    return row


def _scope_filter(user):
    scope = resolve_scope(user)
    return {"organization": scope["organization"]} if "organization" in scope else {"owner": user}


def bind_device(user, device_id, label="", *, app="mobile"):
    _uuid(device_id)
    if app not in {"mobile", "web"}:
        raise ValidationError({"app": "Unsupported client."})
    scope = resolve_scope(user)
    if app == "mobile" and "organization" in scope:
        raise ValidationError({"detail": "Corporate accounts are not yet supported on mobile."})
    device, created = MobileDevice.objects.get_or_create(device_id=str(device_id), defaults={"owner": user, "label": label[:160]})
    if not created and device.owner_id != user.pk:
        raise ValidationError({"deviceId": "This device is already linked to another account."})
    if label and device.label != label[:160]:
        device.label = label[:160]
        device.save(update_fields=["label", "updated_at"])
    return device


def require_device(user, device_id):
    _uuid(device_id)
    device = MobileDevice.objects.filter(device_id=str(device_id), owner=user).first()
    if not device:
        raise ValidationError({"deviceId": "Device is not linked to this account."})
    return device


def latest_cursor(user):
    return SyncChange.objects.filter(**_scope_filter(user)).order_by("-pk").values_list("pk", flat=True).first() or 0


def cursor_floor(user):
    return SyncCursorFloor.objects.filter(**_scope_filter(user)).values_list("floor", flat=True).first() or 0


def welcome(user, payload):
    if payload.get("type") != "hello" or payload.get("v") != 2:
        raise ValidationError({"type": "Expected a v2 hello message."})
    app = payload.get("app", "mobile")
    if app == "mobile" and payload.get("schemaVersion") != 4:
        raise ValidationError({"schemaVersion": "Schema version 4 is required."})
    device = bind_device(user, payload.get("deviceId"), payload.get("deviceLabel", ""), app=app)
    assess_overdue_penalties(_scope_filter(user))
    cursor = _cursor(payload.get("cursor", 0))
    return device, {"type": "welcome", "v": 2, "serverTime": timezone.now().isoformat(),
                    "latestCursor": latest_cursor(user), "resyncRequired": cursor < cursor_floor(user)}


def _cursor(value):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"cursor": "A non-negative integer cursor is required."}) from exc
    if result < 0:
        raise ValidationError({"cursor": "A non-negative integer cursor is required."})
    return result


def feed(user, cursor, limit=500):
    cursor = _cursor(cursor)
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError) as exc:
        raise ValidationError({"limit": "A positive integer is required."}) from exc
    if cursor < cursor_floor(user):
        return {"type": "resync_required", "reason": "cursor_expired"}
    rows = list(SyncChange.objects.filter(**_scope_filter(user), pk__gt=cursor).order_by("pk")[:limit + 1])
    page, has_more = rows[:limit], len(rows) > limit
    items = [{"cursor": item.pk, "entity": item.entity, "id": str(item.entity_id), "op": item.op,
              "revision": item.pk, "fields": item.fields,
              "originDeviceId": item.origin_device.device_id if item.origin_device_id else None,
              "mutationId": str(item.mutation.mutation_id) if item.mutation_id else None}
             for item in page]
    return {"type": "changes", "fromCursor": cursor,
            "toCursor": page[-1].pk if page else cursor, "hasMore": has_more, "items": items}


def bootstrap(user):
    scope = _scope_filter(user)
    assess_overdue_penalties(scope)
    rows = []
    for entity in ("client", "debt", "installment", "payment"):
        for row in _queryset(entity, scope).order_by("pk").iterator():
            rows.append(canonical_row(row))
    return {"type": "bootstrap", "v": 2, "latestCursor": latest_cursor(user), "items": rows}


def _conflicting_fields(entity, row_id, base_revision, requested, scope):
    if base_revision is None:
        raise SyncApplyError("missing_base_revision", entity, row_id)
    try:
        base_revision = _cursor(base_revision)
    except ValidationError as exc:
        raise SyncApplyError("bad_base_revision", entity, row_id, rejected=True) from exc
    changes = SyncChange.objects.filter(**scope, entity=entity, entity_id=row_id, pk__gt=base_revision).values_list("changed_fields", flat=True)
    changed = set().union(*(set(item) for item in changes))
    return sorted(changed.intersection(requested))


def _parent_revision(change, debt):
    value = change.get("debtBaseRevision", change.get("baseRevision"))
    if value is None or _cursor(value) != debt.revision:
        raise SyncApplyError("stale_base_revision", "debt", debt.public_id, canonical_row(debt))


def _same_insert(entity, row, fields):
    if not INSERT_REQUIRED[entity].issubset(fields):
        return False
    current = wire_fields(row)
    for key, value in fields.items():
        if key not in INSERT_FIELDS[entity]:
            continue
        if key in MONEY_FIELDS:
            if _amount(value) != Decimal(current[key]):
                return False
        elif key in {"name", "phone", "email", "address", "notes", "note"}:
            if str(value or "") != current[key]:
                return False
        elif str(value) != str(current[key]):
            return False
    return True


def _apply_one(user, device, change, scope, touched_debts, created_debts, touched, notices):
    entity, op = change.get("entity"), change.get("op")
    if entity not in MODELS or op not in {"insert", "update", "delete"}:
        raise SyncApplyError("bad_change", rejected=True)
    row_id = _uuid(change.get("id"))
    fields = change.get("fields" if op == "insert" else "changedFields", {})
    if op != "delete" and not isinstance(fields, dict):
        raise SyncApplyError("bad_fields", entity, row_id, rejected=True)
    row = _queryset(entity, scope).filter(public_id=row_id).first()
    if op == "insert" and row:
        if _same_insert(entity, row, fields):
            touched.append(row)
            return False
        raise SyncApplyError("already_exists", entity, row_id, canonical_row(row))
    if op != "insert" and row is None:
        raise SyncApplyError("not_found", entity, row_id, rejected=True)

    organization = scope.get("organization")
    if op == "insert":
        if entity == "client":
            if not str(fields.get("name", "")).strip():
                raise SyncApplyError("name_required", entity, row_id, rejected=True)
            row = Client.objects.create(public_id=row_id, owner=user, organization=organization,
                                        **{target: str(fields.get(key) or "") for key, target in CLIENT_FIELDS.items()})
        elif entity == "debt":
            client = _get("client", _uuid(fields.get("clientId")), scope)
            principal = _amount(fields.get("principal"))
            rate = _amount(fields.get("interestRate", "0"))
            interest = money(principal * rate / 100)
            loan_type = fields.get("loanType")
            start_date, due_date = _date(fields.get("startDate")), _date(fields.get("dueDate"))
            if principal <= 0 or loan_type not in Debt.LoanType.values:
                raise SyncApplyError("invalid_debt", entity, row_id, rejected=True)
            if due_date < start_date:
                raise SyncApplyError("invalid_due_date", entity, row_id, rejected=True)
            row = Debt.objects.create(public_id=row_id, owner=user, organization=organization, client=client,
                                      reference=next_reference(), loan_type=loan_type, principal=principal,
                                      capital_remaining=principal, interest_rate=rate,
                                      penalty_rate=Decimal("0") if loan_type == Debt.LoanType.SINGLE else _amount(fields.get("penaltyRate", "0")),
                                      duration_months=max(1, int(fields.get("durationMonths", 1))),
                                      start_date=start_date, due_date=due_date,
                                      total=principal + interest, outstanding=principal + interest)
            touched_debts.add(row.pk)
            created_debts.add(row.pk)
        elif entity == "installment":
            debt = _get("debt", _uuid(fields.get("debtId")), scope)
            if debt.pk not in created_debts:
                _parent_revision(change, debt)
            base = _amount(fields.get("baseAmount"))
            row = Installment.objects.create(public_id=row_id, debt=debt, number=int(fields.get("number")),
                                             due_date=_date(fields.get("dueDate")), base_amount=base, amount=base)
            touched_debts.add(debt.pk)
        else:
            debt = _get("debt", _uuid(fields.get("debtId")), scope)
            installment = _get("installment", _uuid(fields["installmentId"]), scope) if fields.get("installmentId") else None
            if installment and installment.debt_id != debt.pk:
                raise SyncApplyError("invalid_relationship", entity, row_id, rejected=True)
            amount = _amount(fields.get("amount"))
            if amount <= 0 or fields.get("type") not in Payment.PaymentType.values:
                raise SyncApplyError("invalid_payment", entity, row_id, rejected=True)
            row = Payment.objects.create(public_id=row_id, owner=user, organization=organization, client=debt.client,
                                         debt=debt, installment=installment, operation_id=_uuid(fields.get("operationId")),
                                         amount=amount, payment_type=fields["type"], payment_date=_date(fields.get("date")),
                                         note=str(fields.get("note") or ""))
            touched_debts.add(debt.pk)
    elif op == "update":
        if entity == "client":
            if not set(fields).issubset(CLIENT_FIELDS):
                raise SyncApplyError("read_only_field", entity, row_id, rejected=True)
            for key in _conflicting_fields(entity, row_id, change.get("baseRevision"), fields, scope):
                notices.append({"entity": entity, "id": str(row_id), "reason": "field_overwritten", "field": key})
            for key, value in fields.items():
                setattr(row, CLIENT_FIELDS[key], str(value or ""))
            row.save(update_fields=[*(CLIENT_FIELDS[key] for key in fields), "updated_at"])
        elif entity == "debt":
            structural = set(fields) - set(DEBT_FIELDS) - {"capitalRemaining"}
            if structural:
                raise SyncApplyError("read_only_field", entity, row_id, rejected=True)
            if "capitalRemaining" in fields:
                _parent_revision(change, row)
            for key in _conflicting_fields(entity, row_id, change.get("baseRevision"), set(fields) & set(DEBT_FIELDS), scope):
                notices.append({"entity": entity, "id": str(row_id), "reason": "field_overwritten", "field": key})
            update_fields = []
            for key, value in fields.items():
                if key == "penaltyRate":
                    row.penalty_rate = _amount(value)
                    update_fields.append("penalty_rate")
                elif key == "dueDate":
                    row.due_date = _date(value)
                    update_fields.append("due_date")
                elif key == "clientId":
                    row.client = _get("client", _uuid(value), scope)
                    update_fields.append("client")
            if update_fields:
                row.save(update_fields=[*update_fields, "updated_at"])
                if "clientId" in fields:
                    for payment in row.payments.all():
                        payment.client = row.client
                        payment.save(update_fields=["client", "updated_at"])
            touched_debts.add(row.pk)
        elif entity == "installment":
            _parent_revision(change, row.debt)
            if not set(fields).issubset({"number", "dueDate", "baseAmount"}):
                raise SyncApplyError("read_only_field", entity, row_id, rejected=True)
            if "number" in fields:
                row.number = int(fields["number"])
            if "dueDate" in fields:
                row.due_date = _date(fields["dueDate"])
            if "baseAmount" in fields:
                row.base_amount = _amount(fields["baseAmount"])
                row.amount = row.base_amount
            row.save()
            touched_debts.add(row.debt_id)
        else:
            if not set(fields).issubset({"note", "reversedAt"}):
                raise SyncApplyError("read_only_field", entity, row_id, rejected=True)
            if "note" in fields:
                row.note = str(fields["note"] or "")
            if fields.get("reversedAt") and row.reversed_at is None:
                parsed = parse_datetime(str(fields["reversedAt"]))
                if parsed is None or timezone.is_naive(parsed):
                    raise SyncApplyError("bad_reversal_time", entity, row_id, rejected=True)
                row.reversed_at = parsed
            row.save()
            touched_debts.add(row.debt_id)
    else:
        if entity == "client" and (row.debts.exists() or row.payments.exists()):
            raise SyncApplyError("has_dependents", entity, row_id, canonical_row(row))
        if entity in {"debt", "installment"}:
            _parent_revision(change, row if entity == "debt" else row.debt)
        if entity == "payment":
            if row.reversed_at is None:
                row.reversed_at = timezone.now()
                row.save(update_fields=["reversed_at", "updated_at"])
            touched_debts.add(row.debt_id)
        else:
            if entity == "installment":
                touched_debts.add(row.debt_id)
            try:
                row.delete()
            except ProtectedError as exc:
                raise SyncApplyError("has_dependents", entity, row_id, canonical_row(row)) from exc
            return True
    touched.append(row)
    return True


@transaction.atomic
def apply_mutation(user, device, payload):
    if device.owner_id != user.pk:
        return {"type": "push_result", "status": "rejected", "error": "device_conflict"}
    mutation_id = _uuid(payload.get("mutationId"))
    if payload.get("type") != "push" or not isinstance(payload.get("changes"), list) or not payload["changes"]:
        raise ValidationError({"type": "A push with changes is required."})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    prior = SyncMutation.objects.filter(device=device, mutation_id=mutation_id).first()
    if prior:
        if prior.payload_hash != digest:
            return {"type": "push_result", "mutationId": str(mutation_id), "status": "rejected",
                    "cursor": latest_cursor(user), "rows": [], "conflicts": [], "error": "mutation_id_reused"}
        return {**prior.result, "status": "duplicate"}

    record = SyncMutation.objects.create(device=device, mutation_id=mutation_id, payload_hash=digest,
                                         action=str(payload.get("action") or "")[:32], status="applied")
    scope = _scope_filter(user)
    touched, touched_debts, created_debts, notices, overpaid = [], set(), set(), [], []
    changes = payload["changes"]
    if len(changes) > 100:
        raise ValidationError({"changes": "At most 100 row changes are allowed."})
    if any(not isinstance(item, dict) for item in changes):
        raise ValidationError({"changes": "Every change must be an object."})
    ordered = sorted(changes, key=lambda item: (item.get("op") == "delete",
                         INSERT_ORDER.get(item.get("entity"), 99) if item.get("op") != "delete"
                         else -INSERT_ORDER.get(item.get("entity"), 99)))
    try:
        with transaction.atomic():
            with sync_origin(device, record):
                applied_any = False
                for change in ordered:
                    applied_any = _apply_one(user, device, change, scope, touched_debts, created_debts, touched, notices) or applied_any
                for debt_id in created_debts:
                    if not Installment.objects.filter(debt_id=debt_id).exists():
                        debt = Debt.objects.get(pk=debt_id)
                        raise SyncApplyError("missing_installments", "debt", debt.public_id, rejected=True)
                for debt_id in touched_debts:
                    debt = Debt.objects.filter(pk=debt_id, **scope).first()
                    if debt:
                        reconcile_debt(debt)
                        if (any(item.paid_amount > item.amount for item in debt.installments.all())
                                or (debt.loan_type == Debt.LoanType.SINGLE and
                                    sum((p.amount for p in debt.payments.filter(reversed_at__isnull=True,
                                        payment_type=Payment.PaymentType.PRINCIPAL, installment__isnull=True)), Decimal("0")) > debt.principal)):
                            overpaid.append(str(debt.public_id))
                        touched.append(debt)
                        touched.extend(debt.installments.all())
            rows = list({(type(row), row.pk): canonical_row(row) for row in touched if row.pk}.values())
            result = {"type": "push_result", "mutationId": str(mutation_id),
                      "status": "applied" if applied_any else "duplicate",
                      "cursor": latest_cursor(user), "rows": rows, "conflicts": notices,
                      "overpaid": overpaid, "error": None}
    except (SyncApplyError, IntegrityError, ValidationError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, SyncApplyError):
            reason, entity, entity_id, server_row, rejected = exc.reason, exc.entity, exc.entity_id, exc.server_row, exc.rejected
        else:
            reason, entity, entity_id, server_row, rejected = "invalid_change", "", uuid.UUID(int=0), {}, True
        status = "rejected" if rejected else "conflict"
        details = {"entity": entity, "id": str(entity_id), "reason": reason}
        SyncConflict.objects.create(mutation=record, entity=entity, entity_id=entity_id,
                                    reason=reason, client_payload=payload, server_row=server_row)
        result = {"type": "push_result", "mutationId": str(mutation_id), "status": status,
                  "cursor": latest_cursor(user), "rows": [server_row] if server_row else [],
                  "conflicts": [details], "error": reason if rejected else None}
    record.status = result["status"]
    record.result = result
    record.save(update_fields=["status", "result", "updated_at"])
    return result
