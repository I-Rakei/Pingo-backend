import time

from django.core.management.base import BaseCommand

from ledger.push import send_due_notifications


class Command(BaseCommand):
    help = "Run the Pingo due-notification worker continuously."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=3600, help="Seconds between checks (minimum 60).")

    def handle(self, *args, **options):
        interval = max(60, options["interval"])
        self.stdout.write(f"Pingo notification worker running every {interval} seconds.")
        while True:
            result = send_due_notifications()
            self.stdout.write("Push notifications: " + ", ".join(f"{key}={value}" for key, value in result.items()))
            self.stdout.flush()
            time.sleep(interval)
