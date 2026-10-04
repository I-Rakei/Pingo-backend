"""HTTP transport for the same JSON messages carried by the v2 socket."""

from django.conf import settings
from rest_framework.decorators import api_view
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.authtoken.models import Token

from .services import get_membership
from .sync_v2 import apply_mutation, bootstrap, feed, require_device, welcome


def _device_id(request):
    # The push JSON is transport-identical to a WebSocket push. HTTP carries
    # its connection identity in a query parameter or header instead.
    value = request.query_params.get("deviceId") or request.headers.get("X-Pingo-Device-Id")
    if not value:
        raise ValidationError({"deviceId": "A device ID is required."})
    return value


def _check_mobile_scope(request):
    if isinstance(request.auth, Token) and get_membership(request.user):
        raise PermissionDenied("Corporate accounts are not yet supported on mobile.")


@api_view(["GET"])
def config_view(request):
    return Response({"mobileV2Enabled": settings.PINGO_MOBILE_V2_ENABLED and not bool(get_membership(request.user))})


@api_view(["POST"])
def hello_view(request):
    _check_mobile_scope(request)
    _, result = welcome(request.user, request.data)
    return Response(result)


@api_view(["GET"])
def changes_view(request):
    _check_mobile_scope(request)
    require_device(request.user, _device_id(request))
    return Response(feed(request.user, request.query_params.get("cursor", 0), request.query_params.get("limit", 500)))


@api_view(["POST"])
def push_view(request):
    _check_mobile_scope(request)
    device = require_device(request.user, _device_id(request))
    payload = request.data if isinstance(request.data, dict) else {}
    try:
        return Response(apply_mutation(request.user, device, payload))
    except ValidationError:
        # Same reply as the socket. An HTTP 400 here would make the phone retry
        # this malformed entry forever and block every change queued after it.
        return Response({"type": "push_result", "mutationId": payload.get("mutationId"),
                         "status": "rejected", "error": "invalid_push"})


@api_view(["GET"])
def bootstrap_view(request):
    _check_mobile_scope(request)
    require_device(request.user, _device_id(request))
    return Response(bootstrap(request.user))
