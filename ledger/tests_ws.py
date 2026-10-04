"""ASGI socket contract and account isolation with an in-memory channel layer."""

import uuid

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.test import Client as TestClient, TransactionTestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.authtoken.models import Token

from config.asgi import application
from .models import Client
from .password_reset import reset_password, token_generator


@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    CORS_ALLOWED_ORIGINS=["http://localhost:5173"],
)
class LedgerSocketTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user("socket-a", "a@example.test", "password123")
        self.other = User.objects.create_user("socket-b", "b@example.test", "password123")
        self.token = Token.objects.create(user=self.user)
        self.other_token = Token.objects.create(user=self.other)

    def socket(self, token=None, origin=None, cookie=None):
        headers = []
        if token:
            headers.append((b"authorization", f"Token {token.key}".encode()))
        if origin:
            headers.append((b"origin", origin.encode()))
        if cookie:
            headers.append((b"cookie", cookie.encode()))
        return WebsocketCommunicator(application, "/ws/ledger/", headers=headers)

    async def hello(self, socket, device_id=None, **extra):
        device_id = device_id or str(uuid.uuid4())
        await socket.send_json_to({"type": "hello", "v": 2, "app": "mobile", "schemaVersion": 4,
                                   "deviceId": device_id, "cursor": 0, **extra})
        welcome = await socket.receive_json_from(timeout=2)
        self.assertEqual(welcome["type"], "welcome")
        return device_id

    async def test_handshake_and_device_binding(self):
        anonymous = self.socket(origin="http://localhost:5173")
        connected, code = await anonymous.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4401)

        bad_origin = self.socket(origin="https://evil.example")
        connected, code = await bad_origin.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4401)

        invalid_token = self.socket(token=type("T", (), {"key": "bad"})())
        connected, code = await invalid_token.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4401)

        a = self.socket(token=self.token)
        connected, _ = await a.connect()
        self.assertTrue(connected)
        device_id = await self.hello(a)
        b = self.socket(token=self.other_token)
        connected, _ = await b.connect()
        self.assertTrue(connected)
        await b.send_json_to({"type": "hello", "v": 2, "app": "mobile", "schemaVersion": 4,
                              "deviceId": device_id, "cursor": 0})
        self.assertEqual((await b.receive_output(timeout=2))["code"], 4401)
        await a.disconnect()

    async def test_session_and_scoped_change_feed(self):
        web = TestClient()
        self.assertTrue(await database_sync_to_async(web.login)(username="socket-a", password="password123"))
        cookie = f"sessionid={web.cookies['sessionid'].value}"
        session_user = await database_sync_to_async(lambda: Session.objects.get(session_key=web.cookies['sessionid'].value).get_decoded().get('_auth_user_id'))()
        self.assertEqual(session_user, str(self.user.pk))
        a = self.socket(origin="http://localhost:5173", cookie=cookie)
        b = self.socket(token=self.other_token)
        self.assertEqual((await a.connect()), (True, None))
        self.assertTrue((await b.connect())[0])
        await a.send_json_to({"type": "hello", "v": 2, "app": "web", "deviceId": str(uuid.uuid4()), "cursor": 0})
        self.assertEqual((await a.receive_json_from())["type"], "welcome")
        await self.hello(b)

        created = await database_sync_to_async(Client.objects.create)(owner=self.user, name="A client")
        page = await a.receive_json_from(timeout=2)
        self.assertEqual(page["type"], "changes")
        self.assertEqual(page["items"][0]["id"], str(created.public_id))
        self.assertTrue(await b.receive_nothing(timeout=0.2))

        await database_sync_to_async(Client.objects.create)(owner=self.other, name="B client")
        other_page = await b.receive_json_from(timeout=2)
        self.assertEqual(other_page["items"][0]["fields"]["name"], "B client")
        self.assertTrue(await a.receive_nothing(timeout=0.2))
        await a.disconnect()
        await b.disconnect()

    async def test_password_reset_closes_socket(self):
        socket = self.socket(token=self.token)
        self.assertTrue((await socket.connect())[0])
        await self.hello(socket)
        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        reset_token = token_generator.make_token(self.user)
        await database_sync_to_async(reset_password)(uid, reset_token, "newpassword123")
        closed = await socket.receive_output(timeout=2)
        self.assertEqual(closed["type"], "websocket.close")
        self.assertEqual(closed["code"], 4401)

    async def test_catch_up_and_push(self):
        client = await database_sync_to_async(Client.objects.create)(owner=self.user, name="Existing")
        socket = self.socket(token=self.token)
        self.assertTrue((await socket.connect())[0])
        await self.hello(socket)
        page = await socket.receive_json_from(timeout=2)
        self.assertEqual(page["items"][0]["id"], str(client.public_id))
        await socket.send_json_to({"type": "ack", "cursor": page["toCursor"]})
        await socket.send_json_to({"type": "ping"})
        self.assertEqual((await socket.receive_json_from())["type"], "pong")

        await socket.send_json_to({"type": "push", "mutationId": str(uuid.uuid4()), "action": "add client",
                                   "changes": [{"entity": "client", "op": "insert", "id": str(uuid.uuid4()),
                                                "fields": {"name": "New client"}}]})
        responses = [await socket.receive_json_from(timeout=2) for _ in range(2)]
        result = next(response for response in responses if response["type"] == "push_result")
        self.assertEqual(result["status"], "applied")
        self.assertTrue(await database_sync_to_async(Client.objects.filter(owner=self.user, name="New client").exists)())
        await socket.disconnect()

    async def test_socket_limit_and_sequential_load(self):
        sockets = [self.socket(token=self.token) for _ in range(5)]
        for socket in sockets:
            self.assertTrue((await socket.connect())[0])
            await self.hello(socket)
        sixth = self.socket(token=self.token)
        self.assertEqual(await sixth.connect(), (False, 4429))

        # Every one of five connections sees every one of 100 committed writes.
        # Sequential writes keep the SQLite transaction model realistic.
        for index in range(100):
            await database_sync_to_async(Client.objects.create)(owner=self.user, name=f"Load {index}")
            pages = [await socket.receive_json_from(timeout=2) for socket in sockets]
            self.assertTrue(all(page["type"] == "changes" and len(page["items"]) == 1 for page in pages))
        for socket in sockets:
            await socket.disconnect()

    async def test_two_web_sessions_receive_a_change(self):
        web = TestClient()
        self.assertTrue(await database_sync_to_async(web.login)(username="socket-a", password="password123"))
        cookie = f"sessionid={web.cookies['sessionid'].value}"
        tabs = [self.socket(origin="http://localhost:5173", cookie=cookie) for _ in range(2)]
        for tab in tabs:
            self.assertTrue((await tab.connect())[0])
            await tab.send_json_to({"type": "hello", "v": 2, "app": "web",
                                    "deviceId": str(uuid.uuid4()), "cursor": 0})
            self.assertEqual((await tab.receive_json_from(timeout=2))["type"], "welcome")
        client = await database_sync_to_async(Client.objects.create)(owner=self.user, name="From tab one")
        for tab in tabs:
            page = await tab.receive_json_from(timeout=2)
            self.assertEqual(page["items"][0]["id"], str(client.public_id))
            await tab.disconnect()
