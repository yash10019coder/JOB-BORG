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
from collections import Counter
from urllib.parse import quote

import requests
from django.conf import settings

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

    def _get_json(self, path):
        try:
            response = self.session.get(
                f"{API}{path}", headers=self._headers(), timeout=TIMEOUT, allow_redirects=False, stream=True
            )
        except requests.RequestException:
            raise GitHubError("github_unavailable") from None
        try:
            status = response.status_code
            if status == 404:
                raise GitHubError("github_not_found")
            if status in (403, 429) or response.headers.get("X-RateLimit-Remaining") == "0":
                raise GitHubError("github_rate_limited")
            if status != 200:  # includes every 3xx: a redirect is an error, never followed
                raise GitHubError("github_error")
            body = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise GitHubError("github_error")
            try:
                return json.loads(bytes(body))
            except ValueError:
                raise GitHubError("github_error") from None
        finally:
            response.close()

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
