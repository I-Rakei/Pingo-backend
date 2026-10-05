from django.apps import AppConfig


class LedgerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ledger"

    def ready(self):
        from . import sync_log  # noqa: F401
        from . import client_notifications  # noqa: F401
