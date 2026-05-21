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
    "shared.apps.SharedConfig",
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
    "apps.dashboards",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "shared.middleware.JsonExceptionMiddleware",
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
    # Enforces Idempotency-Key on state-mutating API calls. Must run after
    # AuditMiddleware so cache keys can scope on the request_id-derived
    # correlation surface; runs before DRF auth so anonymous retries also
    # dedupe.
    "shared.middleware.IdempotencyKeyMiddleware",
    # Edge throttle and 24h IP block. Counts rejected (401/403/429)
    # responses. Runs near the top of the chain so blocks short-circuit
    # before auth/DB work happens.
    "shared.middleware.EdgeRateLimitMiddleware",
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
        "HOST": os.environ.get("NBES_DBHOST", os.environ.get("DBHOST", "")),
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

SPECTACULAR_SETTINGS = {
    "TITLE": "National Bar Examination System API",
    "DESCRIPTION": (
        "REST API for NBES System 10A. Protected endpoints use Bearer JWTs "
        "issued by IAM/Keycloak in production or HS256 development tokens in local mode."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    "TAGS": [
        {"name": "RBAC Admin", "description": "Manage NBES role and permission mapping."},
        {"name": "Current User", "description": "Inspect the current user's NBES permissions and dashboard."},
        {"name": "Audit", "description": "Search and verify the append-only audit trail (Auditor / Administrator only)."},
        {"name": "NBEC Committee", "description": "NBEC member register, meetings, agendas, minutes, and conflict-of-interest declarations (Phase 2 — NBE-F01)."},
    ],
}

# ── CORS ──────────────────────────────────────────────────────────────────────
CORS_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS", "http://localhost:3000"
).split(",")

# ── Celery ────────────────────────────────────────────────────────────────────
REDIS_URL = os.environ.get(
    "NBES_REDIS_URL",
    os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
)
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
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

# NBES's own backend client_id. Tokens for NBES must list this in `aud`,
# and NBES reads its system roles from resource_access[NBES_CLIENT_ID].roles.
NBES_CLIENT_ID = os.environ.get("NBES_CLIENT_ID", "nbes-api")

# Service-account credentials used by shared/keycloak_admin.py to call the
# Keycloak Admin API (e.g. revoking roles on tenure expiry). The client must
# have the realm-management "manage-users" and "manage-realm" service roles.
KEYCLOAK_ADMIN_CLIENT_ID = os.environ.get(
    "KEYCLOAK_ADMIN_CLIENT_ID",
    os.environ.get("KEYCLOAK_CLIENT_ID_INTERNAL", ""),
)
KEYCLOAK_ADMIN_CLIENT_SECRET = os.environ.get(
    "KEYCLOAK_ADMIN_CLIENT_SECRET",
    os.environ.get("KEYCLOAK_CLIENT_SECRET_INTERNAL", ""),
)
KEYCLOAK_VALID_AUDIENCES = [
    value.strip()
    for value in os.environ.get(
        "KEYCLOAK_VALID_AUDIENCES", NBES_CLIENT_ID
    ).split(",")
    if value.strip()
]

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
# HMAC secret shared with System 17 for signed inter-system calls. Required
# whenever KAFKA_ENABLED=True (i.e. the outbox actually publishes off-box).
SYSTEM_17_HMAC_SECRET = os.environ.get("SYSTEM_17_HMAC_SECRET", "")
SYSTEM_17_TIMEOUT_SECONDS = float(os.environ.get("SYSTEM_17_TIMEOUT_SECONDS", "5"))
SYSTEM_17_NONCE_WINDOW_SECONDS = int(
    os.environ.get("SYSTEM_17_NONCE_WINDOW_SECONDS", "300")
)

# Idempotency cache — 24h default.
IDEMPOTENCY_CACHE_TTL_SECONDS = int(
    os.environ.get("IDEMPOTENCY_CACHE_TTL_SECONDS", "86400")
)

# Edge throttle thresholds — used by shared.middleware.EdgeRateLimitMiddleware.
# Blueprint §1.2.6 / F000-06.
EDGE_THROTTLE_THRESHOLD = int(os.environ.get("EDGE_THROTTLE_THRESHOLD", "100"))
EDGE_BLOCK_THRESHOLD_24H = int(os.environ.get("EDGE_BLOCK_THRESHOLD_24H", "1000"))
EDGE_SECURITY_EVENT_RETENTION_DAYS = int(
    os.environ.get("EDGE_SECURITY_EVENT_RETENTION_DAYS", "90")
)
SYSTEM_20_WEBHOOK_SECRET = os.environ.get("SYSTEM_20_WEBHOOK_SECRET", "")
SYSTEM_21_URL = os.environ.get("SYSTEM_21_URL", "")
SYSTEM_21_API_KEY = os.environ.get("SYSTEM_21_API_KEY", "")
NLEMS_URL = os.environ.get("NLEMS_URL", "")
NLEMS_API_KEY = os.environ.get("NLEMS_API_KEY", "")
