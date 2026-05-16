from django.apps import AppConfig

class SittingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sitting"

    def ready(self):
        pass
