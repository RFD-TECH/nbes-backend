import os
import environ
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = os.environ["SECRET_KEY"]

ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost").split(",")

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
    "django_celery_beat",
    "django_fsm",
    # "viewflow",  # Uncomment when Board ratification flows are implemented
]

LOCAL_APPS = [
    "shared",
    "apps.users",
    "apps.committee",
    "apps.itembank",
    "apps.sitting",
    "apps.registration",
    "apps.marking",
    "apps.results",
    "apps.resit",
    "apps.cert_trigger",
    "apps.notifications",
    "apps.audit",
    "apps.sla",
    "apps.reporting",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Injects X-Request-ID and captures IP/user-agent for audit events.
    # See shared/middleware.py
    "shared.middleware.AuditMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# ── Database — configured per environment via env vars ───────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends." + os.environ.get("DBENGINE", "sqlite3"),
        "NAME": os.environ.get("DBNAME", BASE_DIR / "db.sqlite3"),
        "USER": os.environ.get("DBUSER", ""),
        "PASSWORD": os.environ.get("DBPASSWORD", ""),
        "HOST": os.environ.get("DBHOST", ""),
        "PORT": os.environ.get("DBPORT", ""),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "Africa/Accra")
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Static and media ─────────────────────────────────────────────────────────
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# ── DRF ──────────────────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    # JWT authentication — delegates to Keycloak in prod, shared-secret JWT in dev.
    # See shared/auth.py → KeycloakJWTAuthentication
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "shared.auth.KeycloakJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    # Maps TransitionNotAllowed → 400 TRANSITION_NOT_ALLOWED.
    # Wraps all responses in standard envelope. See shared/exceptions.py
    "EXCEPTION_HANDLER": "shared.exceptions.nbes_exception_handler",
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "shared.pagination.StandardResultsPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# ── CORS ──────────────────────────────────────────────────────────────────────
CORS_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS", "http://localhost:3000"
).split(",")

# ── Cache (Redis) ────────────────────────────────────────────────────────────
# Used by:
#   - IP-level brute-force throttle counters (apps/users/throttle.py)
#   - Future: role/permission cache (60s TTL per SRS §1.2.5)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ.get("REDIS_URL", "redis://localhost:6379/1"),
    }
}

# ── Auth / Session lifetimes ─────────────────────────────────────────────────
ACCESS_TOKEN_LIFETIME_MINUTES = int(os.environ.get("ACCESS_TOKEN_LIFETIME_MINUTES", "15"))
REFRESH_TOKEN_LIFETIME_DAYS = int(os.environ.get("REFRESH_TOKEN_LIFETIME_DAYS", "7"))
INVITE_TOKEN_LIFETIME_DAYS = int(os.environ.get("INVITE_TOKEN_LIFETIME_DAYS", "7"))

# SRS §1.2.3 — account lockout after N consecutive failed logins
MAX_FAILED_LOGINS = int(os.environ.get("MAX_FAILED_LOGINS", "5"))
ACCOUNT_LOCKOUT_MINUTES = int(os.environ.get("ACCOUNT_LOCKOUT_MINUTES", "15"))

# SRS §1.2.6 — IP-level brute-force defence
IP_THROTTLE_FAILS_PER_MINUTE = int(os.environ.get("IP_THROTTLE_FAILS_PER_MINUTE", "100"))
IP_THROTTLE_FAILS_PER_DAY = int(os.environ.get("IP_THROTTLE_FAILS_PER_DAY", "1000"))
IP_THROTTLE_MINUTES = int(os.environ.get("IP_THROTTLE_MINUTES", "15"))
IP_BLOCK_HOURS = int(os.environ.get("IP_BLOCK_HOURS", "24"))

# ── Celery ────────────────────────────────────────────────────────────────────
CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Named queues — see architecture doc §6.1
CELERY_TASK_QUEUES = {
    "marking-high": {},      # AI scoring, audit hash — exam-critical
    "moderation": {},        # Borderline routing, reconciliation
    "results": {},           # Normalisation, hash verification, PDF generation
    "cert-trigger": {},      # System 14 webhook — 1-hour SLA
    "notifications": {},     # System 21 dispatch
    "sla-monitor": {},       # SLA checking — runs every 15 minutes
    "vault-integrity": {},   # Daily vault SHA-256 integrity check
    "outbox": {},            # Outbox poller — runs every 5 seconds
}

# ── Kafka ─────────────────────────────────────────────────────────────────────
KAFKA_ENABLED = os.environ.get("KAFKA_ENABLED", "False") == "True"
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

# ── Keycloak / JWT ────────────────────────────────────────────────────────────
KEYCLOAK_ENABLED = os.environ.get("KEYCLOAK_ENABLED", "False") == "True"
KEYCLOAK_REALM_URL = os.environ.get("KEYCLOAK_REALM_URL", "")
# In dev (KEYCLOAK_ENABLED=False), this shared secret is used to validate JWTs.
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", SECRET_KEY)
JWT_ALGORITHM = "HS256"

# ── Vault ─────────────────────────────────────────────────────────────────────
VAULT_DEV_MODE = os.environ.get("VAULT_DEV_MODE", "True") == "True"
PKCS11_LIB_PATH = os.environ.get("PKCS11_LIB_PATH", "")
HSM_TOKEN_LABEL = os.environ.get("HSM_TOKEN_LABEL", "nbes-vault")
HSM_PIN = os.environ.get("HSM_PIN", "")

# ── MinIO / Object Storage ────────────────────────────────────────────────────
MINIO_ENABLED = os.environ.get("MINIO_ENABLED", "False") == "True"
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "")
MINIO_BUCKET_NAME = os.environ.get("MINIO_BUCKET_NAME", "nbes-bucket")

# ── External Systems ──────────────────────────────────────────────────────────
SYSTEM_17_URL = os.environ.get("SYSTEM_17_URL", "")
SYSTEM_17_API_KEY = os.environ.get("SYSTEM_17_API_KEY", "")
SYSTEM_20_WEBHOOK_SECRET = os.environ.get("SYSTEM_20_WEBHOOK_SECRET", "")
SYSTEM_21_URL = os.environ.get("SYSTEM_21_URL", "")
SYSTEM_21_API_KEY = os.environ.get("SYSTEM_21_API_KEY", "")
NLEMS_URL = os.environ.get("NLEMS_URL", "")
NLEMS_API_KEY = os.environ.get("NLEMS_API_KEY", "")

# ── API docs ──────────────────────────────────────────────────────────────────
SPECTACULAR_SETTINGS = {
    "TITLE": "National Bar Examination System API",
    "DESCRIPTION": (
        "NBES Core Platform — System 10A. "
        "See SYSTEM_ARCHITECTURE.md for full domain documentation."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
