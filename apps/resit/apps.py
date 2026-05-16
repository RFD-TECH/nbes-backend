from django.apps import AppConfig

class ResitConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.resit"

    def ready(self):
        pass
