"""Per-user Profile — matching criteria + who-you-are fields.

The built-in Django User is the auth/users table (see Key Decisions); Profile
is a OneToOne extension holding everything the matching fan-out reads.
"""
import os
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models.fields.files import FieldFile
from django.utils import timezone

from .crypto import encrypt_secret


def resume_upload_path(instance, filename):
    """Per-user resume path -- keeps uploads from colliding across users
    regardless of which storage backend (local/S3) is active."""
    extension = os.path.splitext(filename)[1].lower()
    return f"resumes/{instance.user_id}/{uuid.uuid4().hex}{extension}"


def validate_resume_file(file):
    """Enforce the resume upload allowlist (U1) before the file is ever
    handed to the parsing task -- a storage/DoS control and a first line of
    defense against malicious uploads, independent of the parsing task's own
    bounded time limit (see apps/accounts/tasks.py).
    """
    if isinstance(file, FieldFile) and file._committed:
        return

    max_size = getattr(settings, "RESUME_MAX_UPLOAD_SIZE_BYTES", 10 * 1024 * 1024)
    if file.size > max_size:
        raise ValidationError(
            f"Resume file is too large ({file.size} bytes); max is {max_size} bytes."
        )

    allowed_extensions = getattr(
        settings, "RESUME_ALLOWED_EXTENSIONS", [".pdf", ".docx", ".txt"]
    )
    ext = os.path.splitext(file.name or "")[1].lower()
    if ext not in allowed_extensions:
        raise ValidationError(
            f"Unsupported resume file type '{ext}'. "
            f"Allowed types: {', '.join(allowed_extensions)}."
        )

    # Only in-flight uploads (UploadedFile) carry a browser-reported
    # content_type; a FieldFile re-validated from storage does not, so this
    # check is best-effort on top of the extension check above, not a
    # replacement for it. A FieldFile doesn't proxy arbitrary attributes to
    # its wrapped file, so check both `file.content_type` (raw UploadedFile)
    # and `file.file.content_type` (FieldFile wrapping an uncommitted
    # UploadedFile, as with a freshly-assigned `profile.resume`).
    content_type = getattr(file, "content_type", None)
    if content_type is None:
        content_type = getattr(getattr(file, "file", None), "content_type", None)
    allowed_content_types = getattr(
        settings,
        "RESUME_ALLOWED_CONTENT_TYPES",
        [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain",
        ],
    )
    if content_type not in (None, "application/octet-stream") and content_type not in allowed_content_types:
        raise ValidationError(f"Unsupported resume content type '{content_type}'.")


# Profile.VisaStatus value -> (authorized, needs_sponsorship), as consumed by
# `Profile.authorization_for_country`. Each element is True / False, or None
# meaning *deliberately unknown* -- which is the point of this table: a US
# visa status tells us the applicant is authorized in the US but says nothing
# reliable about whether they will *require sponsorship* (someone extending
# from F-1 onto a new H-1B did; a green card holder did not). Inferring one
# from the other would be a guess, so the answer stays unknown and the draft
# leaves the field blank + needs_review -- the same "a confidently wrong
# resolution is worse than an unresolved one" rule `apps.locations.engine`
# follows. Mapping these onto a live form's option labels is the job of
# `apps.accounts.question_semantics`, not this table.
_AUTHORIZATION_BY_VISA_STATUS: dict[str, tuple[bool | None, bool | None]] = {
    "citizen": (True, False),
    "permanent_resident": (True, False),
    "work_permit": (True, False),
    # Needs sponsorship, but authorization is unknown: H-1B transferees also
    # choose this, and they are authorized to work for their current employer.
    "requires_sponsorship": (None, True),
    # Not authorized says nothing about whether sponsorship would be needed.
    "not_authorized": (False, None),
    # US/AU statuses: authorized yes, sponsorship unknown -- see above.
    "h1b": (True, None),
    "opt": (True, None),
    "o1": (True, None),
    "tn": (True, None),
    "e3": (True, None),
    "other": (None, None),
}


class Profile(models.Model):
    class RemotePref(models.TextChoices):
        ANY = "any", "Any"
        REMOTE_ONLY = "remote_only", "Remote only"
        ONSITE_ONLY = "onsite_only", "On-site only"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )

    # Who-you-are.
    full_name = models.CharField(max_length=255, blank=True, default="")
    headline = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    linkedin_url = models.URLField(max_length=255, blank=True, default="")
    # Standard-field sources for auto-apply drafting (see
    # apps.auto_apply.services.drafting._STANDARD_FIELD_PATTERNS), same role
    # as linkedin_url above -- collected once here instead of the LLM
    # guessing (or leaving blank) every "GitHub"/"Website"/"Current Company"
    # question on every application.
    github_url = models.URLField(max_length=255, blank=True, default="")
    portfolio_url = models.URLField(max_length=255, blank=True, default="")
    current_employer = models.CharField(max_length=255, blank=True, default="")

    # Where the applicant lives and works, for "Country" / "City" / "Address" /
    # "Time zone" questions. These are form-filling facts, not matching
    # criteria (`target_locations` is that), so matching never reads them.
    # `location_country` is ISO 3166-1 alpha-3, like every other country on
    # this model; `working_timezone` is an IANA name ("Asia/Kolkata").
    location_city = models.CharField(max_length=255, blank=True, default="")
    location_country = models.CharField(max_length=3, blank=True, default="")
    mailing_address = models.TextField(blank=True, default="")
    working_timezone = models.CharField(max_length=64, blank=True, default="")

    # Resume -- standard-field source for auto-apply drafting (see
    # docs/plans/2026-08-02-001-feat-auto-apply-greenhouse-slice-plan.md U1).
    # `resume_text` is populated asynchronously by the `parse_resume` Celery
    # task (apps/accounts/tasks.py), explicitly enqueued from
    # `Profile.set_resume()` -- deliberately not a post_save signal, so the
    # trigger is visible at the call site rather than implicit. It stays
    # empty until parsing completes, or forever if no resume is uploaded or
    # nothing was extractable; downstream consumers must treat empty as
    # "no resume text available", not an error.
    resume = models.FileField(
        upload_to=resume_upload_path,
        blank=True,
        null=True,
        validators=[validate_resume_file],
        help_text="PDF/DOCX/TXT only. Parsed into resume_text asynchronously.",
    )
    resume_text = models.TextField(blank=True, default="")

    # Matching criteria.
    target_titles = models.JSONField(default=list, blank=True)
    # Skill/keyword list scored against a job's classification_tags to produce
    # matched_tags — the profile-side counterpart the scorer intersects with.
    target_tags = models.JSONField(default=list, blank=True)
    target_locations = models.JSONField(default=list, blank=True)
    # Structured mirror of target_locations, one entry per raw string, each
    # shaped {"raw": str, "city": str|None, "region": str|None,
    # "country": str|None, "resolved": bool} — computed by ProfileForm via
    # apps.locations.engine.normalize_location whenever target_locations
    # changes. target_locations itself stays untouched (raw, user-typed) so
    # the CSV form field round-trips exactly what the user entered.
    target_locations_normalized = models.JSONField(default=list, blank=True)
    target_locations_alias_version = models.CharField(
        max_length=32, blank=True, default="", db_index=True
    )
    excluded_employers = models.JSONField(
        default=list,
        blank=True,
        help_text="Employer slugs to exclude from recommendations.",
    )
    min_salary = models.IntegerField(null=True, blank=True)

    # Auto-apply explicit answers (work auth, sponsorship, salary by region).
    #
    # Authorization is inherently per-country: "authorized to work here" is
    # meaningless without knowing *where* "here" is, and a single flat
    # `visa_status` column silently answered that question with US-centric
    # values (H-1B/OPT/E-3) for jobs in every country. These two fields are
    # keyed by country for that reason:
    #   visa_status_by_country: {"IND": "citizen", "DEU": "requires_sponsorship"}
    #   citizenship_countries:  ["IND"]  (dual citizens list several)
    #
    # Keys are ISO 3166-1 alpha-3 (see apps.accounts.regions for why alpha-3 and
    # not alpha-2/the engine's canonical name).
    class VisaStatus(models.TextChoices):
        # Country-agnostic, and the only statuses a non-US applicant should
        # normally need: they are what most countries' forms actually offer.
        CITIZEN = "citizen", "Citizen"
        PERMANENT_RESIDENT = "permanent_resident", "Permanent resident / Green card"
        WORK_PERMIT = "work_permit", "Work permit held (no sponsorship needed)"
        REQUIRES_SPONSORSHIP = "requires_sponsorship", "Will require visa sponsorship"
        NOT_AUTHORIZED = "not_authorized", "Not authorized to work"
        # US-specific, with the country named in the label so the value is
        # never ambiguous about where it applies.
        H1B = "h1b", "H-1B (US)"
        OPT = "opt", "OPT or CPT (US)"
        O1 = "o1", "O-1 (US)"
        TN = "tn", "TN (US)"
        E3 = "e3", "E-3 (Australia)"
        OTHER = "other", "Other / not listed"

    visa_status_by_country = models.JSONField(
        default=dict,
        blank=True,
        help_text="Per-country work authorization, e.g. {\"IND\": \"citizen\"}.",
    )
    citizenship_countries = models.JSONField(
        default=list,
        blank=True,
        help_text='ISO alpha-3 codes of countries you are a citizen of.',
    )
    # Multi-region salary expectations: {"US": "150-200k", "EU": "100-150k", "IN": "30-50L"}
    salary_by_region = models.JSONField(default=dict, blank=True)
    preferred_currency = models.CharField(
        max_length=3,
        choices=[
            ("USD", "USD ($)"),
            ("EUR", "EUR (€)"),
            ("GBP", "GBP (£)"),
            ("CAD", "CAD (C$)"),
            ("AUD", "AUD (A$)"),
            ("INR", "INR (₹)"),
            ("SGD", "SGD (S$)"),
            ("CHF", "CHF"),
            ("JPY", "JPY (¥)"),
        ],
        default="USD",
    )

    # Who wrote each covered field, so an importer or the learner can never
    # silently overwrite what the user typed. Shape:
    #   {"phone": {"source": "user", "locked": false,
    #              "updated_at": "<iso8601>", "detail": ""}, ...}
    # Per-key entries are used for the dict/list authorization fields, e.g.
    # "visa_status_by_country.USA". A missing entry on a non-empty value means
    # "user" (legacy data). Only `apps.accounts.services.profile_fields`
    # should write this -- see FR7.10 in
    # docs/plans/2026-10-03-2300-consolidated-requirements.md.
    field_provenance = models.JSONField(default=dict, blank=True)

    remote_pref = models.CharField(
        max_length=16,
        choices=RemotePref.choices,
        default=RemotePref.ANY,
    )

    # Gates whether this profile participates in matching fan-out at all.
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def authorization_for_country(self, country):
        """``(authorized, needs_sponsorship)`` for `country`, or ``None`` if
        this profile says nothing about it.

        `country` may be given in any form :func:`apps.accounts.regions.region_for_country`
        accepts (alpha-3, alpha-2, or the locations engine's canonical name),
        because the caller usually has the last of those off a
        ``Job.location_country``.

        Each element is ``True``, ``False`` or ``None`` meaning *deliberately
        unknown* (see ``_AUTHORIZATION_BY_VISA_STATUS``): a per-country visa
        status alone must not produce a sponsorship answer.

        Returns ``None`` -- not ``(None, None)`` -- when the profile has no
        entry for the country at all, so callers can tell "nothing known"
        apart from "known, but deliberately undecided".
        """
        from apps.locations.engine import alpha3_for_country

        alpha3 = alpha3_for_country(country)
        if alpha3 is None:
            return None
        if alpha3 in set(self.citizenship_countries or []):
            # Citizenship is authoritative for its own country: no
            # sponsorship is ever needed at home.
            return (True, False)
        status = (self.visa_status_by_country or {}).get(alpha3)
        if status is None:
            return None
        return _AUTHORIZATION_BY_VISA_STATUS.get(status)

    def set_resume(self, file):
        """Assign an uploaded resume file, or clear it if `file` is falsy,
        and explicitly enqueue async text extraction (skipped on clear).

        This is the one call site every write path to `resume` should go
        through -- add *and* clear -- (the profile-edit view once it exists
        in apps/web, and `ProfileAdmin.save_model` today) so parsing is
        triggered the same way everywhere and there's no second, divergent
        path for clearing. Deliberately not a `post_save` signal, per U1's
        approach, so the trigger stays visible here instead of implicit.
        """
        previous = self.resume
        self.resume = file or None
        self.resume_text = ""
        self.full_clean(validate_unique=False)
        self.save(update_fields=["resume", "resume_text", "updated_at"])

        if previous and previous.name != getattr(self.resume, "name", None):
            self._retire_previous_resume(previous)

        if file:
            from .tasks import parse_resume

            transaction.on_commit(lambda: parse_resume.delay(self.pk))

    def _retire_previous_resume(self, previous):
        """Delete `previous`'s storage object, unless an in-flight
        `AutoApplyDraft` still references it.

        `draft_for` snapshots `profile.resume.name` into `draft.answers` at
        draft time (see apps/auto_apply/services/drafting.py), so a draft
        created before this replacement can still hold the old storage key.
        A `SENDING` draft materializes that key into a local file at send
        time (apps/auto_apply/tasks.py) -- deleting it out from under that
        in-flight send would fail the submission, so deletion is skipped
        entirely this call rather than raced; a `DRAFTED` draft referencing
        the old key is marked `STALE` instead of silently sending a resume
        the user never reviewed against this job.
        """
        from apps.auto_apply.greenhouse_form.field_mapping import FILE
        from apps.auto_apply.models import AutoApplyDraft

        active_drafts = AutoApplyDraft.objects.filter(
            user_id=self.user_id, status__in=AutoApplyDraft.ACTIVE_STATUSES
        )
        referencing = [
            draft
            for draft in active_drafts
            if any(
                entry.get("field_type") == FILE and entry.get("value") == previous.name
                for entry in (draft.answers or {}).values()
            )
        ]
        if any(d.status == AutoApplyDraft.Status.SENDING for d in referencing):
            return

        stale_ids = [
            d.pk for d in referencing if d.status == AutoApplyDraft.Status.DRAFTED
        ]
        if stale_ids:
            AutoApplyDraft.objects.filter(pk__in=stale_ids).update(
                status=AutoApplyDraft.Status.STALE,
                reason_code=AutoApplyDraft.ReasonCode.RESUME_REPLACED,
            )

        transaction.on_commit(lambda: previous.storage.delete(previous.name))

    def __str__(self):
        return f"Profile<{self.user.username}>"


class EmailInboxCredential(models.Model):
    """One per-user IMAP inbox credential, used exclusively to auto-solve
    Greenhouse's post-submit email verification step (see
    docs/plans/2026-08-04-001-feat-auto-apply-greenhouse-email-verification-plan.md).

    The stored app password is the highest-value secret this product holds
    (see the plan's Security Assessment), so this model deliberately has
    exactly two mutators -- `set_app_password()` and `mark_auth_failed()` --
    and no other write path is expected to touch `app_password_encrypted`,
    `is_active`, `last_error_code`, or `last_error_at`.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="email_inbox_credential",
    )

    email_address = models.EmailField()
    imap_host = models.CharField(max_length=255)
    imap_port = models.PositiveIntegerField(default=993)

    # Fernet ciphertext (see apps.accounts.crypto) -- never plaintext, never
    # decrypted implicitly. Fernet ciphertext is non-deterministic (a random
    # IV per encryption), so this column can NEVER be used as a DB lookup key
    # -- no `.filter(app_password_encrypted=...)`/`.get(app_password_encrypted=...)`
    # anywhere, and deliberately no `db_index`.
    app_password_encrypted = models.TextField()

    is_active = models.BooleanField(default=True)

    # A short code (e.g. "inbox_auth_failed"), NOT a message -- IMAP server
    # error text can echo back the username/password attempt, so raw
    # exception text must never be stored here.
    last_error_code = models.CharField(max_length=32, blank=True, default="")
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()

        allowed_hosts = getattr(settings, "AUTO_APPLY_IMAP_ALLOWED_HOSTS", [])
        if self.imap_host not in allowed_hosts:
            raise ValidationError(
                f"IMAP host {self.imap_host!r} is not on the allowed host "
                f"list {sorted(allowed_hosts)}."
            )

        # TLS-only, per the plan's scope boundaries -- 993 is the IMAPS port;
        # nothing else is accepted.
        if self.imap_port != 993:
            raise ValidationError(
                f"IMAP port must be 993 (TLS-only); got {self.imap_port!r}."
            )

    def set_app_password(self, raw_password: str) -> None:
        """Encrypt and store a new app password, reactivating the credential.

        Strips whitespace from `raw_password` first: Google renders Gmail
        app passwords with spaces for readability (`"abcd efgh ijkl mnop"`),
        and every user's first paste attempt fails without stripping them --
        a known real-world gotcha, not speculative.

        This is the one call site every write path to `app_password_encrypted`
        should go through (the credential-connect view once it exists in
        apps/web, mirroring `Profile.set_resume()`'s idiom) -- deliberately
        not a `post_save` signal, so the trigger stays visible here instead
        of implicit.
        """
        stripped = "".join(raw_password.split())
        self.app_password_encrypted = encrypt_secret(stripped)
        self.is_active = True
        self.last_error_code = ""
        self.save(
            update_fields=["app_password_encrypted", "is_active", "last_error_code", "updated_at"]
        )

    def mark_auth_failed(self, error_code: str) -> None:
        """Deactivate the credential after a genuine IMAP auth rejection.

        This is the ONLY method in the whole codebase allowed to set
        `is_active=False` (R7) -- a transient/unreachable IMAP failure must
        NOT call this and must NOT deactivate the credential; only a
        confirmed auth rejection (revoked/wrong app password) does.
        """
        self.is_active = False
        self.last_error_code = error_code
        self.last_error_at = timezone.now()
        self.save(update_fields=["is_active", "last_error_code", "last_error_at", "updated_at"])

    def __str__(self):
        return f"EmailInboxCredential<{self.user.username}>"


class AnswerBank(models.Model):
    """One answer to one normalized application question, with provenance.

    Typed Profile facts (visa/citizenship/salary) are consulted before this
    table; see `apps.accounts.services.answer_resolver.resolve_answer`.
    Precedence is user-locked > user-set > learned > imported: a lower
    writer may never overwrite a higher one (it can only propose).
    """

    class Source(models.TextChoices):
        USER = "user", "User"
        LEARNED = "learned", "Learned"
        IMPORTED = "imported", "Imported"

    # Values mirror `apps.accounts.tiering.Tier`.
    class RiskTier(models.TextChoices):
        T0_LEGAL = "t0_legal", "T0 - legal attestation"
        T1_COMMERCIAL = "t1_commercial", "T1 - commercial"
        T2_FACTUAL = "t2_factual", "T2 - factual"

    class Category(models.TextChoices):
        EXPERIENCE = "experience", "Experience"
        TECHNOLOGIES = "technologies", "Technologies"
        RELOCATION = "relocation", "Relocation"
        PROJECTS = "projects", "Projects"
        EDUCATION = "education", "Education"
        AVAILABILITY = "availability", "Availability"
        COMPLIANCE = "compliance", "Compliance"
        OTHER = "other", "Other"

    profile = models.ForeignKey(
        Profile,
        on_delete=models.CASCADE,
        related_name="answer_bank",
    )
    question_key = models.CharField(max_length=255)
    # Region key (`apps.accounts.regions.REGION_KEYS`) this answer is limited
    # to, or "" for "everywhere". Location-sensitive answers (relocation,
    # on-site, a named country) are stored per region so one saved for a US
    # job is never reused for an India job.
    scope_region = models.CharField(max_length=8, blank=True, default="")
    question_text = models.TextField(blank=True, default="")
    value = models.JSONField()
    category = models.CharField(
        max_length=32, choices=Category.choices, default=Category.OTHER
    )
    risk_tier = models.CharField(max_length=16, choices=RiskTier.choices)
    source = models.CharField(max_length=16, choices=Source.choices)
    # e.g. {"origin": "resume_llm"} or {"draft_ids": [1, 2, 3]}.
    source_detail = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(default=1.0)
    is_locked = models.BooleanField(default=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "question_key", "scope_region"],
                name="uniq_answerbank_profile_question_key_scope",
            ),
            # A lock is a user decision; automation must never hold one.
            models.CheckConstraint(
                condition=~models.Q(
                    source__in=["learned", "imported"], is_locked=True
                ),
                name="chk_answerbank_locked_only_user",
            ),
        ]

    def __str__(self):
        scope = f"@{self.scope_region}" if self.scope_region else ""
        return f"AnswerBank<{self.profile_id}:{self.question_key}{scope}:{self.source}>"


class AnswerBankHistory(models.Model):
    """Append-only record of a value an `AnswerBank` row used to hold.

    Keyed by (profile, question_key) rather than a FK to the row so history
    survives the row being deleted or replaced.
    """

    profile = models.ForeignKey(
        Profile,
        on_delete=models.CASCADE,
        related_name="answer_bank_history",
    )
    question_key = models.CharField(max_length=255)
    scope_region = models.CharField(max_length=8, blank=True, default="")
    value = models.JSONField()
    risk_tier = models.CharField(max_length=16)
    source = models.CharField(max_length=16)
    source_detail = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(default=1.0)
    was_locked = models.BooleanField(default=False)
    superseded_by_source = models.CharField(max_length=16, blank=True, default="")
    superseded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["profile", "question_key"], name="abh_profile_question_idx"
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("AnswerBankHistory is append-only; it cannot be updated.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"AnswerBankHistory<{self.profile_id}:{self.question_key}>"


class AnswerObservation(models.Model):
    """What the user actually submitted for one question on one application.

    Written when a draft is sent, from the exact values in
    ``AutoApplyDraft.submitted_answers_snapshot``. It is the ground truth the
    learning loop counts over ("same answer across distinct jobs") and the
    (user, job, question) unit shadow-mode precision is measured on.

    ``job_id`` / ``draft_id`` are plain integers, not foreign keys: this app
    sits below ``jobs`` and ``auto_apply`` and must not depend on them, and an
    observation should outlive a discarded draft. Append-only.
    """

    profile = models.ForeignKey(
        Profile,
        on_delete=models.CASCADE,
        related_name="answer_observations",
    )
    question_key = models.CharField(max_length=255)
    question_text = models.TextField(blank=True, default="")
    value = models.JSONField()
    tier = models.CharField(max_length=16)
    field_type = models.CharField(max_length=32, blank=True, default="")
    # Where the submitted value came from (provenance of the draft entry).
    provenance_source = models.CharField(max_length=16, blank=True, default="")
    provenance_origin = models.CharField(max_length=64, blank=True, default="")
    was_edited = models.BooleanField(default=False)
    job_id = models.BigIntegerField(null=True, blank=True)
    draft_id = models.BigIntegerField(null=True, blank=True)
    employer_name = models.CharField(max_length=255, blank=True, default="")
    job_region = models.CharField(max_length=8, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["profile", "question_key"], name="aobs_profile_question_idx"
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("AnswerObservation is append-only; it cannot be updated.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"AnswerObservation<{self.profile_id}:{self.question_key}>"
