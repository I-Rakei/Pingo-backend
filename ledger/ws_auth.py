"""Token handshakes for native clients; same-site session handshakes for web."""

from urllib.parse import urlparse

from channels.db import database_sync_to_async
from channels.security.websocket import OriginValidator
from django.conf import settings
from rest_framework.authtoken.models import Token


@database_sync_to_async
def token_user(key):
    token = Token.objects.select_related("user").filter(key=key).first()
    return token.user if token and token.user.is_active else None


class TokenOrSessionAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode("latin1")
        if authorization.startswith("Token "):
            key = authorization[6:].strip()
            user = await token_user(key) if key else None
            if user is None:
                await send({"type": "websocket.close", "code": 4401})
                return
            scope = {**scope, "user": user, "auth_kind": "token"}
        elif authorization:
            await send({"type": "websocket.close", "code": 4401})
            return
        else:
            # Browsers send the session cookie automatically. Require an exact
            # configured web origin, including scheme and port.
            raw_origin = headers.get(b"origin", b"").decode("latin1")
            try:
                valid_origin = raw_origin in settings.CORS_ALLOWED_ORIGINS and OriginValidator(self.app, settings.CORS_ALLOWED_ORIGINS).valid_origin(urlparse(raw_origin))
            except ValueError:
                valid_origin = False
            if not valid_origin:
                await send({"type": "websocket.close", "code": 4401})
                return
            scope = {**scope, "auth_kind": "session"}
        await self.app(scope, receive, send)
