from django.apps import AppConfig


class LedgerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ledger"

    def ready(self):
        import logging

        from django.conf import settings

        from . import sync_log  # noqa: F401
        from . import client_notifications  # noqa: F401

        if not settings.DEBUG and not settings.REDIS_URL:
            logging.getLogger(__name__).warning(
                "REDIS_URL is not set: websocket groups and socket counts are per-process. "
                "This is fine for one process; set REDIS_URL before running several ASGI workers.")
