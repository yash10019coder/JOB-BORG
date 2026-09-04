"""System checks for apps.auto_apply."""
from django.conf import settings
from django.core.checks import Error, register

_SUBMIT_HARD_KILL_SECONDS = 900


@register()
def check_auto_apply_sending_timeout_ordering(app_configs, **kwargs):
    """Assert that AUTO_APPLY_SENDING_TIMEOUT_SECONDS is less than hard kill time_limit."""
    sending_timeout = getattr(settings, "AUTO_APPLY_SENDING_TIMEOUT_SECONDS", 600)
    errors = []

    if sending_timeout >= _SUBMIT_HARD_KILL_SECONDS:
        errors.append(
            Error(
                f"AUTO_APPLY_SENDING_TIMEOUT_SECONDS ({sending_timeout}s) must be "
                f"strictly less than Celery hard time limit ({_SUBMIT_HARD_KILL_SECONDS}s).",
                id="auto_apply.E001",
            )
        )
    return errors
