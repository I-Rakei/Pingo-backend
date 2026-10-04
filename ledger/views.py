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

from .client_notifications import notify_debt_created, notify_payment_received
from .models import (Client, Debt, DocumentSequence, MobileDevice, OrganizationMembership, Payment, Preference,
                     WebPushSubscription)
from .mobile_sync import snapshot_for_user, sync_snapshot
from .password_reset import reset_password, send_no_account_email, send_password_reset_email
from .push import get_vapid_public_key, send_push_to_user
from .sync_v2 import latest_cursor
from .serializers import (AmortizationScheduleSerializer, BalanceNoteSerializer, ClientPublicSerializer,
                          ClientSerializer, ClientShareSerializer, CreditNoteSerializer, DebitNoteSerializer,
                          DebtCreateSerializer, DebtSerializer, DebtUpdateSerializer, DocumentCreateSerializer,
                          InvoiceSerializer, MobileAuthSerializer, MobileSyncSerializer, OrganizationSerializer,
                          OrganizationSetupSerializer, PasswordResetConfirmSerializer, PasswordResetRequestSerializer,
                          PaymentRequestSerializer, PaymentSerializer, PreferenceSerializer, StaffCreateSerializer,
                          StaffMemberSerializer, StaffUpdateSerializer, UserSerializer, UserUpdateSerializer,
                          WebPushSubscriptionSerializer)
from .services import (assess_overdue_penalties, create_debt, create_organization_with_owner, create_staff_account, generate_share_token,
                       get_membership, issue_document, record_payment, remove_staff_account, require_owner_role,
                       resolve_scope, revert_installment, update_staff_account)

DOCUMENT_SERIALIZERS = {
    DocumentSequence.DocumentType.INVOICE: InvoiceSerializer,
    DocumentSequence.DocumentType.DEBIT_NOTE: DebitNoteSerializer,
    DocumentSequence.DocumentType.CREDIT_NOTE: CreditNoteSerializer,
    DocumentSequence.DocumentType.BALANCE_NOTE: BalanceNoteSerializer,
}


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


def _validate_organization_payload(request):
    """Shared by register_view/mobile_register_view. accountType defaults to
    personal; corporate requires an `organization` object with at least a
    name. Returns (errors_dict, validated_org_data_or_None)."""
    account_type = str(request.data.get("accountType", "personal")).strip().lower()
    if account_type != "corporate":
        return {}, None
    org_serializer = OrganizationSetupSerializer(data=request.data.get("organization") or {})
    if not org_serializer.is_valid():
        return {"organization": org_serializer.errors}, None
    return {}, org_serializer.validated_data


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
    org_errors, org_data = _validate_organization_payload(request)
    errors.update(org_errors)
    if errors:
        return Response(errors, status=status.HTTP_400_BAD_REQUEST)
    first_name, _, last_name = name.partition(" ")
    user = User.objects.create_user(username=email, email=email, password=password, first_name=first_name, last_name=last_name)
    preference_for(user)
    if org_data is not None:
        create_organization_with_owner(user, org_data)
    login(request, user)
    return Response({"user": UserSerializer(user).data, "settings": PreferenceSerializer(user.pingo_preferences).data}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([AllowAny])
def password_reset_request_view(request):
    """Shared by web and mobile. Always returns 200 regardless of whether the
    email belongs to an account, so this endpoint's HTTP response never
    reveals account existence to a caller. If the email has no account, the
    "no account" hint is sent to that mailbox instead of surfaced in the API
    response -- only someone who already controls that inbox ever sees it."""
    serializer = PasswordResetRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data["email"].strip().lower()
    user = User.objects.filter(username=email).first()
    if user:
        send_password_reset_email(user)
    else:
        send_no_account_email(email)
    return Response(status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([AllowAny])
def password_reset_confirm_view(request):
    """Shared by web and mobile. No session/token is issued here -- the
    caller logs in normally afterward with the new password."""
    serializer = PasswordResetConfirmSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    reset_password(**serializer.validated_data)
    return Response(status=status.HTTP_200_OK)


CORPORATE_MOBILE_BLOCK_MESSAGE = "Corporate accounts are not yet supported on mobile. Use the Pingo web app."


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
    # Corporate accounts are web-only in v1 (see MASTER_CONTEXT / Phase 4 plan).
    account_type = str(request.data.get("accountType", "personal")).strip().lower()
    if account_type == "corporate":
        errors["accountType"] = CORPORATE_MOBILE_BLOCK_MESSAGE
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
    if get_membership(user):
        return Response({"detail": CORPORATE_MOBILE_BLOCK_MESSAGE}, status=status.HTTP_403_FORBIDDEN)
    token, _ = Token.objects.get_or_create(user=user)
    return Response({"token": token.key, "user": UserSerializer(user).data})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def mobile_sync_view(request):
    if get_membership(request.user):
        return Response({"detail": CORPORATE_MOBILE_BLOCK_MESSAGE}, status=status.HTTP_403_FORBIDDEN)
    assess_overdue_penalties(resolve_scope(request.user))
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
    scope = resolve_scope(request.user)
    assess_overdue_penalties(scope)
    # Read the feed position before the data: anything committed in between is
    # replayed to the live socket, which only causes a harmless extra refresh.
    sync_cursor = latest_cursor(request.user)
    clients = Client.objects.filter(**scope)
    debts = Debt.objects.filter(**scope).select_related("client").prefetch_related("installments")
    payments = Payment.objects.filter(reversed_at__isnull=True, **scope).select_related("debt", "client", "installment")
    membership = get_membership(request.user)
    organization = None
    if membership:
        org = membership.organization
        organization = {"name": org.name, "role": membership.role, "nuit": org.nuit, "address": org.address}
    return Response({"user": UserSerializer(request.user).data, "settings": PreferenceSerializer(preference_for(request.user)).data,
                     "organization": organization, "syncCursor": sync_cursor,
                     "clients": ClientSerializer(clients, many=True).data, "debts": DebtSerializer(debts, many=True).data,
                     "payments": PaymentSerializer(payments, many=True).data})


@api_view(["GET"])
def dashboard_summary_view(request):
    assess_overdue_penalties(resolve_scope(request.user))
    debts = Debt.objects.filter(**resolve_scope(request.user))
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
    assess_overdue_penalties(resolve_scope(request.user))
    debts = Debt.objects.filter(**resolve_scope(request.user)).select_related("client").prefetch_related("installments")
    return Response(DebtSerializer(debts, many=True).data)


@api_view(["GET", "POST"])
def clients_view(request):
    scope = resolve_scope(request.user)
    if request.method == "GET":
        return Response(ClientSerializer(Client.objects.filter(**scope), many=True).data)
    serializer = ClientSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    client = serializer.save(owner=request.user, organization=scope.get("organization"))
    return Response(ClientSerializer(client).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
def client_detail_view(request, client_id):
    client = Client.objects.filter(pk=client_id, **resolve_scope(request.user)).first()
    if not client:
        return Response({"detail": "Client not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "PATCH":
        serializer = ClientSerializer(client, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        client = serializer.save()
    return Response(ClientSerializer(client).data)


@api_view(["GET", "POST"])
@csrf_protect
def client_share_view(request, client_id):
    client = Client.objects.filter(pk=client_id, **resolve_scope(request.user)).first()
    if not client:
        return Response({"detail": "Client not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "POST":
        generate_share_token(client)
    return Response(ClientShareSerializer(client).data)


@api_view(["GET", "POST"])
@csrf_protect
def client_share_by_public_id_view(request, public_id):
    """Same as client_share_view, keyed by public_id instead of the integer
    PK. Mobile only knows a synced client's server_id (public_id), never the
    Django-internal integer id, so it needs this lookup path."""
    client = Client.objects.filter(public_id=public_id, **resolve_scope(request.user)).first()
    if not client:
        return Response({"detail": "Client not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "POST":
        generate_share_token(client)
    return Response(ClientShareSerializer(client).data)


@api_view(["GET"])
@permission_classes([AllowAny])
def public_client_view(request, token):
    client = Client.objects.filter(share_token=token).first()
    if not client:
        return Response({"detail": "This link is invalid or has expired."}, status=status.HTTP_404_NOT_FOUND)
    return Response(ClientPublicSerializer(client).data)


@api_view(["GET", "POST", "DELETE"])
def debts_view(request):
    scope = resolve_scope(request.user)
    if request.method == "GET":
        assess_overdue_penalties(scope)
        debts = Debt.objects.filter(**scope).select_related("client").prefetch_related("installments")
        return Response(DebtSerializer(debts, many=True).data)
    if request.method == "DELETE":
        Debt.objects.filter(**scope).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    serializer = DebtCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    debt = create_debt(request.user, serializer.validated_data)
    debt = Debt.objects.select_related("client").prefetch_related("installments").get(pk=debt.pk)
    notify_debt_created(debt)
    return Response(DebtSerializer(debt).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
def debt_detail_view(request, reference):
    if request.method == "GET":
        assess_overdue_penalties(resolve_scope(request.user))
    debt = Debt.objects.filter(reference=reference, **resolve_scope(request.user)).select_related("client").prefetch_related("installments").first()
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
    queryset = Payment.objects.filter(reversed_at__isnull=True, **resolve_scope(request.user)).select_related("debt", "client", "installment")
    return Response(PaymentSerializer(queryset, many=True).data)


@api_view(["POST"])
def debt_payment_view(request, reference):
    serializer = PaymentRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    if not Debt.objects.filter(reference=reference, **resolve_scope(request.user)).exists():
        return Response({"detail": "Debt not found."}, status=status.HTTP_404_NOT_FOUND)
    debt, payments = record_payment(request.user, reference, serializer.validated_data)
    debt = Debt.objects.select_related("client").prefetch_related("installments").get(pk=debt.pk)
    for payment in payments:
        notify_payment_received(debt, payment)
    return Response({"debt": DebtSerializer(debt).data, "payments": PaymentSerializer(payments, many=True).data})


@api_view(["POST"])
def installment_revert_view(request, reference, number):
    if not Debt.objects.filter(reference=reference, **resolve_scope(request.user)).exists():
        return Response({"detail": "Debt not found."}, status=status.HTTP_404_NOT_FOUND)
    debt, payments = revert_installment(request.user, reference, number)
    debt = Debt.objects.select_related("client").prefetch_related("installments").get(pk=debt.pk)
    return Response({"debt": DebtSerializer(debt).data, "payments": PaymentSerializer(payments, many=True).data})


# ---------------------------------------------------------------------------
# Corporate accounts: staff management and fiscal documents
# ---------------------------------------------------------------------------

@api_view(["GET", "POST"])
def staff_list_view(request):
    membership = get_membership(request.user)
    if not membership:
        return Response({"detail": "This account is not part of an organization."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "GET":
        members = OrganizationMembership.objects.filter(organization=membership.organization).select_related("user").order_by("created_at")
        return Response(StaffMemberSerializer(members, many=True).data)
    serializer = StaffCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    user = create_staff_account(request.user, data["name"], data["email"], data["password"], data["role"])
    created_membership = OrganizationMembership.objects.get(user=user)
    return Response(StaffMemberSerializer(created_membership).data, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
def staff_detail_view(request, user_id):
    if request.method == "DELETE":
        remove_staff_account(request.user, user_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
    serializer = StaffUpdateSerializer(data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    _, staff_membership = update_staff_account(request.user, user_id, **serializer.validated_data)
    return Response(StaffMemberSerializer(staff_membership).data)


@api_view(["GET", "POST"])
def document_list_view(request, document_type):
    membership = get_membership(request.user)
    if not membership:
        return Response({"detail": "Only corporate accounts can use documents."}, status=status.HTTP_404_NOT_FOUND)
    model_serializer = DOCUMENT_SERIALIZERS.get(document_type)
    if model_serializer is None:
        return Response({"detail": "Unknown document type."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "GET":
        queryset = model_serializer.Meta.model.objects.filter(organization=membership.organization).select_related("client", "debt", "payment", "issued_by")
        return Response(model_serializer(queryset, many=True).data)
    serializer = DocumentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    extra = {}
    if document_type == DocumentSequence.DocumentType.CREDIT_NOTE and data.get("reason"):
        extra["reason"] = data["reason"]
    document = issue_document(
        request.user, document_type, data["clientId"],
        debt_reference=data.get("debtReference") or None, payment_id=data.get("paymentId"),
        amount=data["amount"], description=data.get("description", ""), **extra,
    )
    return Response(model_serializer(document).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
def amortization_schedule_view(request, reference):
    membership = get_membership(request.user)
    if not membership:
        return Response({"detail": "Only corporate accounts can use documents."}, status=status.HTTP_404_NOT_FOUND)
    debt = Debt.objects.filter(reference=reference, organization=membership.organization).select_related("client").prefetch_related("installments").first()
    if not debt:
        return Response({"detail": "Debt not found."}, status=status.HTTP_404_NOT_FOUND)
    payload = {"organization": membership.organization, "debt": debt}
    return Response(AmortizationScheduleSerializer(payload).data)
