"""Backfill and normalization services for structured locations.

Functions here accept the model class as a parameter rather than importing
``apps.jobs.models.Job`` / ``apps.accounts.models.Profile`` directly, so the
same function is safe to call from a migration's historical model
(``apps.get_model(...)``) or the management command's live model.
"""
from django.conf import settings

from .engine import CURRENT_LOCATION_ALIAS_VERSION, normalize_location

_DEFAULT_BATCH_SIZE = 500


def _batch_size(batch_size):
    if batch_size is not None:
        return batch_size
    return getattr(settings, "LOCATION_BACKFILL_BATCH_SIZE", _DEFAULT_BATCH_SIZE)


def normalize_target_locations(raw_locations):
    """Structured mirror of a raw target_locations list.

    One entry per raw string (order preserved, 1:1 with the raw list), deduped
    on the normalized (city, region, country) tuple so typing "NYC" and
    "New York" together doesn't double-count in hierarchy matching. Unresolved
    entries are kept (not dropped) -- scoring treats them as inert.
    """
    seen_keys = set()
    normalized = []
    for raw in raw_locations:
        structured = normalize_location(raw)
        key = (structured["city"], structured["region"], structured["country"])
        if structured["resolved"] and key in seen_keys:
            continue
        if structured["resolved"]:
            seen_keys.add(key)
        normalized.append({"raw": raw, **structured})
    return normalized


def backfill_jobs(job_model, batch_size=None):
    """Normalize every Job row not yet at CURRENT_LOCATION_ALIAS_VERSION.

    Idempotent and safe to interleave with concurrent ingestion writes: each
    row's write is a conditional update guarded on the row's
    location_alias_version still equal to the value read for that row -- if a
    concurrent writer already advanced it, this backfill's write for that row
    affects zero rows and is silently skipped rather than overwriting fresher
    data.
    """
    size = _batch_size(batch_size)
    updated = 0
    while True:
        batch = list(
            job_model.objects.exclude(location_alias_version=CURRENT_LOCATION_ALIAS_VERSION)
            .only("id", "location", "location_alias_version")
            .order_by("pk")[:size]
        )
        if not batch:
            break
        for row in batch:
            version_seen = row.location_alias_version
            structured = normalize_location(row.location)
            changed = job_model.objects.filter(
                pk=row.pk, location_alias_version=version_seen
            ).update(
                location_city=structured["city"] or "",
                location_region=structured["region"] or "",
                location_country=structured["country"] or "",
                location_resolved=structured["resolved"],
                location_alias_version=CURRENT_LOCATION_ALIAS_VERSION,
            )
            updated += changed
    return {"updated": updated}


def diff_stale_locations(job_model, profile_model, batch_size=None):
    """Read-only preview of what backfill_jobs/backfill_profiles would
    change, restricted to rows whose resolved value would actually *change*
    (or regress to unresolved) -- not rows that would merely newly-resolve,
    which is the desired, expected outcome of a dataset swap and would
    otherwise dominate the report with noise.

    This is the safety check for the one path a dataset-version cutover can
    land wrong silently: a same-type ambiguity tiebreak (see U2) picking a
    different candidate for a bare alias that the previous dataset version
    resolved uniquely (it never had a same-type collision to disambiguate).
    Intended to run against production data *before* CURRENT_LOCATION_ALIAS_VERSION
    is bumped, so a bad pick is caught as a diff to review, not a silent
    write.

    Unlike backfill_jobs/backfill_profiles, this never writes, so nothing
    advances rows out of the staleness filter between batches -- pagination
    is by explicit pk cursor rather than repeatedly re-querying the same
    top-N stale rows.
    """
    size = _batch_size(batch_size)
    job_changes = []
    last_pk = 0
    while True:
        batch = list(
            job_model.objects.exclude(location_alias_version=CURRENT_LOCATION_ALIAS_VERSION)
            .filter(pk__gt=last_pk)
            .only(
                "id", "location", "location_city", "location_region",
                "location_country", "location_resolved",
            )
            .order_by("pk")[:size]
        )
        if not batch:
            break
        for row in batch:
            last_pk = row.pk
            if not row.location_resolved:
                continue
            structured = normalize_location(row.location)
            old = (row.location_city, row.location_region, row.location_country)
            new = (structured["city"] or "", structured["region"] or "", structured["country"] or "")
            if old != new or not structured["resolved"]:
                job_changes.append(
                    {
                        "pk": row.pk,
                        "location": row.location,
                        "old": {
                            "city": row.location_city,
                            "region": row.location_region,
                            "country": row.location_country,
                            "resolved": row.location_resolved,
                        },
                        "new": structured,
                    }
                )

    profile_changes = []
    last_pk = 0
    while True:
        batch = list(
            profile_model.objects.exclude(
                target_locations_alias_version=CURRENT_LOCATION_ALIAS_VERSION
            )
            .filter(pk__gt=last_pk)
            .only("id", "target_locations", "target_locations_normalized")
            .order_by("pk")[:size]
        )
        if not batch:
            break
        for row in batch:
            last_pk = row.pk
            old_resolved = [e for e in row.target_locations_normalized if e.get("resolved")]
            if not old_resolved:
                continue
            new_normalized = normalize_target_locations(row.target_locations)
            old_keys = {(e["city"], e["region"], e["country"]) for e in old_resolved}
            new_keys = {
                (e["city"], e["region"], e["country"]) for e in new_normalized if e["resolved"]
            }
            if old_keys != new_keys:
                profile_changes.append(
                    {
                        "pk": row.pk,
                        "target_locations": row.target_locations,
                        "old": row.target_locations_normalized,
                        "new": new_normalized,
                    }
                )

    return {"job_changes": job_changes, "profile_changes": profile_changes}


def backfill_profiles(profile_model, batch_size=None):
    """Normalize every Profile row not yet at CURRENT_LOCATION_ALIAS_VERSION.

    Same conditional-update race safety as backfill_jobs, guarded on
    target_locations_alias_version.
    """
    size = _batch_size(batch_size)
    updated = 0
    while True:
        batch = list(
            profile_model.objects.exclude(
                target_locations_alias_version=CURRENT_LOCATION_ALIAS_VERSION
            )
            .only("id", "target_locations", "target_locations_alias_version")
            .order_by("pk")[:size]
        )
        if not batch:
            break
        for row in batch:
            version_seen = row.target_locations_alias_version
            normalized = normalize_target_locations(row.target_locations)
            changed = profile_model.objects.filter(
                pk=row.pk, target_locations_alias_version=version_seen
            ).update(
                target_locations_normalized=normalized,
                target_locations_alias_version=CURRENT_LOCATION_ALIAS_VERSION,
            )
            updated += changed
    return {"updated": updated}
