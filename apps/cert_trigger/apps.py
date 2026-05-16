from django.apps import AppConfig

class CertTriggerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.cert_trigger"

    def ready(self):
        pass
