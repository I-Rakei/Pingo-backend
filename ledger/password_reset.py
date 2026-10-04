"""Email-based password reset for both the web (session) and mobile (DRF
token) clients. Kept separate from services.py, which is pure financial
logic, to avoid blurring that file's purpose.

Both steps are deliberately platform-agnostic: request-reset takes only an
email and always returns success regardless of whether the account exists
(prevents account enumeration -- the HTTP response never reveals it either
way); confirm-reset takes the emailed token plus a new password and has no
session/token side effects beyond rotating the DRF token so any
already-connected mobile device must re-authenticate.

send_no_account_email() is the one deliberate exception to the "never reveal"
rule: it's only reachable by someone who already controls that mailbox, so
telling THEM (via email, not the API response) that no account exists and
inviting them to register is not an enumeration leak -- an outside attacker
probing the API never sees a different response either way.
"""

import logging

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)

token_generator = PasswordResetTokenGenerator()


def send_password_reset_email(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = token_generator.make_token(user)
    link = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/?reset_uid={uid}&reset_token={token}"
    try:
        send_mail(
            subject="Reset your Pingo password",
            message=(
                "We received a request to reset your Pingo password.\n\n"
                f"Open this link to choose a new password: {link}\n\n"
                "If you did not request this, you can ignore this email."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            fail_silently=True,  # never let a broken mail relay reveal account existence via a 500
        )
    except Exception:  # pragma: no cover - defensive: fail_silently should already catch send errors
        logger.exception("Failed to send password reset email for user id=%s", user.pk)


def send_no_account_email(email):
    """Sent instead of a reset link when the requested email has no Pingo
    account. See module docstring for why this does not weaken the API's
    enumeration-safety: the HTTP response stays generic regardless."""
    register_link = settings.FRONTEND_BASE_URL.rstrip("/") + "/"
    try:
        send_mail(
            subject="No Pingo account found for this email",
            message=(
                "We received a password reset request for this email address, "
                "but no Pingo account is registered with it.\n\n"
                f"If you'd like to create one, sign up here: {register_link}\n\n"
                "If you did not request this, you can ignore this email."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=True,
        )
    except Exception:  # pragma: no cover - defensive: fail_silently should already catch send errors
        logger.exception("Failed to send no-account email to %s", email)


def reset_password(uid, token, password):
    try:
        pk = force_str(urlsafe_base64_decode(uid))
        user = User.objects.get(pk=pk)
    except Exception as exc:
        raise ValidationError({"detail": "This reset link is invalid or has expired."}) from exc
    if not token_generator.check_token(user, token):
        raise ValidationError({"detail": "This reset link is invalid or has expired."})
    if len(password) < 8:
        raise ValidationError({"password": "Use at least 8 characters."})
    user.set_password(password)
    user.save(update_fields=["password"])
    Token.objects.filter(user=user).delete()  # force re-login everywhere; old tokens invalidated
    from django.db import transaction
    from .sync_log import notify_auth_revoked
    transaction.on_commit(lambda: notify_auth_revoked(user.pk))
    return user
