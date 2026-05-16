from django.apps import AppConfig

class MarkingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.marking"

    def ready(self):
        pass
