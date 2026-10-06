"""Turning fetched GitHub data into projects and skills with evidence (rules GHX3-GHX7).

Pure: no network, no database. The fetcher (``github.py``) gathers whitelisted
data; this module decides what it shows. Nothing here is invented: a skill is
proposed only if a repository shows it (its language, its topics, a dependency
file or a Dockerfile), and every skill carries the evidence that supports it.

Input (``derive``): ``{"user": {...}, "repos": [...], "enrichment": {repo: {...}},
"pinned": [names]}``. ``enrichment`` only exists in full mode and only for the
shortlisted repos: ``{"languages": {name: bytes}, "files": [names], "manifests":
{filename: text}}``.
"""
import json
import math
import re
import tomllib
from datetime import date

from apps.accounts.importing import skills_lexicon
from apps.accounts.services import profile_skills
from apps.accounts.services.resume_facts import MAX_SKILLS as MAX_ENTRY_SKILLS
from apps.accounts.services.resume_facts import clean_description

REPO_NAME = profile_skills.REPO_NAME
MIN_REPO_SIZE_KB = 5  # GitHub reports size in KB; smaller repos are scaffolding
LANGUAGE_SHARE = 0.10  # a language counts for a repo from this share of its bytes
MAX_SKILLS = 40
MAX_PROJECTS = 25
ENRICH_REPOS = 8  # repositories read in depth in full mode
MAX_MANIFEST_BYTES = 64 * 1024
MAX_LINE = 2000
MAX_DEPTH = 20
RECENT_MONTHS = 12
MANIFESTS = (
    "package.json", "composer.json", "requirements.txt", "pyproject.toml", "go.mod", "Gemfile",
    "pom.xml", "build.gradle", "build.gradle.kts",
)
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})")
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_GEM = re.compile(r"""^\s*gem\s+['"]([\w.-]+)['"]""")
_ARTIFACT = re.compile(r"<artifactId>\s*([\w.-]{1,100})\s*</artifactId>")
_GRADLE = re.compile(r"""['"]([\w.-]{1,100}):([\w.-]{1,100})(?::[^'"\s]{0,40})?['"]""")
_GOMOD = re.compile(r"^\s*(?:require\s+)?([A-Za-z0-9][\w.-]*\.[a-z]{2,}/[\w./~-]{1,200})\s+v[\w.+-]+", re.M)


def month_of(value):
    """``'2019-03-14T10:00:00Z'`` -> ``'2019-03'``; anything else -> ``None``."""
    match = _ISO_MONTH.match(value) if isinstance(value, str) else None
    if not match or not 1 <= int(match.group(2)) <= 12 or not 1970 <= int(match.group(1)) <= 2100:
        return None
    return f"{match.group(1)}-{match.group(2)}"


def _months_between(earlier, later):
    ey, em = int(earlier[:4]), int(earlier[5:7])
    ly, lm = int(later[:4]), int(later[5:7])
    return (ly - ey) * 12 + (lm - em)


# --------------------------------------------------------------------------
# Which repositories count
# --------------------------------------------------------------------------
def eligible_repos(login, repos, pinned=()):
    """GHX3: own, non-archived, non-template, non-trivial repositories with a
    valid name and usable dates. A fork counts only when the user pinned it.
    Order is preserved."""
    pinned = {p for p in pinned if isinstance(p, str)}
    out = []
    for repo in repos or []:
        if not isinstance(repo, dict):
            continue
        name = repo.get("name")
        size = repo.get("size")
        if not isinstance(name, str) or not REPO_NAME.match(name):
            continue
        if (repo.get("fork") and name not in pinned) or repo.get("archived") or repo.get("is_template"):
            continue
        if not isinstance(size, int) or size < MIN_REPO_SIZE_KB:
            continue
        if month_of(repo.get("created_at")) is None or month_of(repo.get("pushed_at")) is None:
            continue
        out.append(repo)
    return out


def rank_projects(login, repos, pinned=(), today=None):
    """GHX7: eligible repositories, best first. Pinned repositories lead, then
    log-weighted stars, recent activity and having a description; ties by name."""
    today = today or date.today()
    this_month = f"{today.year:04d}-{today.month:02d}"
    pinned = {p for p in pinned if isinstance(p, str)}

    def score(repo):
        stars = repo.get("stargazers_count") if isinstance(repo.get("stargazers_count"), int) else 0
        recent = _months_between(month_of(repo["pushed_at"]), this_month) <= RECENT_MONTHS
        return -(
            (100 if repo["name"] in pinned else 0) + 2 * math.log1p(max(stars, 0))
            + (3 if recent else 0) + (1 if repo.get("description") else 0)
        )

    return sorted(eligible_repos(login, repos, pinned), key=lambda r: (score(r), r["name"].casefold()))


# --------------------------------------------------------------------------
# Manifests (GHX4): hostile-input-safe, text only, never executed
# --------------------------------------------------------------------------
def _safe_text(text):
    """The text if it is small, has no huge line and is not nested absurdly deep."""
    if not isinstance(text, str) or len(text.encode("utf-8", "ignore")) > MAX_MANIFEST_BYTES:
        return None
    if any(len(line) > MAX_LINE for line in text.splitlines()):
        return None
    depth = deepest = 0
    for char in text:
        if char in "[{":
            depth += 1
            deepest = max(deepest, depth)
            if deepest > MAX_DEPTH:
                return None
        elif char in "]}":
            depth = max(depth - 1, 0)
    return text


def _names_from_requirement_lines(lines):
    names = []
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http")) or "://" in line:
            continue
        match = _REQ_NAME.match(line)
        if match:
            names.append(match.group(1))
    return names


def parse_manifest(filename, text):
    """The dependency names a manifest declares. Never raises: any failure,
    including a hostile file, yields an empty list."""
    try:
        text = _safe_text(text)
        if text is None:
            return []
        if filename in ("package.json", "composer.json"):
            data = json.loads(text)
            if not isinstance(data, dict):
                return []
            keys = ("dependencies", "devDependencies", "peerDependencies", "require", "require-dev")
            return [name for key in keys if isinstance(data.get(key), dict) for name in data[key]]
        if filename == "requirements.txt":
            return _names_from_requirement_lines(text.splitlines())
        if filename == "pyproject.toml":
            data = tomllib.loads(text)
            names = _names_from_requirement_lines(data.get("project", {}).get("dependencies", []) or [])
            for group in (data.get("project", {}).get("optional-dependencies", {}) or {}).values():
                names += _names_from_requirement_lines(group or [])
            poetry = data.get("tool", {}).get("poetry", {})
            names += list(poetry.get("dependencies", {}) or {})
            names += list((poetry.get("group", {}).get("dev", {}) or {}).get("dependencies", {}) or {})
            return [n for n in names if n.lower() != "python"]
        if filename == "go.mod":
            return _GOMOD.findall(text)
        if filename == "Gemfile":
            return [m.group(1) for line in text.splitlines() if (m := _GEM.match(line))]
        if filename == "pom.xml":
            return _ARTIFACT.findall(text)
        if filename in ("build.gradle", "build.gradle.kts"):
            return [artifact for _, artifact in _GRADLE.findall(text)]
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, MemoryError):
        return []
    return []


def manifest_skills(filename, text):
    """Canonical skills for the packages a manifest declares (unknown ones add nothing)."""
    out = []
    for name in parse_manifest(filename, text)[:2000]:
        skill = skills_lexicon.package_skill(str(name))
        if skill and skill not in out:
            out.append(skill)
    return out


# --------------------------------------------------------------------------
# Skills per repository, then across repositories (GHX3, GHX6)
# --------------------------------------------------------------------------
def repo_skills(repo, extra=None):
    """``{skill: {sources}}`` for one repository, plus the set of in-depth sources
    (``extra`` is this repo's enrichment, if any)."""
    found = {}

    def add(skill, source):
        if skill:
            found.setdefault(skill, set()).add(source)

    add(skills_lexicon.language_skill(repo.get("language")), "language")
    for topic in repo.get("topics") or []:
        if isinstance(topic, str):
            add(skills_lexicon.canonical_skill(topic.replace("-", " ")) or skills_lexicon.canonical_skill(topic), "topic")
    if extra:
        languages = extra.get("languages") or {}
        total = sum(v for v in languages.values() if isinstance(v, int)) or 0
        for language, size in languages.items():
            if total and isinstance(size, int) and size / total >= LANGUAGE_SHARE:
                add(skills_lexicon.language_skill(language), "language_share")
        for filename in extra.get("files") or []:
            add(skills_lexicon.FILE_SKILLS.get(str(filename).lower()), "package")
        for filename, text in (extra.get("manifests") or {}).items():
            if filename in MANIFESTS:
                for skill in manifest_skills(filename, text):
                    add(skill, "package")
    return found


def _source_names(sources):
    return sorted({"language" if s == "language_share" else s for s in sources})


def proof_line(name, evidence):
    """GHX6: the evidence in words, saying what it was counted over."""
    count, size = evidence["repo_count"], evidence["scope_size"]
    scope = f"{count} of {size} recent repos" if evidence["scope"] == "recent" else f"{count} of your top {size} repos"
    return f"{name} · {scope} · first {evidence['first_seen'][:4]} · last {evidence['last_seen'][:4]}"


def build_skills(repos, enrichment=None, dismissed=()):
    """GHX3, GHX6: skills with evidence, best first, at most 40.

    ``repos`` are the eligible repositories. A skill's scope is ``recent`` when
    any language or topic of the scanned repositories shows it, otherwise
    ``top`` (only the in-depth reading of the shortlist shows it); the count is
    always over that one set, so numbers never mix populations.
    """
    enrichment = enrichment or {}
    per_skill = {}
    for repo in repos:
        for skill, sources in repo_skills(repo, enrichment.get(repo["name"])).items():
            slot = per_skill.setdefault(skill, {"repos": {}, "sources": set()})
            slot["repos"][repo["name"]] = repo
            slot["sources"] |= sources
    scanned = len(repos)
    deep = len([r for r in repos if r["name"] in enrichment])
    dismissed = {d.casefold() for d in dismissed}
    out = []
    for skill, slot in per_skill.items():
        broad = bool(slot["sources"] & {"language", "topic"})
        scope, size = ("recent", scanned) if broad else ("top", deep)
        members = list(slot["repos"].values())
        if not broad:
            members = [r for r in members if r["name"] in enrichment]
        if not members or size < 1:
            continue
        evidence = {
            "repo_count": len(members), "scope": scope, "scope_size": max(size, len(members)),
            "first_seen": min(month_of(r["created_at"]) for r in members),
            "last_seen": max(month_of(r["pushed_at"]) for r in members),
            "repos": [r["name"] for r in sorted(members, key=lambda r: r["pushed_at"], reverse=True)][:5],
            "sources": _source_names(slot["sources"]),
        }
        evidence["last_seen"] = max(evidence["last_seen"], evidence["first_seen"])
        out.append({
            "name": skill, "origin": "github", "evidence": evidence, "proof": proof_line(skill, evidence),
            "dismissed": skill.casefold() in dismissed,
            "default_accept": len(members) >= 2 and skill.casefold() not in dismissed,
        })
    out.sort(key=lambda s: (-s["evidence"]["repo_count"], _neg(s["evidence"]["last_seen"]), s["name"].casefold()))
    return out[:MAX_SKILLS]


def _neg(month):
    """Sort key putting later months first."""
    return -(int(month[:4]) * 12 + int(month[5:7]))


# --------------------------------------------------------------------------
# Projects (GHX7)
# --------------------------------------------------------------------------
def _snippet(repo):
    stars = repo.get("stargazers_count") if isinstance(repo.get("stargazers_count"), int) else 0
    month = month_of(repo["pushed_at"])
    label = date(int(month[:4]), int(month[5:7]), 1).strftime("%b %Y")
    return (f"★ {stars} · " if stars else "") + f"updated {label}"


def build_projects(login, ranked, enrichment=None):
    """Entry proposals for the best repositories, already valid for ``clean_entry``
    (a repo that cannot be made valid is dropped, never stored half-right)."""
    enrichment = enrichment or {}
    entries = []
    for repo in ranked:
        if len(entries) >= MAX_PROJECTS:
            break
        name = repo["name"]
        if not 2 <= len(name) <= 80 or name.endswith("."):
            continue
        start, end = month_of(repo["created_at"]), month_of(repo["pushed_at"])
        if end < start:
            end = start
        skills = sorted(repo_skills(repo, enrichment.get(name)))[:MAX_ENTRY_SKILLS]
        entries.append({
            "kind": "project", "title": name, "organization": "",
            "start": start, "end": end, "is_current": False, "precision": "month",
            "skills": skills, "description": clean_description(repo.get("description")),
            # rebuilt from the validated login and name, never copied from the response
            "url": f"https://github.com/{login}/{name}",
            "snippet": _snippet(repo), "extractor": "github", "default_accept": False,
        })
    return entries


def derive(data, dismissed=(), today=None):
    """``{"skills": [...], "entries": [...]}`` from fetched data (GHX3-GHX7)."""
    login = (data.get("user") or {}).get("login")
    if not isinstance(login, str):
        return {"skills": [], "entries": []}
    enrichment = data.get("enrichment") or {}
    pinned = data.get("pinned") or []
    ranked = rank_projects(login, data.get("repos") or [], pinned, today)
    return {
        "skills": build_skills(eligible_repos(login, data.get("repos") or [], pinned), enrichment, dismissed),
        "entries": build_projects(login, ranked, enrichment),
    }
