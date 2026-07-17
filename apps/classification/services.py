"""Classification service layer — apply the rule engine to Job rows."""
from apps.jobs.models import Job

from .engine import CURRENT_RULESET_VERSION, classify, load_ruleset


def _snapshot(job):
    return {
        "title": job.title,
        "description": job.description,
        "location": job.location,
        "is_remote": job.is_remote,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
    }


def classify_job(job, ruleset=None):
    """Classify one Job, persist tags, clear its flag, stamp the ruleset version.

    Returns True if the resulting tags differ from what the job already had
    (the signal U10 matching should re-run), False otherwise.
    """
    if ruleset is None:
        ruleset = load_ruleset()
    new_tags = classify(_snapshot(job), ruleset)
    changed = new_tags != job.classification_tags

    job.classification_tags = new_tags
    job.ruleset_version = ruleset.get("version", CURRENT_RULESET_VERSION)
    job.needs_classification = False
    job.save(
        update_fields=[
            "classification_tags",
            "ruleset_version",
            "needs_classification",
            "updated_at",
        ]
    )
    return changed
