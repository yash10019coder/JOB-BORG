"""Resume text extraction -- PDF/DOCX/TXT -> plain text.

Runs as an explicit Celery task (`parse_resume`), enqueued from
`Profile.set_resume()` (apps/accounts/models.py) -- deliberately not a
`post_save` signal, so the trigger is visible at the call site instead of
implicit, and PDF/DOCX extraction (unbounded-latency work) never blocks the
request/save path.

Bounded by `RESUME_PARSE_TASK_TIME_LIMIT_SECONDS` /
`RESUME_PARSE_TASK_SOFT_TIME_LIMIT_SECONDS` (config/settings/base.py):
allowlisting file type at the model layer (see `validate_resume_file` in
models.py) doesn't guarantee a well-formed file, and PDF/DOCX parsers have a
history of resource-exhaustion bugs (zip/decompression bombs, malformed
structure) independent of content type.
"""
import logging
import os

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


def extract_text_from_pdf(file_obj):
    """Extract plain text from a PDF file-like object. Never raises -- a
    corrupted/malformed PDF or a scanned-image PDF with no extractable text
    both resolve to "" rather than an exception."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(file_obj)
        pages_text = []
        for page in reader.pages:
            try:
                pages_text.append(page.extract_text() or "")
            except Exception:  # noqa: BLE001 -- one bad page must not fail the whole resume
                logger.warning("Failed to extract text from a PDF page", exc_info=True)
        return "\n".join(t for t in pages_text if t).strip()
    except Exception as exc:  # noqa: BLE001 -- corrupted/malformed PDF must not raise
        logger.warning("Failed to parse PDF resume: %s", exc)
        return ""


def extract_text_from_docx(file_obj):
    """Extract plain text from a DOCX file-like object. Never raises."""
    from docx import Document

    try:
        document = Document(file_obj)
        paragraphs = [p.text for p in document.paragraphs if p.text]
        return "\n".join(paragraphs).strip()
    except Exception as exc:  # noqa: BLE001 -- corrupted/malformed DOCX must not raise
        logger.warning("Failed to parse DOCX resume: %s", exc)
        return ""


def extract_text_from_txt(file_obj):
    """Read a plain-text resume. Never raises."""
    try:
        raw = file_obj.read()
        text = raw.decode("utf-8", errors="ignore") if isinstance(raw, bytes) else raw
        return text.strip()
    except Exception as exc:  # noqa: BLE001 -- unreadable file must not raise
        logger.warning("Failed to read TXT resume: %s", exc)
        return ""


_EXTRACTORS = {
    ".pdf": extract_text_from_pdf,
    ".docx": extract_text_from_docx,
    ".txt": extract_text_from_txt,
}


def extract_resume_text(file_field):
    """Extract plain text from an uploaded resume file.

    Dispatches on file extension. Returns "" (never raises) for a missing
    file, an unsupported extension, an unreadable file, or a file with no
    extractable text (e.g. a scanned-image PDF) -- callers must treat an
    empty result as "no resume text available", not an error.
    """
    if not file_field:
        return ""

    ext = os.path.splitext(file_field.name or "")[1].lower()
    extractor = _EXTRACTORS.get(ext)
    if extractor is None:
        logger.warning("Unsupported resume extension for parsing: %s", ext)
        return ""

    try:
        file_field.seek(0)
        return extractor(file_field)
    except Exception:  # noqa: BLE001 -- parsing must never crash the caller
        logger.exception("Unexpected error extracting resume text")
        return ""


@shared_task(
    name="apps.accounts.parse_resume",
    time_limit=settings.RESUME_PARSE_TASK_TIME_LIMIT_SECONDS,
    soft_time_limit=settings.RESUME_PARSE_TASK_SOFT_TIME_LIMIT_SECONDS,
)
def parse_resume(profile_id):
    """Extract resume_text for one Profile.

    Enqueued explicitly via `.delay()` from `Profile.set_resume()` -- never
    runs implicitly off a signal (see module docstring).
    """
    from .models import Profile

    try:
        profile = Profile.objects.get(pk=profile_id)
    except Profile.DoesNotExist:
        logger.warning("parse_resume: Profile %s no longer exists", profile_id)
        return

    if not profile.resume:
        logger.info("parse_resume: Profile %s has no resume file", profile_id)
        return

    profile.resume_text = extract_resume_text(profile.resume)
    profile.save(update_fields=["resume_text", "updated_at"])
