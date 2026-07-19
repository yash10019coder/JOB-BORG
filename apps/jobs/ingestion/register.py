"""Shared validate-then-persist sequence for turning a Greenhouse board token
into an Employer + JobSource.

Used by both the ``add_job_source`` command and the discovery admin's approve
action so the manual and automated registration paths can never drift.
"""
from dataclasses import dataclass

from apps.employers.models import Employer
from apps.jobs.models import JobSource

from .greenhouse_client import GreenhouseClient


@dataclass
class RegistrationOutcome:
    status: str  # "registered" or "already_registered"
    employer: Employer
    job_source: JobSource
    job_count: int


def register_job_source(token, employer_name=None, *, client=None):
    """Validate ``token`` against the live Greenhouse API and register it.

    Raises:
        GreenhouseUnavailable / GreenhouseParseError: propagated as-is from
        the client — callers decide how to surface a failed validation.
    """
    client = client or GreenhouseClient()
    jobs = client.fetch_jobs(token)

    existing = JobSource.objects.filter(
        ats=JobSource.ATS.GREENHOUSE, board_token=token
    ).first()
    if existing is not None:
        return RegistrationOutcome(
            status="already_registered",
            employer=existing.employer,
            job_source=existing,
            job_count=len(jobs),
        )

    name = employer_name or token.replace("-", " ").title()
    employer, _ = Employer.objects.get_or_create(slug=token, defaults={"name": name})
    job_source = JobSource.objects.create(
        ats=JobSource.ATS.GREENHOUSE, board_token=token, employer=employer
    )
    return RegistrationOutcome(
        status="registered", employer=employer, job_source=job_source, job_count=len(jobs)
    )
