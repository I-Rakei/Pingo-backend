import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Keep local setup dependency-free while allowing the supplied .env.example to work.
for env_line in (BASE_DIR / ".env").read_text().splitlines() if (BASE_DIR / ".env").exists() else []:
    if "=" in env_line and not env_line.lstrip().startswith("#"):
        env_key, env_value = env_line.split("=", 1)
        os.environ.setdefault(env_key.strip(), env_value.strip())

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "development-only-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [host for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host]

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "corsheaders", "rest_framework", "rest_framework.authtoken", "ledger",
]
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware", "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware", "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True,
              "OPTIONS": {"context_processors": ["django.template.context_processors.request", "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages"]}}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3",
                         "OPTIONS": {"init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;",
                                     "transaction_mode": "IMMEDIATE"}}}
AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = os.getenv("TIME_ZONE", "Africa/Johannesburg")
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    # Browser requests continue to use CSRF-protected sessions. Native clients
    # authenticate independently with a DRF token on the mobile endpoints.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "COERCE_DECIMAL_TO_STRING": False,
}
CORS_ALLOWED_ORIGINS = [origin for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",") if origin]
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS

# The web origin that serves the SPA, used to build links that must open the
# React app directly (client share links, password reset links).
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://localhost:5173")

# Email is unconfigured by default: the console backend prints outgoing mail
# to the runserver log instead of sending it, so password reset can be
# developed and tested locally without any real SMTP credentials. Set
# DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend plus the
# EMAIL_* variables in production with whichever provider is chosen (Gmail
# SMTP, SendGrid, Resend, etc.).
EMAIL_BACKEND = os.getenv("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
# Providers differ: Gmail/most SMTP relays use STARTTLS on 587 (EMAIL_USE_TLS);
# Resend's relay uses implicit SSL on 465 (EMAIL_USE_SSL). Only one of the two
# should be true at once -- Django raises if both are set.
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "false").lower() == "true"
if EMAIL_USE_SSL:
    EMAIL_USE_TLS = False
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "Pingo <no-reply@pingo.local>")

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", str(BASE_DIR / ".secrets" / "vapid_private.pem"))
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "mailto:admin@pingo.local")
