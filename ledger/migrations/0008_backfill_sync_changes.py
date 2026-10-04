from datetime import timezone

from django.db import migrations


def backfill(apps, schema_editor):
    alias = schema_editor.connection.alias
    Client = apps.get_model('ledger', 'Client')
    Debt = apps.get_model('ledger', 'Debt')
    Installment = apps.get_model('ledger', 'Installment')
    Payment = apps.get_model('ledger', 'Payment')
    SyncChange = apps.get_model('ledger', 'SyncChange')

    def money(value):
        return f'{value:.2f}'

    def timestamp(value):
        return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') if value else None

    def append(model, row, entity, fields, owner_id, organization_id):
        change = SyncChange.objects.using(alias).create(
            owner_id=None if organization_id else owner_id, organization_id=organization_id,
            entity=entity, entity_id=row.public_id, op='upsert', fields=fields,
            changed_fields=list(fields),
        )
        model.objects.using(alias).filter(pk=row.pk).update(revision=change.pk)

    for row in Client.objects.using(alias).iterator():
        fields = {'name': row.name, 'phone': row.phone, 'email': row.email,
                  'address': row.address, 'notes': row.notes}
        append(Client, row, 'client', fields, row.owner_id, row.organization_id)
    for row in Debt.objects.using(alias).select_related('client').iterator():
        fields = {'clientId': str(row.client.public_id), 'reference': row.reference,
                  'loanType': row.loan_type, 'principal': money(row.principal),
                  'interestRate': money(row.interest_rate), 'penaltyRate': money(row.penalty_rate),
                  'durationMonths': row.duration_months, 'startDate': row.start_date.isoformat(),
                  'dueDate': row.due_date.isoformat(), 'capitalRemaining': money(row.capital_remaining),
                  'total': money(row.total), 'collected': money(row.collected),
                  'outstanding': money(row.outstanding), 'status': row.status}
        append(Debt, row, 'debt', fields, row.owner_id, row.organization_id)
    for row in Installment.objects.using(alias).select_related('debt').iterator():
        fields = {'debtId': str(row.debt.public_id), 'number': row.number,
                  'dueDate': row.due_date.isoformat(), 'baseAmount': money(row.base_amount),
                  'penaltyAmount': money(row.penalty_amount), 'amount': money(row.amount),
                  'paidAmount': money(row.paid_amount)}
        append(Installment, row, 'installment', fields, row.debt.owner_id, row.debt.organization_id)
    for row in Payment.objects.using(alias).select_related('debt', 'installment').iterator():
        fields = {'debtId': str(row.debt.public_id),
                  'installmentId': str(row.installment.public_id) if row.installment_id else None,
                  'operationId': str(row.operation_id), 'amount': money(row.amount),
                  'type': row.payment_type, 'date': row.payment_date.isoformat(),
                  'note': row.note, 'reversedAt': timestamp(row.reversed_at)}
        append(Payment, row, 'payment', fields, row.owner_id, row.organization_id)


class Migration(migrations.Migration):
    dependencies = [('ledger', '0007_sync_v2_models')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
