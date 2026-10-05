import time

from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.utils import timezone

from ledger.push import send_due_notifications


class Command(BaseCommand):
    help = "Deliver client lifecycle emails and scheduled email/web push reminders continuously."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=60, help="Seconds between checks (minimum 60).")

    def handle(self, *args, **options):
        interval = max(60, options["interval"])
        self.stdout.write(f"Pingo notification worker running every {interval} seconds.")
        last_compaction = None
        while True:
            today = timezone.localdate()
            if today != last_compaction:
                call_command("compact_sync_log", days=90)
                last_compaction = today
            result = send_due_notifications()
            self.stdout.write("Notifications: " + ", ".join(f"{key}={value}" for key, value in result.items()))
            self.stdout.flush()
            time.sleep(interval)
