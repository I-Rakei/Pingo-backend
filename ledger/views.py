from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User
from django.db import transaction
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import Client, Debt, MobileDevice, Payment, Preference, WebPushSubscription
from .mobile_sync import snapshot_for_user, sync_snapshot
from .push import get_vapid_public_key, send_push_to_user
from .serializers import (ClientSerializer, DebtCreateSerializer, DebtSerializer, DebtUpdateSerializer,
                          MobileAuthSerializer, MobileSyncSerializer, PaymentRequestSerializer, PaymentSerializer,
                          PreferenceSerializer, UserSerializer, UserUpdateSerializer, WebPushSubscriptionSerializer)
from .services import create_debt, record_payment, revert_installment


def preference_for(user):
    preference, _ = Preference.objects.get_or_create(owner=user)
    return preference


@api_view(["GET"])
@permission_classes([AllowAny])
@ensure_csrf_cookie
def csrf(request):
    return Response({"csrfToken": get_token(request)})


@api_view(["POST"])
@permission_classes([AllowAny])
@csrf_protect
def login_view(request):
    email = str(request.data.get("email", "")).strip().lower()
    password = request.data.get("password", "")
    user = authenticate(request, username=email, password=password)
    if not user:
        return Response({"detail": "Invalid email or password."}, status=status.HTTP_400_BAD_REQUEST)
    login(request, user)
    return Response({"user": UserSerializer(user).data, "settings": PreferenceSerializer(preference_for(user)).data})


@api_view(["POST"])
@permission_classes([AllowAny])
@csrf_protect
def register_view(request):
    email = str(request.data.get("email", "")).strip().lower()
    password = request.data.get("password", "")
    name = str(request.data.get("name", "")).strip()
    errors = {}
    if not email or "@" not in email:
        errors["email"] = "Enter a valid email address."
    elif User.objects.filter(username=email).exists():
        errors["email"] = "An account with this email already exists."
    if len(password) < 8:
        errors["password"] = "Use at least 8 characters."
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)
    first_name, _, last_name = name.partition(" ")
    user = User.objects.create_user(username=email, email=email, password=password, first_name=first_name, last_name=last_name)
    preference_for(user)
    login(request, user)
    return Response({"user": UserSerializer(user).data, "settings": PreferenceSerializer(user.pingo_preferences).data}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
def mobile_register_view(request):
    """Create an opt-in cloud account while leaving the phone database untouched."""
    serializer = MobileAuthSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data["email"].strip().lower()
    password = serializer.validated_data["password"]
    name = serializer.validated_data.get("name", "").strip()
    errors = {}
    if len(password) < 8:
        errors["password"] = "Use at least 8 characters."
    if User.objects.filter(username=email).exists():
        errors["email"] = "An account with this email already exists. Log in to migrate to it."
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)
    first_name, _, last_name = name.partition(" ")
    user = User.objects.create_user(username=email, email=email, password=password, first_name=first_name, last_name=last_name)
    preference_for(user)
    token, _ = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "user": UserSerializer(user).data}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
def mobile_login_view(request):
    serializer = MobileAuthSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data["email"].strip().lower()
    user = authenticate(request, username=email, password=serializer.validated_data["password"])
    if not user:
        return Response({"detail": "Invalid email or password."}, status=status.HTTP_400_BAD_REQUEST)
    token, _ = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "user": UserSerializer(user).data})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def mobile_sync_view(request):
    if request.method == "GET":
        device_id = str(request.query_params.get("deviceId", "")).strip()
        device = None
        if device_id:
            device = MobileDevice.objects.filter(owner=request.user, device_id=device_id).first()
        return Response({"snapshot": snapshot_for_user(request.user, device), "serverTime": timezone.now()})

    serializer = MobileSyncSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    counts, replayed = sync_snapshot(
        user=request.user,
        device_id=data["deviceId"],
        device_label=data.get("deviceLabel", ""),
        batch_id=data["batchId"],
        snapshot=data["snapshot"],
    )
    device = MobileDevice.objects.get(owner=request.user, device_id=data["deviceId"])
    return Response({
        "status": "already_processed" if replayed else "completed",
        "counts": counts,
        "snapshot": snapshot_for_user(request.user, device),
        "serverTime": timezone.now(),
    })


@api_view(["GET"])
@permission_classes([AllowAny])
def push_config_view(request):
    public_key = get_vapid_public_key()
    return Response({"available": bool(public_key), "publicKey": public_key})


@api_view(["GET", "POST", "DELETE"])
def push_subscription_view(request):
    if request.method == "GET":
        return Response({"enabled": WebPushSubscription.objects.filter(owner=request.user).exists()})
    if request.method == "DELETE":
        endpoint = str(request.data.get("endpoint", "")).strip()
        queryset = WebPushSubscription.objects.filter(owner=request.user)
        if endpoint:
            queryset = queryset.filter(endpoint=endpoint)
        deleted, _ = queryset.delete()
        return Response({"enabled": False, "deleted": deleted})

    serializer = WebPushSubscriptionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    subscription, created = WebPushSubscription.objects.update_or_create(
        endpoint=data["endpoint"],
        defaults={
            "owner": request.user,
            "p256dh": data["keys"]["p256dh"],
            "auth": data["keys"]["auth"],
            "user_agent": request.headers.get("User-Agent", "")[:500],
        },
    )
    return Response({"enabled": True, "id": subscription.public_id}, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@api_view(["POST"])
def push_test_view(request):
    if not WebPushSubscription.objects.filter(owner=request.user).exists():
        return Response({"detail": "Enable browser notifications first."}, status=status.HTTP_400_BAD_REQUEST)
    result = send_push_to_user(request.user, {
        "title": "Pingo notifications are active",
        "body": "Payment reminders can now arrive even when Pingo is closed.",
        "url": "/",
        "tag": "pingo-test",
    })
    if not result["configured"]:
        return Response({"detail": "Push delivery is not configured on the server."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    if not result["sent"]:
        return Response({"detail": "The push service did not accept the notification.", "result": result}, status=status.HTTP_502_BAD_GATEWAY)
    return Response(result)


@api_view(["POST"])
@csrf_protect
def logout_view(request):
    logout(request)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET", "PATCH"])
def me_view(request):
    if request.method == "PATCH":
        serializer = UserUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        first_name, _, last_name = serializer.validated_data["name"].strip().partition(" ")
        request.user.first_name, request.user.last_name = first_name, last_name
        request.user.save(update_fields=["first_name", "last_name"])
    return Response(UserSerializer(request.user).data)


@api_view(["GET", "PATCH"])
def preferences_view(request):
    preference = preference_for(request.user)
    if request.method == "PATCH":
        serializer = PreferenceSerializer(preference, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
    return Response(PreferenceSerializer(preference).data)


@api_view(["GET"])
def bootstrap_view(request):
    clients = Client.objects.filter(owner=request.user)
    debts = Debt.objects.filter(owner=request.user).select_related("client").prefetch_related("installments")
    payments = Payment.objects.filter(owner=request.user, reversed_at__isnull=True).select_related("debt", "client", "installment")
    return Response({"user": UserSerializer(request.user).data, "settings": PreferenceSerializer(preference_for(request.user)).data,
                     "clients": ClientSerializer(clients, many=True).data, "debts": DebtSerializer(debts, many=True).data,
                     "payments": PaymentSerializer(payments, many=True).data})


@api_view(["GET"])
def dashboard_summary_view(request):
    debts = Debt.objects.filter(owner=request.user)
    open_debts = [debt for debt in debts if debt.current_status != Debt.Status.PAID]
    return Response({
        "outstanding": sum((debt.outstanding for debt in debts), 0),
        "collected": sum((debt.collected for debt in debts), 0),
        "overdue": sum(debt.current_status == Debt.Status.OVERDUE for debt in debts),
        "active": len(open_debts),
        "total": debts.count(),
    })


@api_view(["GET"])
def dashboard_debts_view(request):
    debts = Debt.objects.filter(owner=request.user).select_related("client").prefetch_related("installments")
    return Response(DebtSerializer(debts, many=True).data)


@api_view(["GET", "POST"])
def clients_view(request):
    if request.method == "GET":
        return Response(ClientSerializer(Client.objects.filter(owner=request.user), many=True).data)
    serializer = ClientSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    client = serializer.save(owner=request.user)
    return Response(ClientSerializer(client).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
def client_detail_view(request, client_id):
    client = Client.objects.filter(owner=request.user, pk=client_id).first()
    if not client:
        return Response({"detail": "Client not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "PATCH":
        serializer = ClientSerializer(client, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        client = serializer.save()
    return Response(ClientSerializer(client).data)


@api_view(["GET", "POST", "DELETE"])
def debts_view(request):
    if request.method == "GET":
        debts = Debt.objects.filter(owner=request.user).select_related("client").prefetch_related("installments")
        return Response(DebtSerializer(debts, many=True).data)
    if request.method == "DELETE":
        Debt.objects.filter(owner=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    serializer = DebtCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    debt = create_debt(request.user, serializer.validated_data)
    debt = Debt.objects.select_related("client").prefetch_related("installments").get(pk=debt.pk)
    return Response(DebtSerializer(debt).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
def debt_detail_view(request, reference):
    debt = Debt.objects.filter(owner=request.user, reference=reference).select_related("client").prefetch_related("installments").first()
    if not debt:
        return Response({"detail": "Debt not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        debt.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    if request.method == "PATCH":
        serializer = DebtUpdateSerializer(debt, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        debt = serializer.save()
        debt.set_status()
        debt.save(update_fields=["status", "updated_at"])
    return Response(DebtSerializer(debt).data)


@api_view(["GET"])
def payments_view(request):
    queryset = Payment.objects.filter(owner=request.user, reversed_at__isnull=True).select_related("debt", "client", "installment")
    return Response(PaymentSerializer(queryset, many=True).data)


@api_view(["POST"])
def debt_payment_view(request, reference):
    serializer = PaymentRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if not Debt.objects.filter(owner=request.user, reference=reference).exists():
        return Response({"detail": "Debt not found."}, status=status.HTTP_404_NOT_FOUND)
    debt, payments = record_payment(request.user, reference, serializer.validated_data)
    debt = Debt.objects.select_related("client").prefetch_related("installments").get(pk=debt.pk)
    return Response({"debt": DebtSerializer(debt).data, "payments": PaymentSerializer(payments, many=True).data})


@api_view(["POST"])
def installment_revert_view(request, reference, number):
    if not Debt.objects.filter(owner=request.user, reference=reference).exists():
        return Response({"detail": "Debt not found."}, status=status.HTTP_404_NOT_FOUND)
    debt, payments = revert_installment(request.user, reference, number)
    debt = Debt.objects.select_related("client").prefetch_related("installments").get(pk=debt.pk)
    return Response({"debt": DebtSerializer(debt).data, "payments": PaymentSerializer(payments, many=True).data})
