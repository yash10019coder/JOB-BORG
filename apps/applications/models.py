"""JobApplication — a user's action on a job (saved / applied / dismissed).

Per-user and independent of matching: a UserJobMatch disappearing (profile
narrowed, job closed) never touches this record, and vice versa.
"""
from django.conf import settings
from django.db import models


class JobApplication(models.Model):
    class Status(models.TextChoices):
        SAVED = "saved", "Saved"
        APPLIED = "applied", "Applied"
        DISMISSED = "dismissed", "Dismissed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_applications",
    )
    job = models.ForeignKey(
        "jobs.Job",
        on_delete=models.CASCADE,
        related_name="applications",
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.SAVED
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "job"], name="uniq_jobapplication_user_job"
            ),
        ]

    def __str__(self):
        return f"Application<{self.user_id}:{self.job_id}={self.status}>"
