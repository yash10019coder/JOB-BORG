"""Profile post-save -> debounced rematch, so recommendations refresh on edit."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.accounts.models import Profile

from .services import MATCHING_PROFILE_FIELDS
from .tasks import schedule_rematch


@receiver(post_save, sender=Profile, dispatch_uid="profile_rematch")
def rematch_on_profile_save(sender, instance, update_fields=None, **kwargs):
    # A save with explicit `update_fields` that touches no matching input
    # (e.g. an Answers-tab save, a provenance stamp, resume text) cannot
    # change any match. A full save (`update_fields=None`) always rematches.
    if update_fields is not None and not MATCHING_PROFILE_FIELDS & set(update_fields):
        return
    schedule_rematch(instance.pk)
