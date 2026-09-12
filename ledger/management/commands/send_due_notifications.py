from django.core.management.base import BaseCommand

from ledger.push import send_due_notifications


class Command(BaseCommand):
    help = "Send due-tomorrow and overdue web push notifications. Run this command hourly."

    def handle(self, *args, **options):
        result = send_due_notifications()
        self.stdout.write(self.style.SUCCESS(
            "Push notifications: " + ", ".join(f"{key}={value}" for key, value in result.items())
        ))
