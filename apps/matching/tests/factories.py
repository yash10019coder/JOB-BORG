"""Shared test helpers for the matching fan-out tests."""
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.accounts.models import Profile
from apps.employers.models import Employer
from apps.jobs.models import Job

User = get_user_model()


def make_employer(slug="acme", name=None):
    return Employer.objects.get_or_create(slug=slug, defaults={"name": name or slug})[0]


def make_job(employer, source_job_id="1", *, tags=None, is_remote=True,
             status=Job.Status.OPEN, scraped_at=None, title="Backend Engineer",
             salary_min=None, salary_max=None, location="Remote - US"):
    return Job.objects.create(
        source_ats="greenhouse",
        source_job_id=str(source_job_id),
        employer=employer,
        title=title,
        classification_tags=tags or [],
        is_remote=is_remote,
        status=status,
        scraped_at=scraped_at or timezone.now(),
        salary_min=salary_min,
        salary_max=salary_max,
        location=location,
        needs_classification=False,
    )


def make_profile(username, *, tags=None, titles=None, locations=None,
                 excluded=None, min_salary=None, remote_pref=None, is_active=True):
    """Create a user (auto-creates a Profile) and set the profile's criteria."""
    user = User.objects.create_user(username=username, password="pw")
    profile = user.profile
    profile.target_tags = tags or []
    profile.target_titles = titles or []
    profile.target_locations = locations or []
    profile.excluded_employers = excluded or []
    profile.min_salary = min_salary
    profile.remote_pref = remote_pref or Profile.RemotePref.ANY
    profile.is_active = is_active
    profile.save()
    return profile
