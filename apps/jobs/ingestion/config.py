"""Ingestion helpers — content hashing for change detection."""
import hashlib

# Fields whose change should re-flag a job for classification.
_HASH_FIELDS = ("title", "description", "location", "is_remote", "salary_min", "salary_max")


def compute_content_hash(normalized_job):
    """Stable SHA-256 over the content fields of a normalized job dict.

    Ingestion re-runs are idempotent: an unchanged posting hashes identically,
    so classification only re-runs when real content changes.
    """
    parts = []
    for field in _HASH_FIELDS:
        parts.append(f"{field}={normalized_job.get(field)!r}")
    payload = "|".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
