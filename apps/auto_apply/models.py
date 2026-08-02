"""auto_apply core models -- ExplicitAnswer (R9) and AutoApplyDraft (R7).

`AutoApplyDraft` is deliberately a separate model rather than a new
`JobApplication.Status` value: `JobApplication`'s existing three-state enum
(Saved/Applied/Dismissed) stays untouched, and a draft only ever produces a
`JobApplication` transition on a successful send (U7), never before.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q


class ExplicitAnswer(models.Model):
    """A user's small, reusable set of auto-apply answers (R9) -- separate
    from the full Phase 5 `answers_bank`. One row per (user, category).
    """

    class Category(models.TextChoices):
        WORK_AUTHORIZATION = "work_authorization", "Work authorization"
        SPONSORSHIP = "sponsorship", "Sponsorship"
        SALARY_EXPECTATION = "salary_expectation", "Salary expectation"
        OTHER = "other", "Other"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="explicit_answers",
    )
    category = models.CharField(max_length=32, choices=Category.choices)
    answer_text = models.TextField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "category"],
                name="uniq_explicitanswer_user_category",
            ),
        ]

    def __str__(self):
        return f"ExplicitAnswer<{self.user_id}:{self.category}>"


class AutoApplyDraft(models.Model):
    """A drafted (and eventually sent) auto-apply attempt for one
    (user, job) pair. Non-terminal statuses (DRAFTED, SENDING) are
    conditionally unique per (user, job) -- see Meta.constraints -- so a
    terminal outcome (FAILED/STALE/EXCLUDED) never permanently blocks a
    fresh retry for the same job.
    """

    class Status(models.TextChoices):
        DRAFTED = "drafted", "Drafted"
        STALE = "stale", "Stale"
        SENDING = "sending", "Sending"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"
        EXCLUDED = "excluded", "Excluded"

    # Non-terminal statuses that block a concurrent duplicate draft for the
    # same (user, job) -- see uniq_autoapplydraft_user_job_active below.
    ACTIVE_STATUSES = (Status.DRAFTED, Status.SENDING)

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="auto_apply_drafts",
    )
    job = models.ForeignKey(
        "jobs.Job",
        on_delete=models.CASCADE,
        related_name="auto_apply_drafts",
    )
    job_application = models.ForeignKey(
        "applications.JobApplication",
        on_delete=models.PROTECT,
        related_name="auto_apply_drafts",
        null=True,
        blank=True,
        help_text=(
            "Set only once a send succeeds and links to the real "
            "JobApplication record; PROTECT so a completed auto-apply's "
            "link to its submission never silently cascade-deletes."
        ),
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.DRAFTED
    )
    answers = models.JSONField(
        default=dict,
        blank=True,
        help_text="Resolved per-question answer set, including confidence/needs_review metadata.",
    )
    answers_schema_version = models.IntegerField(default=1)
    exclusion_reason = models.TextField(
        null=True,
        blank=True,
        help_text="Populated when status=EXCLUDED (R6).",
    )
    error_message = models.TextField(
        null=True,
        blank=True,
        help_text="Populated when status=FAILED (R14).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Scoped to non-terminal statuses only: a FAILED/STALE/EXCLUDED
            # draft must never permanently block a fresh retry for the same
            # (user, job) -- only one DRAFTED/SENDING draft may exist at a
            # time per (user, job).
            models.UniqueConstraint(
                fields=["user", "job"],
                condition=Q(status__in=["drafted", "sending"]),
                name="uniq_autoapplydraft_user_job_active",
            ),
        ]

    def __str__(self):
        return f"AutoApplyDraft<{self.user_id}:{self.job_id}={self.status}>"
