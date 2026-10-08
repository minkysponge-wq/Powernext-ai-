import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
from csp.constants import NONE, SELF
from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env", override=False)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_THINKING_LEVEL = os.getenv("GEMINI_THINKING_LEVEL", "medium")
EXTRACTION_PROVIDER = os.getenv("EXTRACTION_PROVIDER", "gemini")
DEMO_EXTRACTION_CACHE = (
    Path(os.getenv("VECTORLAB_DEMO_CACHE_DIR", ""))
    if os.getenv("VECTORLAB_DEMO_CACHE_DIR")
    else None
)
DEMO_ALLOW_LEGACY_CACHE = os.getenv("VECTORLAB_DEMO_ALLOW_LEGACY_CACHE", "0") == "1"
INSIGHT_PROVIDER = os.getenv("INSIGHT_PROVIDER", "gemini")
CLOUD_AI_ALLOWED = os.getenv("CLOUD_AI_ALLOWED", "0") == "1"
# Separate cost switch: leave paid outbound AI off unless an operator explicitly enables it.
PAID_AI_ALLOWED = os.getenv("PAID_AI_ALLOWED", "0") == "1"
PDF_SIGNING_P12_PATH = os.getenv("PDF_SIGNING_P12_PATH", "")
PDF_SIGNING_P12_PASSWORD = os.getenv("PDF_SIGNING_P12_PASSWORD", "")
EMAIL_DELIVERY_ENABLED = os.getenv("EMAIL_DELIVERY_ENABLED", "0") == "1"
EMAIL_BACKEND = os.getenv("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.getenv("SMTP_HOST", "")
EMAIL_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_USE_TLS = os.getenv("SMTP_USE_TLS", "1") == "1"
EMAIL_HOST_USER = os.getenv("SMTP_USERNAME", "")
EMAIL_HOST_PASSWORD = os.getenv("SMTP_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", "")
EMAIL_TIMEOUT = 20
LOCAL_AI_TIMEOUT = int(os.getenv("LOCAL_AI_TIMEOUT", "300"))
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
LOCAL_LEGACY_ACCESS = DEBUG  # Existing ungrouped accounts only in the local demo.
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise RuntimeError("Set DJANGO_SECRET_KEY or explicitly enable local DJANGO_DEBUG=1")
    SECRET_KEY = "local-development-only-not-for-deployment"
if not DEBUG and (len(SECRET_KEY) < 50 or SECRET_KEY.startswith("replace-with-")):
    raise RuntimeError("Set DJANGO_SECRET_KEY to a unique random value of at least 50 characters.")
AUDIT_CHAIN_KEY = os.getenv("AUDIT_CHAIN_KEY") or (SECRET_KEY if DEBUG else "")
if not DEBUG and (
    len(AUDIT_CHAIN_KEY) < 50
    or AUDIT_CHAIN_KEY.startswith("replace-with-")
    or AUDIT_CHAIN_KEY == SECRET_KEY
):
    raise RuntimeError(
        "Set AUDIT_CHAIN_KEY to an independent random value of at least 50 characters."
    )
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver").split(",")
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "axes",
    "django_q",
    "csp",
    "lab",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "lab.security_middleware.PrivilegedIdleTimeoutMiddleware",
    "lab.security_middleware.PrivilegedMFAMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "csp.middleware.CSPMiddleware",
    "axes.middleware.AxesMiddleware",
]
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)
AXES_LOCKOUT_PARAMETERS = ["username", ["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "lab.roles.role_context",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
if os.getenv("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "vectorlab"),
            "USER": os.getenv("POSTGRES_USER", "vectorlab"),
            "PASSWORD": os.environ["POSTGRES_PASSWORD"],
            "HOST": os.environ["POSTGRES_HOST"],
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("VECTORLAB_SQLITE_PATH", BASE_DIR / "local.sqlite3"),
            "OPTIONS": {"timeout": 30},
        }
    }
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = Path(os.getenv("PRIVATE_STORAGE", BASE_DIR / "private"))
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
MFA_ENFORCED = not DEBUG or os.getenv("VECTORLAB_FORCE_MFA", "0") == "1"
PRIVILEGED_IDLE_SECONDS = int(os.getenv("PRIVILEGED_IDLE_SECONDS", "900"))
OTP_TOTP_ISSUER = "CPRI VectorLab"
PUBLIC_VERIFY_BASE_URL = (
    os.getenv("VECTORLAB_PUBLIC_BASE_URL")
    or os.getenv("PUBLIC_VERIFY_BASE_URL")
    or "http://127.0.0.1:8000"
).rstrip("/")
LOGOUT_REDIRECT_URL = "/accounts/login/"
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 22 * 1024 * 1024
PDF_PARSE_TIMEOUT = int(os.getenv("PDF_PARSE_TIMEOUT", "15"))
PDF_RENDER_TIMEOUT = int(os.getenv("PDF_RENDER_TIMEOUT", "20"))
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
# Set HSTS only after the complete public endpoint is verified as HTTPS.
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", "0" if DEBUG else "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
if os.getenv("DJANGO_TRUST_PROXY_HTTPS", "0") == "1":
    # Enable only when a trusted reverse proxy strips client-supplied
    # X-Forwarded-Proto and sets its own value.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CONTENT_SECURITY_POLICY = {
    "DIRECTIVES": {
        "default-src": [SELF],
        "script-src": [SELF],
        "style-src": [SELF],
        "img-src": [SELF, "data:"],
        "font-src": [SELF],
        "connect-src": [SELF],
        "object-src": [NONE],
        "base-uri": [SELF],
        "frame-ancestors": [NONE],
        "form-action": [SELF],
    }
}
AI_JOB_TIMEOUT = int(
    os.getenv("AI_JOB_TIMEOUT", "3600" if EXTRACTION_PROVIDER == "local_qwen" else "600")
)
Q_CLUSTER = {
    "name": "vectorlab",
    "workers": 1,
    "timeout": AI_JOB_TIMEOUT,
    "retry": AI_JOB_TIMEOUT + 60,
    "queue_limit": 2,
    "bulk": 1,
    "orm": "default",
    "max_attempts": 2,
    "sync": os.getenv("Q_SYNC", "0") == "1",
}
