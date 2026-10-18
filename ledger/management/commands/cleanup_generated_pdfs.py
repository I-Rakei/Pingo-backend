from django.core.management.base import BaseCommand

from ledger import pdf_store


class Command(BaseCommand):
    help = "Delete generated PDFs older than the limit. The notification worker runs this every 10 minutes."

    def add_arguments(self, parser):
        parser.add_argument("--max-age", type=int, default=None,
                            help="Delete files older than this many seconds (default: GENERATED_PDF_TTL_SECONDS).")

    def handle(self, *args, **options):
        removed = pdf_store.delete_expired(options["max_age"])
        self.stdout.write(self.style.SUCCESS(f"Deleted {removed} generated PDF(s)."))
