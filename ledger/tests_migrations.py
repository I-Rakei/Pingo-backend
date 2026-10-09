from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class SyncBackfillMigrationTests(TransactionTestCase):
    """Upgrade an existing ledger through the additive v2 backfill."""

    def test_existing_rows_receive_revisions_and_ordered_change_rows(self):
        from_state = [('ledger', '0007_sync_v2_models')]
        to_state = [('ledger', '0008_backfill_sync_changes')]
        executor = MigrationExecutor(connection)
        executor.migrate(from_state)
        try:
            old = executor.loader.project_state(from_state).apps
            user = User.objects.create_user(username='upgrade@example.com', password='secret-pass')
            client = old.get_model('ledger', 'Client').objects.create(owner_id=user.pk, name='Existing')
            debt = old.get_model('ledger', 'Debt').objects.create(
                owner_id=user.pk, client_id=client.pk, reference='PNG-1001', loan_type='multi',
                principal=Decimal('100.00'), capital_remaining=Decimal('100.00'),
                interest_rate=Decimal('10.00'), penalty_rate=Decimal('0.00'), duration_months=1,
                total=Decimal('110.00'), outstanding=Decimal('90.00'), collected=Decimal('20.00'),
                start_date=date(2099, 1, 1), due_date=date(2099, 2, 1))
            installment = old.get_model('ledger', 'Installment').objects.create(
                debt_id=debt.pk, number=1, due_date=date(2099, 2, 1),
                base_amount=Decimal('110.00'), amount=Decimal('110.00'), paid_amount=Decimal('20.00'))
            payment = old.get_model('ledger', 'Payment').objects.create(
                owner_id=user.pk, client_id=client.pk, debt_id=debt.pk, installment_id=installment.pk,
                amount=Decimal('20.00'), payment_type='principal', payment_date=date(2099, 1, 15))
            executor.loader.build_graph()
            executor.migrate(to_state)
            new = executor.loader.project_state(to_state).apps
            changes = list(new.get_model('ledger', 'SyncChange').objects.order_by('pk'))
            self.assertEqual([row.entity for row in changes], ['client', 'debt', 'installment', 'payment'])
            for model_name, row in [('Client', client), ('Debt', debt), ('Installment', installment), ('Payment', payment)]:
                self.assertGreater(new.get_model('ledger', model_name).objects.get(pk=row.pk).revision, 0)
            self.assertEqual(changes[1].fields['outstanding'], '90.00')
            self.assertEqual(changes[3].fields['installmentId'], str(installment.public_id))
        finally:
            # Restore the latest schema, not just 0008, so later tests see every migration.
            executor.loader.build_graph()
            executor.migrate(executor.loader.graph.leaf_nodes())
