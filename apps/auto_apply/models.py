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

    class ReasonCode(models.TextChoices):
        """Machine-readable classification of why a draft is EXCLUDED/FAILED.

        `exclusion_reason`/`error_message` remain free text for logs/
        debugging, but UI code (see `apps/web/views.py`'s
        `_friendly_draft_message`) should switch on this code, not
        substring-match the free text -- the two would otherwise have to
        stay in sync by hand across modules with no test/type enforcing it.
        """

        SCHEMA_MISMATCH = "schema_mismatch", "Unsupported form field"
        FORM_LOAD_FAILED = "form_load_failed", "Could not load application form"
        # Narrowed: `drafting.draft_for()` only produces this for a blank
        # *standard* (Profile-derived) required field -- an incomplete
        # Profile. A required custom question the LLM couldn't answer no
        # longer excludes the draft; it becomes a blank needs_review
        # placeholder in `answers` for a human to fill in via the review
        # queue instead (`send_auto_apply_draft` blocks sending until it's
        # filled). This code stays reachable for the Profile-incompleteness
        # case and any future terminal-exclusion use.
        UNANSWERABLE_REQUIRED = "unanswerable_required", "Required question unanswered"
        CAPTCHA_CHALLENGED = "captcha_challenged", "Bot-detection challenge"
        SUBMISSION_FAILED = "submission_failed", "Submission rejected"
        SENDING_TIMEOUT = "sending_timeout", "Submission timed out"
        UNEXPECTED_ERROR = "unexpected_error", "Unexpected error"
        NO_INBOX_CREDENTIALS = "no_inbox_credentials", "No inbox credentials connected"
        VERIFICATION_CODE_TIMEOUT = "verification_code_timeout", "Verification email timed out"
        INBOX_AUTH_FAILED = "inbox_auth_failed", "Inbox authentication failed"
        INBOX_UNAVAILABLE = "inbox_unavailable", "Inbox connection unavailable"
        VERIFICATION_CODE_AMBIGUOUS = "verification_code_ambiguous", "Multiple verification codes found"
        VERIFICATION_CODE_REJECTED = "verification_code_rejected", "Verification code rejected"


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
    form_schema_snapshot = models.JSONField(
        null=True,
        blank=True,
        help_text=(
            "The Greenhouse application-page FormSchema (see "
            "apps.auto_apply.greenhouse_form.field_mapping) captured at "
            "draft time. Passed back to GreenhouseFormClient.submit() as "
            "expected_schema at send time so the schema-drift check can "
            "actually run -- without this, drift detection has no "
            "baseline to compare the live page against."
        ),
    )
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
    reason_code = models.CharField(
        max_length=32,
        choices=ReasonCode.choices,
        null=True,
        blank=True,
        help_text=(
            "Machine-readable reason for EXCLUDED/FAILED, set alongside "
            "exclusion_reason/error_message. UI code should switch on this, "
            "not the free-text message."
        ),
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
