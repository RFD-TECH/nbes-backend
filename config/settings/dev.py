from .base import *  # noqa

DEBUG = True

# Use console email backend in dev — emails print to terminal
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Allow all hosts in dev
ALLOWED_HOSTS = ["*"]
