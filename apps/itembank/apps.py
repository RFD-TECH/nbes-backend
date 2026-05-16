from django.apps import AppConfig

class ItemBankConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.itembank"

    def ready(self):
        pass
