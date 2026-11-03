from django.apps import AppConfig


class TenantiqAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "app"

    def ready(self) -> None:
        # Registers the system checks (#55); `manage.py check` and `migrate` run them.
        from app import checks  # noqa: F401
