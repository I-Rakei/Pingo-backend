import time

from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.utils import timezone

from ledger.push import send_due_notifications

# Generated PDFs are single-use downloads; sweep what is left every 10 minutes.
PDF_CLEANUP_INTERVAL_SECONDS = 600


class Command(BaseCommand):
    help = ("Deliver client lifecycle emails and scheduled email/web push reminders continuously, "
            "and delete stored PDFs every 10 minutes.")

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=60, help="Seconds between checks (minimum 60).")

    def handle(self, *args, **options):
        interval = max(60, options["interval"])
        self.stdout.write(f"Pingo notification worker running every {interval} seconds.")
        last_compaction = None
        last_pdf_cleanup = None
        while True:
            today = timezone.localdate()
            if today != last_compaction:
                call_command("compact_sync_log", days=90)
                last_compaction = today
            if last_pdf_cleanup is None or time.monotonic() - last_pdf_cleanup >= PDF_CLEANUP_INTERVAL_SECONDS:
                try:
                    call_command("cleanup_generated_pdfs", stdout=self.stdout)
                except Exception as error:  # a full disk or odd permissions must not stop reminders
                    self.stderr.write(f"PDF cleanup failed: {error}")
                last_pdf_cleanup = time.monotonic()
            result = send_due_notifications()
            self.stdout.write("Notifications: " + ", ".join(f"{key}={value}" for key, value in result.items()))
            self.stdout.flush()
            time.sleep(interval)
