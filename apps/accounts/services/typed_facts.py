"""Answers that come straight from the user's own profile settings.

Work authorization per country, citizenship, salary by region and the
contact/location facts are *typed* Profile data: the user entered them once on
purpose, and they are consulted before any stored free-form answer
(``resolve_answer``'s order: typed facts -> AnswerBank -> legacy rows).

:func:`resolve` decides whether a question is one of these, which fact answers
it, and what to put in the form. It returns ``None`` when the question is not a
typed-fact question at all. Otherwise it returns a :class:`TypedFact` whose
``value`` is ``None`` whenever the profile cannot answer *confidently* -- an
unknown sponsorship status, two countries named, no option that clearly means
"yes". ``covered`` records whether the profile holds an entry for the
question's subject, so the resolver knows not to let a looser source answer a
question the user has already addressed (an H-1B holder's sponsorship stays
blank rather than being filled from an old saved answer).

Every value returned is exactly what the form can accept: the verbatim option
text when the form has options, never an invented one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from apps.accounts import question_semantics as qs
from apps.accounts.regions import label_for_country, region_for_country
from apps.accounts.salary_bands import get_salary_band_label, validate_salary_by_region
from apps.locations.engine import alpha3_for_country

# Kinds, for provenance and the questions panel.
AUTHORIZATION = "authorization"
SPONSORSHIP = "sponsorship"
CITIZENSHIP = "citizenship"
SALARY = "salary"
COUNTRY = "country"
CITY = "city"
LOCATION = "location"
ADDRESS = "address"
TIMEZONE = "timezone"
MEMBERSHIP = "membership"


@dataclass(frozen=True)
class TypedFact:
    kind: str
    covered: bool = False
    value: Any = None
    # `profile_fields` key whose provenance applies, e.g.
    # "visa_status_by_country.USA" or "location_country".
    provenance_key: str = ""
    detail: dict = field(default_factory=dict)
    # Why `value` is None ("status_unknown", "no_option_match", ...), shown
    # in the questions panel as "not covered (reason)".
    reason: str = ""
    # For authorization/sponsorship: the underlying yes/no, used to detect a
    # disagreeing legacy answer.
    truth: bool | None = None


def job_alpha3(job):
    """ISO alpha-3 of the job's country, or ``None``."""
    return alpha3_for_country(getattr(job, "location_country", "") or "")


def job_region(job):
    """Salary/answer region key of the job's country, or ``None``."""
    return region_for_country(getattr(job, "location_country", "") or "")


def resolve(profile, question_text, *, options=(), job=None) -> TypedFact | None:
    """The typed fact for a question, or ``None`` if it is not one."""
    if profile is None:
        return None
    options = tuple(options or ())

    kind = qs.contact_kind(question_text)
    if kind is not None:
        return _contact(profile, kind, question_text, options)

    citizenship = qs.citizenship_question(question_text)
    if citizenship is not None:
        return _citizenship(profile, citizenship, options)

    auth = qs.authorization_kind(question_text)
    if auth in (qs.AuthKind.AUTHORIZATION, qs.AuthKind.SPONSORSHIP):
        return _authorization(profile, auth, question_text, options, job)

    if qs.is_salary_expectation(question_text):
        return _salary(profile, options, job)
    return None


# --------------------------------------------------------------------------
# Work authorization / sponsorship
# --------------------------------------------------------------------------


def _authorization(profile, auth, text, options, job):
    kind = AUTHORIZATION if auth == qs.AuthKind.AUTHORIZATION else SPONSORSHIP
    mentions = qs.country_mentions(text)
    if mentions.ambiguous or (mentions.unresolved and not mentions.single):
        reason = "multiple_countries" if mentions.ambiguous else "country_unresolved"
        return TypedFact(kind, reason=reason)
    alpha3 = mentions.single or job_alpha3(job)
    if alpha3 is None:
        return TypedFact(kind, reason="no_country")

    pair = profile.authorization_for_country(alpha3)
    if pair is None:
        return TypedFact(kind, reason="no_country_entry", detail={"country": alpha3})

    authorized, needs_sponsorship = pair
    citizen = alpha3 in set(profile.citizenship_countries or [])
    key = "citizenship_countries" if citizen else f"visa_status_by_country.{alpha3}"
    detail = {
        "country": alpha3,
        "status": "citizen" if citizen else (profile.visa_status_by_country or {}).get(alpha3),
    }
    truth = authorized if kind == AUTHORIZATION else needs_sponsorship
    if truth is None:
        return TypedFact(kind, True, None, key, detail, "status_unknown")
    if (
        kind == AUTHORIZATION
        and truth
        and needs_sponsorship is None
        and qs.has_restriction_qualifier(text)
    ):
        # A visa holder is authorized for their sponsor, not "without
        # restriction" / "for any employer".
        return TypedFact(kind, True, None, key, detail, "restricted_authorization", truth)

    picked = qs.pick_yes_no(options, truth)
    if picked is None:
        return TypedFact(kind, True, None, key, detail, "no_option_match", truth)
    return TypedFact(kind, True, picked, key, detail, "", truth)


# --------------------------------------------------------------------------
# Citizenship
# --------------------------------------------------------------------------


def _citizenship(profile, question, options):
    held = [c for c in (profile.citizenship_countries or []) if c]
    key = "citizenship_countries"
    if question.kind == "nationality":
        if not held:
            return TypedFact(CITIZENSHIP, reason="no_citizenship_entered")
        if len(held) > 1:
            return TypedFact(CITIZENSHIP, True, None, key, {"countries": held}, "multiple_citizenships")
        alpha3 = held[0]
        detail = {"countries": held}
        if not options:
            return TypedFact(CITIZENSHIP, True, label_for_country(alpha3), key, detail)
        picked = qs.pick_country_option(options, alpha3)
        if picked is None:
            return TypedFact(CITIZENSHIP, True, None, key, detail, "no_option_match")
        return TypedFact(CITIZENSHIP, True, picked, key, detail)

    if question.unresolved:
        return TypedFact(CITIZENSHIP, reason="country_unresolved")
    if len(question.countries) != 1:
        return TypedFact(CITIZENSHIP, reason="multiple_countries" if question.countries else "no_country")
    if not held:
        return TypedFact(CITIZENSHIP, reason="no_citizenship_entered")
    alpha3 = question.countries[0]
    truth = alpha3 in held
    detail = {"country": alpha3, "countries": held}
    picked = qs.pick_yes_no(options, truth)
    if picked is None:
        return TypedFact(CITIZENSHIP, True, None, key, detail, "no_option_match", truth)
    return TypedFact(CITIZENSHIP, True, picked, key, detail, "", truth)


# --------------------------------------------------------------------------
# Salary by region
# --------------------------------------------------------------------------


def _salary(profile, options, job):
    region = job_region(job)
    if region is None:
        return TypedFact(SALARY, reason="no_region")
    band = validate_salary_by_region(profile.salary_by_region or {}).get(region)
    # "other" means "I'll specify it myself": not an answer a form can take.
    if not band or band == "other":
        return TypedFact(SALARY, reason="no_band", detail={"region": region})
    key = f"salary_by_region.{region}"
    detail = {"region": region, "band": band}
    label = get_salary_band_label(region, band)
    if not options:
        return TypedFact(SALARY, True, label, key, detail)
    picked = qs.pick_exact(options, label)
    if picked is None:
        return TypedFact(SALARY, True, None, key, detail, "no_option_match")
    return TypedFact(SALARY, True, picked, key, detail)


# --------------------------------------------------------------------------
# Contact and location
# --------------------------------------------------------------------------


def _contact(profile, kind, text, options):
    if kind == qs.ContactKind.COUNTRY:
        return _country(profile, options)
    if kind == qs.ContactKind.CITY:
        return _plain(CITY, "location_city", profile.location_city, options)
    if kind == qs.ContactKind.ADDRESS:
        address = ", ".join(
            part.strip() for part in (profile.mailing_address or "").splitlines() if part.strip()
        )
        return _plain(ADDRESS, "mailing_address", address, options)
    if kind == qs.ContactKind.TIMEZONE:
        return _timezone(profile, options)
    if kind == qs.ContactKind.LOCATION:
        return _location(profile, options)
    return _membership(profile, text, options)


def _plain(kind, key, value, options):
    value = (value or "").strip()
    if not value:
        return TypedFact(kind, reason="not_entered")
    if not options:
        return TypedFact(kind, True, value, key)
    picked = qs.pick_exact(options, value)
    if picked is None:
        return TypedFact(kind, True, None, key, reason="no_option_match")
    return TypedFact(kind, True, picked, key)


def _country(profile, options):
    alpha3 = (profile.location_country or "").strip()
    if not alpha3:
        return TypedFact(COUNTRY, reason="not_entered")
    key, detail = "location_country", {"country": alpha3}
    if not options:
        return TypedFact(COUNTRY, True, label_for_country(alpha3), key, detail)
    picked = qs.pick_country_option(options, alpha3)
    if picked is None:
        return TypedFact(COUNTRY, True, None, key, detail, "no_option_match")
    # A "Country" picker is usually the phone dial-code selector: if the user
    # typed an international number, the dial code we pick must agree with it.
    phone = (profile.phone or "").strip()
    dial = qs.option_dial_code(picked)
    if phone.startswith("+") and dial:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if not digits.startswith(dial):
            return TypedFact(COUNTRY, True, None, key, detail, "phone_dial_mismatch")
    return TypedFact(COUNTRY, True, picked, key, detail)


def _location(profile, options):
    city = (profile.location_city or "").strip()
    alpha3 = (profile.location_country or "").strip()
    if not city or not alpha3:
        return TypedFact(LOCATION, reason="not_entered")
    value = f"{city}, {label_for_country(alpha3)}"
    key = "location_country"
    if not options:
        return TypedFact(LOCATION, True, value, key, {"city": city, "country": alpha3})
    picked = qs.pick_exact(options, value)
    if picked is None:
        return TypedFact(LOCATION, True, None, key, reason="no_option_match")
    return TypedFact(LOCATION, True, picked, key)


def _offset_label(tz_name):
    try:
        from zoneinfo import ZoneInfo

        offset = datetime.now(ZoneInfo(tz_name)).utcoffset()
    except Exception:  # unknown zone / no tz database
        return None
    if offset is None:
        return None
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def _timezone(profile, options):
    tz = (profile.working_timezone or "").strip()
    if not tz:
        return TypedFact(TIMEZONE, reason="not_entered")
    offset = _offset_label(tz)
    key, detail = "working_timezone", {"timezone": tz}
    if not options:
        value = f"{tz} (UTC{offset})" if offset else tz
        return TypedFact(TIMEZONE, True, value, key, detail)
    picked = _timezone_option(options, tz, offset)
    if picked is None:
        return TypedFact(TIMEZONE, True, None, key, detail, "no_option_match")
    return TypedFact(TIMEZONE, True, picked, key, detail)


def _timezone_option(options, tz, offset):
    """The one option naming this IANA zone or its UTC/GMT offset."""
    named = [o for o in options if tz.lower() in str(o).lower()]
    if len(named) == 1:
        return named[0]
    if named or not offset:
        return None
    sign, hh, mm = offset[0], offset[1:3], offset[4:6]
    forms = {f"{sign}{hh}:{mm}", f"{sign}{hh}{mm}", f"{sign}{int(hh)}:{mm}"}
    by_offset = [
        o
        for o in options
        if any(tag in str(o).lower() for tag in ("utc", "gmt"))
        and any(form in str(o).replace(" ", "") for form in forms)
    ]
    return by_offset[0] if len(by_offset) == 1 else None


def _membership(profile, text, options):
    alpha3 = (profile.location_country or "").strip()
    if not alpha3:
        return TypedFact(MEMBERSHIP, reason="not_entered")
    mentions = qs.country_mentions(text)
    if mentions.unresolved or not mentions.alpha3:
        # "Are you located in Pune / the Bay Area?": a place we cannot test.
        return TypedFact(MEMBERSHIP, True, None, "location_country", reason="place_unresolved")
    truth = alpha3 in mentions.alpha3
    detail = {"country": alpha3, "asked": list(mentions.alpha3)}
    picked = qs.pick_yes_no(options, truth)
    if picked is None:
        return TypedFact(MEMBERSHIP, True, None, "location_country", detail, "no_option_match", truth)
    return TypedFact(MEMBERSHIP, True, picked, "location_country", detail, "", truth)


def legacy_conflicts(profile, legacy_rows):
    """Per-country settings that disagree with a backfilled legacy answer.

    Legacy answers are country-agnostic while the typed settings are per
    country, so they can legitimately differ; the Answers tab lists them so
    the user can delete the old answer if it is stale. Never blocks anything.
    """
    by_key = {row.question_key: row for row in legacy_rows}
    held = set(profile.citizenship_countries or []) | set(profile.visa_status_by_country or {})
    conflicts = []
    for key, index, label in (
        ("legacy:work_authorization", 0, "work authorization"),
        ("legacy:sponsorship", 1, "sponsorship"),
    ):
        row = by_key.get(key)
        boolean = (row.source_detail or {}).get("answer_bool") if row is not None else None
        if boolean is None:
            continue
        for alpha3 in sorted(held):
            pair = profile.authorization_for_country(alpha3)
            typed = pair[index] if pair else None
            if typed is not None and typed != boolean:
                conflicts.append(
                    {"key": key, "label": label, "country": label_for_country(alpha3)}
                )
    return conflicts
