from django.apps import AppConfig


class KielSyncConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "kielsync.django"
    label = "kielsync"
    verbose_name = "KielSync"
