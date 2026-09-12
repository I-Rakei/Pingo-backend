import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Generate the VAPID key used for browser push notifications."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Replace an existing private key.")

    def handle(self, *args, **options):
        path = Path(settings.VAPID_PRIVATE_KEY)
        if not path.is_absolute():
            path = settings.BASE_DIR / path
        if path.exists() and not options["force"]:
            raise CommandError(f"VAPID private key already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)

        private_key = ec.generate_private_key(ec.SECP256R1())
        path.write_bytes(private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        public_bytes = private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        public_key = base64.urlsafe_b64encode(public_bytes).rstrip(b"=").decode("ascii")
        self.stdout.write(self.style.SUCCESS(f"Created VAPID private key: {path}"))
        self.stdout.write(f"Public key: {public_key}")
