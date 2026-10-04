"""Cursor-based WebSocket transport for protocol v2."""

import asyncio
import json
import logging
import time
from collections import deque

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.core.cache import cache
from rest_framework.exceptions import ValidationError

from .services import resolve_scope
from .sync_v2 import apply_mutation, feed, welcome

logger = logging.getLogger(__name__)


@database_sync_to_async
def socket_group(user):
    scope = resolve_scope(user)
    return f"ledger.org.{scope['organization'].pk}" if "organization" in scope else f"ledger.user.{user.pk}"


@database_sync_to_async
def reserve_socket(user_id):
    key = f"pingo:sockets:{user_id}"
    cache.add(key, 0, timeout=120)
    count = cache.incr(key)
    cache.touch(key, timeout=120)
    if count > 5:
        cache.decr(key)
        return False
    return True


@database_sync_to_async
def release_socket(user_id):
    key = f"pingo:sockets:{user_id}"
    if cache.get(key, 0) > 0:
        cache.decr(key)


@database_sync_to_async
def touch_socket(user_id):
    cache.touch(f"pingo:sockets:{user_id}", timeout=120)


class LedgerConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        self.bound_device = None
        self.sent_cursor = None
        self.last_ack = 0
        self.push_times = deque()
        self.push_busy = False
        self.push_task = None
        self.feed_lock = asyncio.Lock()
        self.reserved = False
        user = self.scope.get("user")
        if not user or not user.is_authenticated or not user.is_active:
            await self.close(code=4401)
            return
        try:
            self.reserved = await reserve_socket(user.pk)
            if not self.reserved:
                await self.close(code=4429)
                return
            self.ledger_group = await socket_group(user)
            self.auth_group = f"auth.user.{user.pk}"
            await self.channel_layer.group_add(self.ledger_group, self.channel_name)
            await self.channel_layer.group_add(self.auth_group, self.channel_name)
        except Exception:
            if self.reserved:
                await release_socket(user.pk)
                self.reserved = False
            await self.close(code=1013)
            return
        await self.accept()

    async def disconnect(self, code):
        if self.push_task is not None:
            self.push_task.cancel()
        if self.reserved:
            await self.channel_layer.group_discard(self.ledger_group, self.channel_name)
            await self.channel_layer.group_discard(self.auth_group, self.channel_name)
            await release_socket(self.scope["user"].pk)
            self.reserved = False

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        if bytes_data is not None or text_data is None or len(text_data.encode("utf-8")) > 1024 * 1024:
            await self.close(code=1009)
            return
        try:
            await super().receive(text_data=text_data, **kwargs)
        except (json.JSONDecodeError, UnicodeError):
            await self.send_json({"type": "error", "reason": "invalid_json"})

    async def receive_json(self, content, **kwargs):
        if not isinstance(content, dict):
            await self.send_json({"type": "error", "reason": "invalid_message"})
            return
        kind = content.get("type")
        if kind == "ping":
            await touch_socket(self.scope["user"].pk)
            await self.send_json({"type": "pong"})
        elif kind == "hello":
            if self.bound_device is not None:
                await self.send_json({"type": "error", "reason": "already_bound"})
                return
            try:
                device, response = await database_sync_to_async(welcome)(self.scope["user"], content)
            except ValidationError as exc:
                if "schemaVersion" in exc.detail:
                    await self.send_json({"type": "resync_required", "reason": "unsupported_schema"})
                elif "deviceId" in exc.detail:
                    await self.close(code=4401)
                else:
                    await self.send_json({"type": "error", "reason": "invalid_hello"})
                return
            self.bound_device = device
            await self.send_json(response)
            if response["resyncRequired"]:
                await self.send_json({"type": "resync_required", "reason": "cursor_expired"})
            else:
                self.sent_cursor = int(content.get("cursor", 0))
                await self._drain()
        elif kind == "ack" and self.bound_device is not None:
            try:
                cursor = int(content.get("cursor"))
            except (ValueError, TypeError):
                return
            if 0 <= cursor <= (self.sent_cursor or 0):
                self.last_ack = max(self.last_ack, cursor)
        elif kind == "push" and self.bound_device is not None:
            if self.push_busy:
                await self.send_json({"type": "push_result", "mutationId": content.get("mutationId"), "status": "rate_limited"})
                return
            now = time.monotonic()
            while self.push_times and self.push_times[0] <= now - 60:
                self.push_times.popleft()
            if len(self.push_times) >= 20:
                await self.send_json({"type": "push_result", "mutationId": content.get("mutationId"), "status": "rate_limited"})
                return
            self.push_times.append(now)
            self.push_busy = True
            self.push_task = asyncio.create_task(self._apply_push(content))
        else:
            await self.send_json({"type": "error", "reason": "hello_required" if self.bound_device is None else "unknown_type"})

    async def _apply_push(self, content):
        try:
            result = await database_sync_to_async(apply_mutation)(self.scope["user"], self.bound_device, content)
        except ValidationError:
            result = {"type": "push_result", "mutationId": content.get("mutationId"), "status": "rejected", "error": "invalid_push"}
        except Exception:
            logger.exception("Socket push failed")
            result = {"type": "push_result", "mutationId": content.get("mutationId"), "status": "rejected", "error": "retry_later"}
        finally:
            self.push_busy = False
        await self.send_json(result)

    async def _drain(self):
        if self.sent_cursor is None:
            return
        async with self.feed_lock:
            while True:
                page = await database_sync_to_async(feed)(self.scope["user"], self.sent_cursor)
                if page["type"] == "resync_required":
                    self.sent_cursor = None
                    await self.send_json(page)
                    return
                if not page["items"]:
                    return
                await self.send_json(page)
                self.sent_cursor = page["toCursor"]
                if not page["hasMore"]:
                    return

    async def ledger_changed(self, event):
        if self.sent_cursor is not None and self.sent_cursor < event["to"]:
            await self._drain()

    async def auth_revoked(self, event):
        await self.close(code=4401)
