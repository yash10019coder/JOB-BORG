"""Importing from a public GitHub profile (rules GH1-GH5).

Strictly the public REST API, no OAuth and no scraping. The username is
validated before any network call; requests go only to ``https://api.github.com``
with the username URL-quoted, redirects are never followed, the body is size
capped, and only whitelisted JSON fields are ever read. Errors are short codes;
nothing from a response is logged. What it proposes (GitHub URL, a few skill
tags, a portfolio link) is reviewed by the user like any other import.
"""
import json
import re
import time
from collections import Counter
from urllib.parse import quote

import requests
from django.conf import settings

from apps.accounts.importing import github_skills
from apps.accounts.importing.grounding import tag_for_skill
from apps.accounts.importing.validators import GITHUB_RESERVED, ImportValueError, clean_url

API = "https://api.github.com"
USERNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
TIMEOUT = (3, 5)  # connect, read
MAX_BYTES = 256 * 1024
USER_FIELDS = ("login", "html_url", "blog")
REPO_FIELDS = ("name", "html_url", "fork", "archived", "language", "stargazers_count", "homepage")
MAX_REPOS = 30
TOP_LANGUAGES = 5
# Enriched import (GHX1): repo lists are larger than single objects, and the work
# per import is bounded in calls, time and remaining quota.
PAGE_SIZE = 30
MAX_PAGES = 3
LIST_MAX_BYTES = 600 * 1024
MAX_CALLS = 80
MAX_SECONDS = 45
MIN_QUOTA = 10  # stop enriching when GitHub says fewer calls than this remain
_PROFILE_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/]+)/?$", re.I)


class GitHubError(Exception):
    """A GitHub import failed. ``code`` is short and safe to show or log."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def normalize_username(raw):
    """GH1: trim, accept ``@name`` or ``https://github.com/name``, then require a
    valid, non-reserved username. Runs before any I/O."""
    value = (raw or "").strip()
    match = _PROFILE_URL.match(value)
    if match:
        value = match.group(1)
    value = value.lstrip("@")
    if not USERNAME.match(value) or value.lower() in GITHUB_RESERVED:
        raise GitHubError("github_bad_username")
    return value


class GitHubClient:
    """A thin, injectable client (``session`` is replaced in tests)."""

    def __init__(self, session=None, token=None):
        self.session = session or requests.Session()
        self.token = token if token is not None else getattr(settings, "GITHUB_API_TOKEN", "")

    def _headers(self):
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "JobBorg-profile-import",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    remaining = None  # X-RateLimit-Remaining from the latest response, if GitHub sent one
    calls = 0

    def _request(self, path, *, max_bytes=MAX_BYTES, accept=None, payload=None):
        """One call to ``https://api.github.com`` (GET, or a JSON POST when
        ``payload`` is given). The URL is always the API host plus a path built
        from fixed templates, so the token can only ever reach that host."""
        headers = self._headers()
        if accept:
            headers["Accept"] = accept
        self.calls += 1
        try:
            if payload is None:
                response = self.session.get(
                    f"{API}{path}", headers=headers, timeout=TIMEOUT, allow_redirects=False, stream=True
                )
            else:
                response = self.session.post(
                    f"{API}{path}", headers=headers, json=payload, timeout=TIMEOUT, allow_redirects=False, stream=True
                )
        except requests.RequestException:
            raise GitHubError("github_unavailable") from None
        try:
            status = response.status_code
            left = response.headers.get("X-RateLimit-Remaining")
            if isinstance(left, str) and left.isdigit():
                self.remaining = int(left)
            if status == 404:
                raise GitHubError("github_not_found")
            if status in (403, 429) or left == "0":
                raise GitHubError("github_rate_limited")
            if status != 200:  # includes every 3xx: a redirect is an error, never followed
                raise GitHubError("github_error")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise GitHubError("github_error")
            return bytes(body)
        finally:
            response.close()

    def _get_json(self, path, *, max_bytes=MAX_BYTES, payload=None):
        body = self._request(path, max_bytes=max_bytes, payload=payload)
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise GitHubError("github_error") from None

    def fetch(self, username):
        """The whitelisted fields of the user and their recent own repositories."""
        safe = quote(username, safe="")
        user = self._get_json(f"/users/{safe}")
        repos = self._get_json(f"/users/{safe}/repos?type=owner&sort=pushed&per_page={MAX_REPOS}")
        if not isinstance(user, dict) or not isinstance(repos, list):
            raise GitHubError("github_error")
        return {
            "user": {key: user.get(key) for key in USER_FIELDS},
            "repos": [
                {key: repo.get(key) for key in REPO_FIELDS} for repo in repos[:MAX_REPOS] if isinstance(repo, dict)
            ],
        }


    # ------------------------------------------------------------------
    # Enriched profile (rules GHX1, GHX2): projects and skills evidence
    # ------------------------------------------------------------------
    def fetch_profile(self, username, *, full=None, today=None, clock=time.monotonic):
        """The user and their repositories, plus (in full mode) an in-depth read of
        the best few. ``full`` defaults to "a token is configured".

        The first two calls (the user, the first repo page) failing fails the
        import. After that nothing fails it: a call that errors is "no data from
        that repo or source", and a rate limit, the call cap or the time budget
        ends enrichment early and marks the result ``partial``.
        """
        full = bool(self.token) if full is None else bool(full and self.token)
        started = clock()
        safe = quote(username, safe="")
        user = self._get_json(f"/users/{safe}")
        repos = self._repo_pages(safe)
        if not isinstance(user, dict):
            raise GitHubError("github_error")
        data = {
            "user": {key: user.get(key) for key in USER_FIELDS},
            "repos": repos,
            "enrichment": {},
            "pinned": [],
            "meta": {"mode": "light", "scanned": len(repos)},
        }
        login = data["user"].get("login")
        if full and isinstance(login, str) and USERNAME.match(login):
            complete = self._enrich(data, login, started, clock, today)
            data["meta"]["mode"] = "full" if complete else "partial"
        data["meta"]["calls"] = self.calls
        return data

    def _repo_pages(self, safe):
        repos = []
        for page in range(1, MAX_PAGES + 1):
            items = self._get_json(
                f"/users/{safe}/repos?type=owner&sort=pushed&per_page={PAGE_SIZE}&page={page}", max_bytes=LIST_MAX_BYTES
            )
            if not isinstance(items, list):
                raise GitHubError("github_error")
            for item in items:
                if isinstance(item, dict):
                    cleaned = _rich_repo(item)
                    if cleaned:
                        repos.append(cleaned)
            if len(items) < PAGE_SIZE:
                break
        return repos

    def _out_of_budget(self, started, clock):
        if self.calls >= MAX_CALLS or clock() - started >= MAX_SECONDS:
            return True
        return self.remaining is not None and self.remaining < MIN_QUOTA

    def _enrich(self, data, login, started, clock, today):
        """Read the shortlist in depth. ``True`` when it ran to completion."""
        safe = quote(login, safe="")
        try:
            if self._out_of_budget(started, clock):
                return False
            data["pinned"] = self._pinned(login)
        except GitHubError as exc:
            if exc.code == "github_rate_limited":
                return False
        ranked = github_skills.rank_projects(login, data["repos"], data["pinned"], today)
        for repo in ranked[: github_skills.ENRICH_REPOS]:
            name = quote(repo["name"], safe="")
            found = {"languages": {}, "files": [], "manifests": {}}
            data["enrichment"][repo["name"]] = found
            for step in (self._languages, self._files, self._manifests):
                if self._out_of_budget(started, clock):
                    return False
                try:
                    step(safe, name, found, lambda: self._out_of_budget(started, clock))
                except _OutOfBudget:
                    return False
                except GitHubError as exc:
                    if exc.code == "github_rate_limited":
                        return False
                    # any other error: no data from this source; carry on
        return True

    def _pinned(self, login):
        query = (
            "query($l:String!){user(login:$l){pinnedItems(first:6,types:REPOSITORY)"
            "{nodes{... on Repository{name}}}}}"
        )
        reply = self._get_json("/graphql", payload={"query": query, "variables": {"l": login}})
        try:
            nodes = reply["data"]["user"]["pinnedItems"]["nodes"]
        except (KeyError, TypeError):
            return []
        names = [n.get("name") for n in nodes if isinstance(n, dict)]
        return [n for n in names if isinstance(n, str) and github_skills.REPO_NAME.match(n)]

    def _languages(self, safe, name, found, over):
        reply = self._get_json(f"/repos/{safe}/{name}/languages")
        if isinstance(reply, dict):
            found["languages"] = {
                k: v for k, v in reply.items() if isinstance(k, str) and len(k) <= 40 and isinstance(v, int)
            }

    def _files(self, safe, name, found, over):
        reply = self._get_json(f"/repos/{safe}/{name}/contents/", max_bytes=LIST_MAX_BYTES)
        if isinstance(reply, list):
            found["files"] = [
                item["name"] for item in reply[:300]
                if isinstance(item, dict) and item.get("type") == "file"
                and isinstance(item.get("name"), str) and len(item["name"]) <= 100
            ]

    def _manifests(self, safe, name, found, over):
        for filename in found["files"]:
            if filename not in github_skills.MANIFESTS:
                continue
            if over():
                raise _OutOfBudget()
            try:
                body = self._request(
                    f"/repos/{safe}/{name}/contents/{quote(filename, safe='')}",
                    max_bytes=github_skills.MAX_MANIFEST_BYTES, accept="application/vnd.github.raw+json",
                )
            except GitHubError as exc:
                if exc.code == "github_rate_limited":
                    raise
                continue  # missing, redirected, oversized or unreadable: no manifest
            found["manifests"][filename] = body.decode("utf-8", "ignore")


class _OutOfBudget(Exception):
    """Internal: the call cap or time budget ended enrichment."""


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else None


def _rich_repo(item):
    """The whitelisted fields of one repository, or ``None`` if its name could not
    be used to build a request path."""
    name = item.get("name")
    if not isinstance(name, str) or not github_skills.REPO_NAME.match(name):
        return None
    topics = item.get("topics")
    cleaned = {key: item.get(key) for key in REPO_FIELDS}
    cleaned.update(
        description=_text(item.get("description"), 1000),
        topics=[t[:50] for t in topics[:30] if isinstance(t, str)] if isinstance(topics, list) else [],
        created_at=_text(item.get("created_at"), 40), pushed_at=_text(item.get("pushed_at"), 40),
        size=item.get("size") if isinstance(item.get("size"), int) and not isinstance(item.get("size"), bool) else None,
        is_template=bool(item.get("is_template")),
    )
    return cleaned


def _owns(login, url):
    return isinstance(url, str) and re.fullmatch(
        rf"https://github\.com/{re.escape(login)}/[A-Za-z0-9._-]+", url, re.I
    ) is not None


def propose(data):
    """GH4: ``{field: {"value", "snippet", "extractor"}}`` from fetched data.

    * ``github_url`` only if the API's own ``html_url`` is that user's profile;
    * ``target_tags``: the top languages across non-fork, non-archived
      repositories, kept only where they map to a live job-matching tag;
    * ``portfolio_url``: the user's ``blog`` if it is a valid https link, else
      the most-starred own repository.
    """
    user, repos = data["user"], data["repos"]
    login = user.get("login")
    if not isinstance(login, str) or not USERNAME.match(login):
        return {}
    proposals = {}

    profile_url = user.get("html_url")
    if isinstance(profile_url, str) and profile_url.rstrip("/").lower() == f"https://github.com/{login}".lower():
        try:
            proposals["github_url"] = {
                "value": clean_url(f"https://github.com/{login}", kind="github"),  # canonical case from the API
                "snippet": f"GitHub profile of {login}",
                "extractor": "github",
            }
        except ImportValueError:
            pass

    own = [r for r in repos if not r.get("fork") and not r.get("archived")]
    counts = Counter(r["language"] for r in own if isinstance(r.get("language"), str))
    top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:TOP_LANGUAGES]
    tags = []
    for language, _ in top:
        tag = tag_for_skill(language)
        if tag and tag not in tags:
            tags.append(tag)
    if tags:
        shown = ", ".join(f"{name} ({count})" for name, count in top)
        proposals["target_tags"] = {"value": tags, "snippet": f"Top languages: {shown}"[:80], "extractor": "github"}

    link, source = None, ""
    blog = user.get("blog")
    if isinstance(blog, str) and blog.strip():
        try:
            link, source = clean_url(blog.strip()), "Website on the GitHub profile"
        except ImportValueError:
            link = None
    if link is None:
        starred = [r for r in own if isinstance(r.get("stargazers_count"), int) and r["stargazers_count"] > 0
                   and _owns(login, r.get("html_url"))]
        if starred:
            best = max(starred, key=lambda r: (r["stargazers_count"], r.get("name") or ""))
            link, source = clean_url(best["html_url"]), f"Most-starred repository ({best['stargazers_count']} stars)"
    if link and not link.lower().startswith(("https://www.linkedin.com", "https://linkedin.com")):
        proposals["portfolio_url"] = {"value": link, "snippet": source, "extractor": "github"}
    return proposals
