from django.core.management.base import BaseCommand

from ledger.push import send_due_notifications


class Command(BaseCommand):
    help = "Deliver pending client emails and due-tomorrow/overdue reminders. Run at least every minute."

    def handle(self, *args, **options):
        result = send_due_notifications()
        self.stdout.write(self.style.SUCCESS(
            "Notifications: " + ", ".join(f"{key}={value}" for key, value in result.items())
        ))
