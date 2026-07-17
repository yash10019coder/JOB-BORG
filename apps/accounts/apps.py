from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounts"

    def ready(self):
        # Register signal handlers (auto-create Profile on User creation).
        from . import signals  # noqa: F401
