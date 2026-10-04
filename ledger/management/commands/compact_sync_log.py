from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from ledger.models import SyncChange, SyncCursorFloor


class Command(BaseCommand):
    help = "Remove old change rows while retaining each entity's latest state."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)

    def handle(self, *args, **options):
        if options["days"] < 1:
            raise ValueError("--days must be positive")
        cutoff = timezone.now() - timedelta(days=options["days"])
        deleted_total = 0
        scopes = SyncChange.objects.values("owner_id", "organization_id").distinct()
        for scope in scopes:
            owner_id, organization_id = scope["owner_id"], scope["organization_id"]
            with transaction.atomic():
                scoped = SyncChange.objects.filter(owner_id=owner_id, organization_id=organization_id)
                latest = set(scoped.values("entity", "entity_id").annotate(last=Max("id")).values_list("last", flat=True))
                outdated = scoped.filter(created_at__lt=cutoff).exclude(pk__in=latest)
                floor = outdated.aggregate(value=Max("id"))["value"]
                if floor is None:
                    continue
                count, _ = outdated.delete()
                deleted_total += count
                state, _ = SyncCursorFloor.objects.get_or_create(owner_id=owner_id, organization_id=organization_id)
                if floor > state.floor:
                    state.floor = floor
                    state.save(update_fields=["floor"])
        self.stdout.write(f"Compacted {deleted_total} change rows.")
