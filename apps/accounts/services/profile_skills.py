"""The profile-level skill list, with the evidence an import found for each skill.

``ProfileSkill`` rows come from the import review (today, GitHub only). This
module is their only writer. Evidence follows a fixed schema and is validated
again here, so third-party strings (repo names, topics) cannot reach storage
unchecked. Nothing is ever deleted: removing a skill marks it ``dismissed`` so
the next import shows it unticked instead of proposing it as new.

Evidence is shown to the user and is never added to job-based years.
"""
import re
from dataclasses import dataclass
from dataclasses import field as dc_field

from django.db import transaction

from apps.accounts.importing.validators import ImportValueError, clean_text
from apps.accounts.models import Profile, ProfileSkill

MAX_NAME = 40
MAX_REPOS_SHOWN = 5
MAX_SKILLS = 40
REPO_NAME = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$")
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SCOPES = frozenset({"recent", "top"})
SOURCES = frozenset({"language", "topic", "package"})
FULL = "full"


class SkillValueError(ValueError):
    """A skill failed validation. ``code`` is short and safe to show or log."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def clean_evidence(raw):
    """Rebuild evidence from the fixed schema; reject anything that does not fit.

    ``{repo_count, scope, scope_size, first_seen, last_seen, repos, sources}``.
    Unknown keys are dropped, never stored.
    """
    if not isinstance(raw, dict):
        raise SkillValueError("bad_evidence")
    count, size = raw.get("repo_count"), raw.get("scope_size")
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in (count, size)) or count > size:
        raise SkillValueError("bad_evidence")
    if raw.get("scope") not in SCOPES:
        raise SkillValueError("bad_evidence")
    months = []
    for name in ("first_seen", "last_seen"):
        value = raw.get(name)
        if not isinstance(value, str) or not MONTH.match(value):
            raise SkillValueError("bad_evidence")
        months.append(value)
    if months[0] > months[1]:
        raise SkillValueError("bad_evidence")
    repos = raw.get("repos") or []
    sources = raw.get("sources") or []
    if not isinstance(repos, (list, tuple)) or not isinstance(sources, (list, tuple)):
        raise SkillValueError("bad_evidence")
    repos = [r for r in repos if isinstance(r, str) and REPO_NAME.match(r)][:MAX_REPOS_SHOWN]
    sources = sorted({s for s in sources if s in SOURCES})
    return {
        "repo_count": count, "scope": raw["scope"], "scope_size": size,
        "first_seen": months[0], "last_seen": months[1], "repos": repos, "sources": sources,
    }


def clean_skill(data):
    """Validate one skill dict: ``name``, ``origin`` and ``evidence``."""
    try:
        name = clean_text(str(data.get("name") or ""), max_len=MAX_NAME, min_len=1)
    except ImportValueError:
        raise SkillValueError("bad_name") from None
    origin = data.get("origin") or ProfileSkill.Origin.GITHUB
    if origin not in ProfileSkill.Origin.values:
        raise SkillValueError("bad_origin")
    return {"name": name, "key": name.casefold(), "origin": origin, "evidence": clean_evidence(data.get("evidence"))}


def merge_evidence(old, new):
    """Keep the richer evidence: the larger count over the larger scope, the
    earliest first and latest last month, and the union of repos and sources."""
    if (new["repo_count"], new["scope_size"]) >= (old.get("repo_count", 0), old.get("scope_size", 0)):
        base = dict(new)
    else:
        base = dict(old)
    base["first_seen"] = min(old.get("first_seen", new["first_seen"]), new["first_seen"])
    base["last_seen"] = max(old.get("last_seen", new["last_seen"]), new["last_seen"])
    base["repos"] = list(dict.fromkeys([*base.get("repos", []), *old.get("repos", []), *new["repos"]]))[:MAX_REPOS_SHOWN]
    base["sources"] = sorted(set(old.get("sources", [])) | set(new["sources"]))
    return base


@dataclass
class SkillsResult:
    created: list = dc_field(default_factory=list)
    updated: list = dc_field(default_factory=list)
    unchanged: list = dc_field(default_factory=list)


def apply_skills(profile_id, skills, *, mode=FULL):
    """Upsert accepted skills. Never deletes.

    A full run replaces a skill's evidence; a light or partial run merges it, so a
    transient rate limit can never overwrite richer proof. Accepting a skill the
    user had removed brings it back. Any invalid skill raises
    :class:`SkillValueError` before anything is written.
    """
    cleaned = []
    for index, data in enumerate(skills):
        try:
            cleaned.append(clean_skill(data))
        except SkillValueError as exc:
            raise SkillValueError(f"{index}:{exc.code}") from None
    if len({item["key"] for item in cleaned}) != len(cleaned):
        raise SkillValueError("duplicate_skill")
    if len(cleaned) > MAX_SKILLS:
        raise SkillValueError("too_many_skills")

    result = SkillsResult()
    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(pk=profile_id)
        stored = {row.key: row for row in ProfileSkill.objects.filter(profile=profile)}
        for item in cleaned:
            row = stored.get(item["key"])
            if row is None:
                ProfileSkill.objects.create(profile=profile, **item)
                result.created.append(item["key"])
                continue
            evidence = item["evidence"] if mode == FULL else merge_evidence(row.evidence or {}, item["evidence"])
            if row.evidence == evidence and not row.dismissed and row.name == item["name"]:
                result.unchanged.append(item["key"])
                continue
            row.name, row.evidence, row.dismissed = item["name"], evidence, False
            row.save(update_fields=["name", "evidence", "dismissed", "updated_at"])
            result.updated.append(item["key"])
    return result


def dismiss_skill(profile, pk):
    """Hide one of the profile's own skills so a later import shows it unticked.
    ``True`` if it existed and was visible. Owner-scoped, never deletes."""
    return bool(ProfileSkill.objects.filter(profile=profile, pk=pk, dismissed=False).update(dismissed=True))


def visible_skills(profile):
    """The skills shown on the Import tab: not removed, best evidence first."""
    rows = ProfileSkill.objects.filter(profile=profile, dismissed=False)
    return sorted(rows, key=lambda r: (-(r.evidence or {}).get("repo_count", 0), r.name.casefold()))


def dismissed_keys(profile):
    """Casefolded names the user removed (used to mark re-proposed skills)."""
    return set(ProfileSkill.objects.filter(profile=profile, dismissed=True).values_list("key", flat=True))
