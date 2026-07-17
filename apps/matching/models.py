"""UserJobMatch — the per-user/per-job join table (the multi-user fix).

Match score/status live here, never on the shared Job row: a job's fit is a
relationship to a specific profile, not a property of the job.
"""
from django.conf import settings
from django.db import models

from .constants import MatchStatus


class UserJobMatch(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="job_matches",
    )
    job = models.ForeignKey(
        "jobs.Job",
        on_delete=models.CASCADE,
        related_name="user_matches",
    )

    match_score = models.FloatField()
    match_status = models.CharField(
        max_length=20,
        choices=MatchStatus.CHOICES,
        default=MatchStatus.BELOW_THRESHOLD,
    )
    # The specific tags that drove the score — the explanation shown in the UI.
    matched_tags = models.JSONField(default=list, blank=True)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "job"], name="uniq_userjobmatch_user_job"
            ),
        ]
        indexes = [
            # Ranked retrieval: a user's matches, highest score first.
            models.Index(fields=["user", "-match_score"], name="ujm_user_score_idx"),
        ]

    def __str__(self):
        return f"Match<{self.user_id}:{self.job_id}={self.match_score}>"
