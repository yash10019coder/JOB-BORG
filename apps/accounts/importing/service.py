"""Running an import: start a job, extract proposals, keep them for review.

``start_document_import`` creates the :class:`ImportJob` (at most one in flight
per profile, rate limited) and the Celery task ``run_document_import`` fills it
with *proposals*. Nothing here writes to the Profile: the user's review decides
(``profile_fields.apply_imported`` / ``resume_facts.apply_entries``).

Privacy (rules P1, P3, D7, D8, D11): the job stores values and short snippets,
never the resume text; the uploaded file is deleted as soon as it has been read;
errors are short codes; logs carry ids, counts and exception *class names* only.
"""
import logging
import os
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.importing import skills_lexicon
from apps.accounts.importing import titles as title_lexicon
from apps.accounts.importing.documents import (
    ALLOWED_EXTENSIONS,
    DocumentError,
    normalize_text,
    read_document,
)
from apps.accounts.importing.pipeline import run_rules
from apps.accounts.models import ImportJob

logger = logging.getLogger(__name__)

PAYLOAD_VERSION = 1

# Friendly text for each short error code (shown on the status page).
ERROR_MESSAGES = {
    "unsupported_type": "Upload a PDF, DOCX or TXT file.",
    "empty_file": "That file is empty.",
    "file_too_large": "That file is too large. The limit is 5 MB.",
    "not_a_pdf": "That file is not a real PDF.",
    "pdf_encrypted": "That PDF is password-protected. Export an unprotected copy and try again.",
    "pdf_too_long": "That PDF has too many pages (the limit is 10).",
    "pdf_active_content": "That PDF contains scripts or attachments, so it cannot be imported.",
    "pdf_unreadable": "We could not read that PDF.",
    "docx_invalid": "That is not a valid Word document.",
    "docx_unreadable": "We could not read that Word document.",
    "docx_macros": "That document contains macros, so it cannot be imported.",
    "docx_unsafe": "That document is not safe to import.",
    "docx_too_complex": "That document is too complex to import.",
    "docx_too_large": "That document is too large to import.",
    "txt_unreadable": "That text file is not valid UTF-8 text.",
    "no_text_found": "No text was found. If it is a scan, export a text-based PDF instead.",
    "no_resume_text": "Your saved resume has no readable text yet. Upload a file instead.",
    "nothing_found": "We could not find anything we are confident about in that document.",
    "timeout": "That import took too long and was stopped.",
    "unexpected": "Something went wrong reading that document.",
}


class ImportInProgress(Exception):
    """The profile already has a pending or running import."""


class ImportRateLimited(Exception):
    """Too many imports in the last hour."""


def error_message(code):
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES["unexpected"])


# --------------------------------------------------------------------------
# Starting a job (A1)
# --------------------------------------------------------------------------
def _rate_key(user_id, kind):
    return f"profile_import:rate:{user_id}:{kind}"


def check_rate_limit(user_id, kind):
    """Count this attempt; raise :class:`ImportRateLimited` past the hourly cap."""
    key = _rate_key(user_id, kind)
    cache.add(key, 0, 3600)
    try:
        count = cache.incr(key)
    except ValueError:  # the key expired between add and incr
        cache.set(key, 1, 3600)
        count = 1
    if count > settings.PROFILE_IMPORT_RATE_PER_HOUR:
        raise ImportRateLimited()


def start_document_import(profile, source_kind, *, upload=None):
    """Create a pending job (and enqueue it once the transaction commits).

    ``upload`` is an uploaded file; omit it for ``current_resume`` (the saved
    resume text is used). Raises :class:`DocumentError` for an unsupported file
    type, :class:`ImportInProgress`, or :class:`ImportRateLimited`.
    """
    from apps.accounts.tasks import run_document_import

    if source_kind == ImportJob.SourceKind.CURRENT_RESUME:
        extension = ""
    else:
        if upload is None:
            raise DocumentError("empty_file")
        extension = os.path.splitext(getattr(upload, "name", "") or "")[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            raise DocumentError("unsupported_type")
        if getattr(upload, "size", 0) == 0:
            raise DocumentError("empty_file")
        if upload.size > getattr(settings, "PROFILE_IMPORT_MAX_PDF_BYTES", 5 * 1024 * 1024):
            raise DocumentError("file_too_large")

    check_rate_limit(profile.user_id, "document")
    try:
        with transaction.atomic():
            job = ImportJob.objects.create(
                profile=profile, kind=ImportJob.Kind.DOCUMENT, source_kind=source_kind
            )
    except IntegrityError:
        raise ImportInProgress() from None
    if upload is not None:
        job.source_file.save(f"import{extension}", ContentFile(upload.read()), save=True)
    transaction.on_commit(lambda: run_document_import.delay(str(job.public_id)))
    return job


# --------------------------------------------------------------------------
# Building the payload (P1): proposals and snippets, never the text
# --------------------------------------------------------------------------
def build_payload(extraction, normalized):
    fields = {
        proposal.field: {
            "value": proposal.value,
            "snippet": proposal.snippet,
            "extractor": proposal.extractor,
            "provisional": proposal.provisional,
        }
        for proposal in extraction.fields
    }
    entries = []
    for entry in extraction.entries:
        data = entry.as_data()
        data.update(
            snippet=entry.snippet,
            status=entry.status,
            layout=entry.layout,
            needs_check=entry.needs_check,
            layout_inferred=entry.layout_inferred,
            rule_fallback=entry.rule_fallback,
            default_accept=entry.default_accept,
            extractor=entry.extractor,
        )
        entries.append(data)
    return {
        "v": PAYLOAD_VERSION,
        "fields": fields,
        "entries": entries,
        "meta": {
            "truncated": normalized.truncated,
            "skills_lexicon": skills_lexicon.SKILLS_LEXICON_VERSION,
            "title_lexicon": title_lexicon.TITLE_LEXICON_VERSION,
        },
    }


# --------------------------------------------------------------------------
# Running a job (the Celery task body)
# --------------------------------------------------------------------------
def _source_text(job):
    """The document text for a job, and nothing else (D1-D11)."""
    if job.source_kind == ImportJob.SourceKind.CURRENT_RESUME:
        text = job.profile.resume_text or ""
        if not text.strip():
            raise DocumentError("no_resume_text")
        return text
    if not job.source_file:
        raise DocumentError("empty_file")
    extension = os.path.splitext(job.source_file.name)[1].lower()
    with job.source_file.open("rb") as handle:
        return read_document(handle.read(), extension).text


def _finish(job, status, *, error_code="", payload=None, extractor=ImportJob.Extractor.RULE):
    job.status = status
    job.error_code = error_code
    job.extractor = extractor
    job.payload = payload if payload is not None else {}
    if job.source_file:
        job.source_file.delete(save=False)  # D8: gone as soon as it has been read
        job.source_file = None
    job.save()


def run_job(public_id):
    """Extract proposals for a pending job. Returns the finished job, or ``None``
    if it was not pending (already handled: the task is idempotent)."""
    claimed = ImportJob.objects.filter(public_id=public_id, status=ImportJob.Status.PENDING).update(
        status=ImportJob.Status.RUNNING, updated_at=timezone.now()
    )
    if not claimed:
        return None
    job = ImportJob.objects.select_related("profile").get(public_id=public_id)
    try:
        normalized = normalize_text(_source_text(job))
        extraction = run_rules(normalized)
        if not extraction.fields and not extraction.entries:
            _finish(job, ImportJob.Status.FAILED, error_code="nothing_found")
        else:
            _finish(job, ImportJob.Status.READY, payload=build_payload(extraction, normalized))
    except DocumentError as exc:
        _finish(job, ImportJob.Status.FAILED, error_code=exc.code)
    except Exception as exc:  # noqa: BLE001 -- coded, class name only (D11)
        logger.error("import job %s failed: %s", job.public_id, type(exc).__name__)
        code = "timeout" if type(exc).__name__ == "SoftTimeLimitExceeded" else "unexpected"
        _finish(job, ImportJob.Status.FAILED, error_code=code)
    logger.info(
        "import job %s %s extractor=%s fields=%d entries=%d code=%s",
        job.public_id, job.status, job.extractor,
        len(job.payload.get("fields", {})), len(job.payload.get("entries", [])), job.error_code,
    )
    return job


# --------------------------------------------------------------------------
# Housekeeping (P1, A2)
# --------------------------------------------------------------------------
def sweep(now=None):
    """Expire unreviewed proposals, fail stuck jobs, drop old rows. Idempotent."""
    now = now or timezone.now()
    stuck_before = now - timedelta(minutes=settings.PROFILE_IMPORT_STUCK_MINUTES)
    counts = {"expired": 0, "failed": 0, "deleted": 0}

    for job in ImportJob.objects.filter(status=ImportJob.Status.READY, expires_at__lte=now):
        _finish(job, ImportJob.Status.EXPIRED, error_code=job.error_code)
        counts["expired"] += 1

    stuck = ImportJob.objects.filter(
        status__in=ImportJob.IN_FLIGHT, updated_at__lte=stuck_before
    )
    for job in stuck:
        _finish(job, ImportJob.Status.FAILED, error_code="timeout")
        counts["failed"] += 1

    # An upload that outlived its job (a crash between save and delete).
    for job in ImportJob.objects.exclude(source_file="").exclude(source_file__isnull=True).filter(
        status__in=[ImportJob.Status.READY, ImportJob.Status.FAILED, ImportJob.Status.EXPIRED,
                    ImportJob.Status.APPLIED, ImportJob.Status.DISCARDED]
    ):
        job.source_file.delete(save=False)
        job.source_file = None
        job.save(update_fields=["source_file", "updated_at"])

    cutoff = now - timedelta(days=settings.PROFILE_IMPORT_ROW_RETENTION_DAYS)
    deleted, _ = ImportJob.objects.filter(created_at__lte=cutoff).exclude(
        status__in=ImportJob.IN_FLIGHT
    ).delete()
    counts["deleted"] = deleted
    return counts
