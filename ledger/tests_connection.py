"""Redis is optional: settings fall back to in-memory, and a dead broadcast backend is not retried per write."""

import os
import runpy
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from ledger import sync_log

SETTINGS_PATH = Path(settings.BASE_DIR) / "config" / "settings.py"


def load_settings(**env):
    with mock.patch.dict(os.environ, env):
        return runpy.run_path(str(SETTINGS_PATH))


class RedisOptionalSettingsTests(SimpleTestCase):
    def test_empty_redis_url_uses_memory_backends(self):
        loaded = load_settings(REDIS_URL="")
        self.assertEqual(loaded["CHANNEL_LAYERS"]["default"]["BACKEND"], "channels.layers.InMemoryChannelLayer")
        self.assertEqual(loaded["CACHES"]["default"]["BACKEND"], "django.core.cache.backends.locmem.LocMemCache")

    def test_redis_url_uses_redis_with_bounded_connect_wait(self):
        loaded = load_settings(REDIS_URL="redis://127.0.0.1:6379/0")
        layer = loaded["CHANNEL_LAYERS"]["default"]
        self.assertEqual(layer["BACKEND"], "channels_redis.core.RedisChannelLayer")
        host = layer["CONFIG"]["hosts"][0]
        self.assertEqual(host["address"], "redis://127.0.0.1:6379/0")
        self.assertEqual(host["socket_connect_timeout"], 1)
        # The layer's receive path blocks by design, so it must not get a read timeout.
        self.assertNotIn("socket_timeout", host)
        self.assertEqual(loaded["CACHES"]["default"]["BACKEND"], "django.core.cache.backends.redis.RedisCache")

    def test_debug_allows_both_local_dev_origins(self):
        loaded = load_settings(DJANGO_DEBUG="true", CORS_ALLOWED_ORIGINS="")
        self.assertEqual(loaded["CORS_ALLOWED_ORIGINS"], ["http://localhost:5173", "http://127.0.0.1:5173"])

    def test_production_origin_list_is_exactly_what_is_configured(self):
        loaded = load_settings(DJANGO_DEBUG="false", CORS_ALLOWED_ORIGINS="https://pingo.rakei.co.za")
        self.assertEqual(loaded["CORS_ALLOWED_ORIGINS"], ["https://pingo.rakei.co.za"])
        loaded = load_settings(DJANGO_DEBUG="false", CORS_ALLOWED_ORIGINS="")
        self.assertEqual(loaded["CORS_ALLOWED_ORIGINS"], ["http://localhost:5173"])


class BroadcastCircuitBreakerTests(SimpleTestCase):
    scope = {"organization_id": None, "owner_id": 7}

    def setUp(self):
        sync_log._broadcast_paused_until = 0.0
        self.addCleanup(setattr, sync_log, "_broadcast_paused_until", 0.0)

    def make_layer(self, fail):
        calls = []

        class Layer:
            async def group_send(self, group, message):
                calls.append(group)
                if fail:
                    raise ConnectionError("redis down")

        return Layer(), calls

    def test_failed_broadcast_pauses_further_attempts(self):
        layer, calls = self.make_layer(fail=True)
        with mock.patch("channels.layers.get_channel_layer", return_value=layer):
            for _ in range(5):
                sync_log.notify_scope(self.scope, 1, 2)
        self.assertEqual(calls, ["ledger.user.7"])

    def test_broadcast_resumes_after_the_pause(self):
        layer, calls = self.make_layer(fail=True)
        with mock.patch("channels.layers.get_channel_layer", return_value=layer):
            sync_log.notify_scope(self.scope, 1, 2)
            sync_log._broadcast_paused_until = 0.0
            sync_log.notify_scope(self.scope, 3, 4)
        self.assertEqual(len(calls), 2)

    def test_healthy_layer_is_never_paused(self):
        layer, calls = self.make_layer(fail=False)
        with mock.patch("channels.layers.get_channel_layer", return_value=layer):
            for _ in range(3):
                sync_log.notify_scope(self.scope, 1, 2)
        self.assertEqual(len(calls), 3)

    def test_auth_revocation_ignores_the_pause(self):
        sync_log._broadcast_paused_until = float("inf")
        layer, calls = self.make_layer(fail=False)
        with mock.patch("channels.layers.get_channel_layer", return_value=layer):
            sync_log.notify_auth_revoked(7)
        self.assertEqual(calls, ["auth.user.7"])
