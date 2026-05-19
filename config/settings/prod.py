from .base import *  # noqa
from django.core.exceptions import ImproperlyConfigured

DEBUG = False

# Audience verification is mandatory in production. An empty list disables
# verification and breaks the per-system isolation guarantee.
if KEYCLOAK_ENABLED and not KEYCLOAK_VALID_AUDIENCES:
    raise ImproperlyConfigured(
        "KEYCLOAK_VALID_AUDIENCES must be set in production when "
        "KEYCLOAK_ENABLED=True. NBES requires its tokens to carry "
        "aud=nbes-api (or whatever NBES_CLIENT_ID is set to)."
    )
if KEYCLOAK_ENABLED and NBES_CLIENT_ID not in KEYCLOAK_VALID_AUDIENCES:
    raise ImproperlyConfigured(
        f"NBES_CLIENT_ID={NBES_CLIENT_ID!r} is not in "
        f"KEYCLOAK_VALID_AUDIENCES={KEYCLOAK_VALID_AUDIENCES!r}. "
        "NBES will reject every token from itself."
    )

# Security hardening for production
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# WhiteNoise compression for static files
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Production email — configure via env vars
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
