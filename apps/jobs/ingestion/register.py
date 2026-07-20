"""Shared validate-then-persist sequence for turning an ATS board token into
an Employer + JobSource.

Used by both the ``add_job_source`` command and the discovery admin's approve
action so the manual and automated registration paths can never drift.
"""
from dataclasses import dataclass

from django.utils.text import slugify

from apps.employers.models import Employer
from apps.jobs.models import JobSource

from .dispatch import get_client
from .vendor.workday.scraper import URL_PATTERN as _WORKDAY_URL_PATTERN


@dataclass
class RegistrationOutcome:
    status: str  # "registered" or "already_registered"
    employer: Employer
    job_source: JobSource
    job_count: int


def derive_employer_name(ats, token):
    """Best-effort display name derived from a board token.

    Every other platform's board_token is already a short, readable slug
    (title-casing it is enough). Workday's token is a full careers URL --
    title-casing that verbatim would produce something like
    "Https://Acme.Wd3.Myworkdayjobs.Com/Careers" instead of "Acme", so this
    extracts the company segment from the URL first.
    """
    if ats == JobSource.ATS.WORKDAY:
        match = _WORKDAY_URL_PATTERN.match(token.rstrip("/"))
        name_source = match.group("company") if match else token
    else:
        name_source = token
    return name_source.replace("-", " ").replace("_", " ").title()


def register_job_source(token, employer_name=None, *, ats=JobSource.ATS.GREENHOUSE, client=None):
    """Validate ``token`` against the live ATS API and register it.

    Raises:
        IngestionUnavailable / IngestionParseError (or an ATS-specific
        subclass): propagated as-is from the client — callers decide how to
        surface a failed validation.
    """
    client = client or get_client(ats)
    jobs = client.fetch_jobs(token)

    existing = JobSource.objects.filter(ats=ats, board_token=token).first()
    if existing is not None:
        return RegistrationOutcome(
            status="already_registered",
            employer=existing.employer,
            job_source=existing,
            job_count=len(jobs),
        )

    name = employer_name or derive_employer_name(ats, token)
    # Slug is derived from the employer name, not the raw token: board_token
    # isn't always slug-safe (Workday's is a full URL), and keying Employer
    # identity on an ATS-specific token risks two different companies on
    # different platforms colliding onto one Employer row if they happen to
    # pick the same short token. slugify(name) matches Employer.save()'s own
    # fallback for employers created outside this path.
    employer, _ = Employer.objects.get_or_create(
        slug=slugify(name), defaults={"name": name}
    )
    job_source = JobSource.objects.create(ats=ats, board_token=token, employer=employer)
    return RegistrationOutcome(
        status="registered", employer=employer, job_source=job_source, job_count=len(jobs)
    )
