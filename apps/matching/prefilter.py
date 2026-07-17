"""Cheap broad gates that shrink the candidate set before scoring.

Pure predicate over snapshot dicts — no DB access. U10 mirrors these gates as
an indexed DB query so scoring only runs on candidates that already pass.

profile snapshot keys: remote_pref, excluded_employers (slugs), min_salary
job snapshot keys: is_remote, employer_slug, salary_min, salary_max
"""
from apps.accounts.models import Profile


def _known_salary_ceiling(job):
    """Best available salary figure for a floor comparison, or None if unknown."""
    if job.get("salary_max") is not None:
        return job["salary_max"]
    return job.get("salary_min")


def passes_prefilter(profile, job):
    """Return True if the job survives the cheap gates and is worth scoring."""
    # Excluded employers.
    if job.get("employer_slug") in set(profile.get("excluded_employers") or []):
        return False

    # Remote preference.
    remote_pref = profile.get("remote_pref", Profile.RemotePref.ANY)
    if remote_pref == Profile.RemotePref.REMOTE_ONLY and not job.get("is_remote"):
        return False
    if remote_pref == Profile.RemotePref.ONSITE_ONLY and job.get("is_remote"):
        return False

    # Salary floor — only exclude when the job's salary is KNOWN and below the
    # floor. Unknown salary is never silently dropped (the scorer handles it).
    min_salary = profile.get("min_salary")
    if min_salary is not None:
        ceiling = _known_salary_ceiling(job)
        if ceiling is not None and ceiling < min_salary:
            return False

    return True
