from django.urls import path

from .consumers import LedgerConsumer

websocket_urlpatterns = [path("ws/ledger/", LedgerConsumer.as_asgi())]
