"""Profile post-save -> debounced rematch, so recommendations refresh on edit."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.accounts.models import Profile

from .tasks import schedule_rematch


@receiver(post_save, sender=Profile, dispatch_uid="profile_rematch")
def rematch_on_profile_save(sender, instance, **kwargs):
    schedule_rematch(instance.pk)
