"""HTTP and authenticated realtime ledger transport."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.auth import AuthMiddlewareStack  # noqa: E402
from django.core.asgi import get_asgi_application  # noqa: E402

django_asgi_app = get_asgi_application()

from ledger.routing import websocket_urlpatterns  # noqa: E402
from ledger.ws_auth import TokenOrSessionAuthMiddleware  # noqa: E402

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(TokenOrSessionAuthMiddleware(URLRouter(websocket_urlpatterns))),
})
