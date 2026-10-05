"""GitHub import (rules GH1-GH5, A1, P3): strict network behaviour, whitelisted data."""
import json
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from apps.accounts import tasks
from apps.accounts.importing import github, review, service
from apps.accounts.models import ImportJob, Profile

User = get_user_model()
SENTINEL = "ZXQ-GITHUB-SENTINEL"


class FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks if chunks is not None else [body]
        self.closed = False

    def iter_content(self, chunk_size=8192):
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeSession:
    """Answers by URL path; records every request so tests can assert none was made."""

    def __init__(self, routes=None, error=None):
        self.routes, self.error, self.calls = routes or {}, error, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        for fragment, response in self.routes.items():
            if fragment in url:
                return response() if callable(response) else response
        return FakeResponse(404)


def jr(payload, status=200, headers=None):
    return FakeResponse(status, json.dumps(payload).encode(), headers)


USER = {"login": "octocat", "html_url": "https://github.com/octocat", "blog": "https://octo.dev", "email": "secret@x.com", "bio": "x"}


def repo(name="r", language="Python", fork=False, archived=False, stars=0, **extra):
    return {"name": name, "html_url": f"https://github.com/octocat/{name}", "fork": fork, "archived": archived,
            "language": language, "stargazers_count": stars, "homepage": None, "owner": {"x": 1}, "secret": "x", **extra}


def session_for(user=None, repos=None):
    return FakeSession({"/repos": jr([repo()] if repos is None else repos), "/users/": jr(USER if user is None else user)})


class UsernameTests(SimpleTestCase):
    def test_GH1_accepted_forms(self):
        for raw in ("octocat", "  octocat ", "@octocat", "https://github.com/octocat", "http://www.github.com/octocat/",
                    "github.com/octocat", "a", "a-b", "A1" + "b" * 37):
            with self.subTest(raw=raw):
                self.assertTrue(github.normalize_username(raw))
        self.assertEqual(github.normalize_username("@https://github.com/octocat".lstrip("@")), "octocat")
        self.assertEqual(github.normalize_username("https://github.com/OctoCat"), "OctoCat")

    def test_GH1_rejected_forms(self):
        for raw in ("", "   ", "-a", "a/b", "../x", "x" * 40, "has space", "josé", "octocat/repo",
                    "https://github.com/octocat/repo", "orgs", "settings", "Login", "a%2Fb", "a;b", "a b", None):
            with self.subTest(raw=raw), self.assertRaises(github.GitHubError) as caught:
                github.normalize_username(raw)
            self.assertEqual(caught.exception.code, "github_bad_username")


class ClientTests(SimpleTestCase):
    def make_client(self, session, token=""):
        return github.GitHubClient(session=session, token=token)

    def test_GH2_requests_go_only_to_the_api_with_strict_parameters(self):
        session = session_for()
        self.make_client(session).fetch("octocat")
        urls = [url for url, _ in session.calls]
        self.assertEqual(urls[0], "https://api.github.com/users/octocat")
        self.assertEqual(urls[1], "https://api.github.com/users/octocat/repos?type=owner&sort=pushed&per_page=30")
        for _, kwargs in session.calls:
            self.assertIs(kwargs["allow_redirects"], False)
            self.assertEqual(kwargs["timeout"], (3, 5))
            self.assertIs(kwargs["stream"], True)
            self.assertNotIn("Authorization", kwargs["headers"])
            self.assertIn("User-Agent", kwargs["headers"])

    def test_GH2_the_username_is_url_quoted_even_if_validation_were_bypassed(self):
        session = session_for()
        self.make_client(session).fetch("a b/../x?y=1#z")
        self.assertEqual(session.calls[0][0], "https://api.github.com/users/a%20b%2F..%2Fx%3Fy%3D1%23z")
        for url, _ in session.calls:
            self.assertTrue(url.startswith("https://api.github.com/users/"))

    def test_GH5_a_token_is_sent_only_when_configured(self):
        session = session_for()
        self.make_client(session, token="tok").fetch("octocat")
        self.assertEqual(session.calls[0][1]["headers"]["Authorization"], "Bearer tok")

    def test_GH2_a_redirect_is_an_error_and_is_never_followed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                session = FakeSession({"/users/": FakeResponse(status, headers={"Location": "https://evil.example/x"})})
                with self.assertRaises(github.GitHubError) as caught:
                    self.make_client(session).fetch("octocat")
                self.assertEqual(caught.exception.code, "github_error")
                self.assertEqual(len(session.calls), 1)
                self.assertNotIn("evil", session.calls[0][0])

    def test_GH2_an_oversized_body_is_refused_and_the_response_closed(self):
        big = FakeResponse(200, chunks=[b"x" * 100_000] * 4)
        session = FakeSession({"/users/": big})
        with self.assertRaises(github.GitHubError) as caught:
            self.make_client(session).fetch("octocat")
        self.assertEqual(caught.exception.code, "github_error")
        self.assertTrue(big.closed)

    def test_GH2_every_response_is_closed(self):
        user, repos = jr(USER), jr([repo()])
        self.make_client(FakeSession({"/repos": repos, "/users/": user})).fetch("octocat")
        self.assertTrue(user.closed and repos.closed)

    def test_GH5_status_codes_map_to_short_codes(self):
        cases = {404: "github_not_found", 403: "github_rate_limited", 429: "github_rate_limited",
                 500: "github_error", 502: "github_error", 401: "github_error"}
        for status, code in cases.items():
            with self.subTest(status=status), self.assertRaises(github.GitHubError) as caught:
                self.make_client(FakeSession({"/users/": FakeResponse(status)})).fetch("octocat")
            self.assertEqual(caught.exception.code, code)

    def test_GH5_an_exhausted_rate_limit_header_counts_even_on_a_200(self):
        session = FakeSession({"/users/": FakeResponse(200, b"{}", {"X-RateLimit-Remaining": "0"})})
        with self.assertRaises(github.GitHubError) as caught:
            self.make_client(session).fetch("octocat")
        self.assertEqual(caught.exception.code, "github_rate_limited")

    def test_network_failures_are_unavailable(self):
        for error in (requests.ConnectionError("x"), requests.Timeout("x"), requests.exceptions.SSLError("x")):
            with self.subTest(error=type(error).__name__), self.assertRaises(github.GitHubError) as caught:
                self.make_client(FakeSession(error=error)).fetch("octocat")
            self.assertEqual(caught.exception.code, "github_unavailable")

    def test_malformed_bodies_are_errors(self):
        for body in (b"not json", b"[]", b'"str"'):
            with self.subTest(body=body), self.assertRaises(github.GitHubError) as caught:
                self.make_client(FakeSession({"/users/": FakeResponse(200, body)})).fetch("octocat")
            self.assertEqual(caught.exception.code, "github_error")
        with self.assertRaises(github.GitHubError):  # repos must be a list
            self.make_client(FakeSession({"/repos": jr({"a": 1}), "/users/": jr(USER)})).fetch("octocat")

    def test_GH3_only_whitelisted_fields_are_read(self):
        data = self.make_client(session_for(repos=[repo(extra_field="x")])).fetch("octocat")
        self.assertEqual(set(data["user"]), set(github.USER_FIELDS))
        self.assertEqual(set(data["repos"][0]), set(github.REPO_FIELDS))
        self.assertNotIn("email", json.dumps(data))
        self.assertNotIn("secret", json.dumps(data))

    def test_GH3_at_most_thirty_repositories(self):
        data = self.make_client(session_for(repos=[repo(name=f"r{i}") for i in range(50)])).fetch("octocat")
        self.assertEqual(len(data["repos"]), 30)


def data_for(user=None, repos=None):
    return {
        "user": {key: (user or USER).get(key) for key in github.USER_FIELDS},
        "repos": [{key: r.get(key) for key in github.REPO_FIELDS} for r in (repos if repos is not None else [repo()])],
    }


class ProposalTests(SimpleTestCase):
    def values(self, **kwargs):
        return {k: v["value"] for k, v in github.propose(data_for(**kwargs)).items()}

    def test_GH4_the_profile_link_comes_only_from_the_apis_own_html_url(self):
        self.assertEqual(self.values()["github_url"], "https://github.com/octocat")
        self.assertEqual(self.values(user={**USER, "html_url": "https://github.com/OCTOCAT/"})["github_url"], "https://github.com/octocat")
        for bad in ("https://github.com/someone-else", "https://evil.example/octocat", None, "javascript:alert(1)"):
            with self.subTest(bad=bad):
                self.assertNotIn("github_url", self.values(user={**USER, "html_url": bad}))

    def test_GH4_languages_become_tags_from_own_repos_only(self):
        repos = [repo("a", "Python"), repo("b", "Python"), repo("c", "Python"), repo("d", "Go"), repo("e", "Go"),
                 repo("f", "JavaScript"), repo("g", "Rust"), repo("h", "Java"), repo("i", "C"),
                 repo("fork", "Kotlin", fork=True), repo("old", "Ruby", archived=True)]
        proposals = github.propose(data_for(repos=repos))
        self.assertEqual(proposals["target_tags"]["value"], ["python", "golang", "javascript"])
        self.assertIn("Python (3)", proposals["target_tags"]["snippet"])
        self.assertLessEqual(len(proposals["target_tags"]["snippet"]), 80)

    def test_GH4_forks_archived_and_unmapped_languages_give_no_tags(self):
        repos = [repo("a", "Rust"), repo("b", "Python", fork=True), repo("c", "Go", archived=True), repo("d", None)]
        self.assertNotIn("target_tags", self.values(repos=repos))

    def test_GH4_the_blog_is_the_portfolio_and_a_scheme_less_one_is_assumed_https(self):
        self.assertEqual(self.values()["portfolio_url"], "https://octo.dev")
        self.assertEqual(self.values(user={**USER, "blog": "octo.dev"})["portfolio_url"], "https://octo.dev")

    def test_FU4_an_unsafe_or_http_blog_falls_back_to_the_most_starred_own_repository(self):
        repos = [repo("small", stars=1), repo("big", stars=50), repo("forked", stars=999, fork=True)]
        for blog in ("javascript:alert(1)", "http://octo.dev", "https://u:p@octo.dev", "", None, "   "):
            with self.subTest(blog=blog):
                self.assertEqual(self.values(user={**USER, "blog": blog}, repos=repos)["portfolio_url"],
                                 "https://github.com/octocat/big")

    def test_GH4_no_blog_and_no_starred_repository_means_no_portfolio(self):
        self.assertNotIn("portfolio_url", self.values(user={**USER, "blog": ""}, repos=[repo(stars=0)]))

    def test_GH4_a_repository_url_that_is_not_the_owners_is_never_used(self):
        foreign = repo("x", stars=9)
        foreign["html_url"] = "https://github.com/someone-else/x"
        self.assertNotIn("portfolio_url", self.values(user={**USER, "blog": ""}, repos=[foreign]))

    def test_a_linkedin_blog_is_not_a_portfolio(self):
        self.assertNotIn("portfolio_url", self.values(user={**USER, "blog": "https://www.linkedin.com/in/octo-cat"}, repos=[]))

    def test_an_implausible_login_yields_nothing(self):
        for login in ("../x", "", None, "a b", 5):
            with self.subTest(login=login):
                self.assertEqual(github.propose(data_for(user={**USER, "login": login})), {})

    def test_snippets_are_short_and_describe_the_source(self):
        for proposal in github.propose(data_for(repos=[repo(stars=3)])).values():
            self.assertLessEqual(len(proposal["snippet"]), 80)
            self.assertEqual(proposal["extractor"], "github")


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        self.schedule = patcher.start()
        self.profile = User.objects.create_user(username="alice", password="pw").profile
        self.other = User.objects.create_user(username="bob", password="pw").profile
        self.schedule.reset_mock()

    def fake_github(self, session=None):
        client = github.GitHubClient(session=session or session_for(), token="")
        return mock.patch("apps.accounts.importing.github.GitHubClient", return_value=client)

    def import_user(self, username="octocat", profile=None, session=None):
        with self.fake_github(session), self.captureOnCommitCallbacks(execute=True):
            job = service.start_github_import(profile or self.profile, username)
        job.refresh_from_db()
        return job


class ServiceTests(_Base):
    def test_GH1_a_bad_username_is_refused_before_any_job_rate_count_or_request(self):
        session = session_for()
        with self.fake_github(session), self.assertRaises(github.GitHubError):
            service.start_github_import(self.profile, "../etc/passwd")
        self.assertEqual((ImportJob.objects.count(), session.calls), (0, []))
        self.assertIsNone(cache.get(service._rate_key(self.profile.user_id, "github")))

    def test_a_ready_job_holds_only_proposals_and_marks_github_as_the_source(self):
        job = self.import_user()
        self.assertEqual((job.status, job.kind, job.source_kind, job.extractor), ("ready", "github", "github", "rule"))
        self.assertEqual(set(job.payload["fields"]), {"github_url", "target_tags", "portfolio_url"})
        self.assertEqual(job.payload["entries"], [])
        self.assertEqual(job.payload["meta"], {"source": "github"})
        self.assertNotIn("secret", json.dumps(job.payload))

    def test_GH5_failures_become_coded_failed_jobs(self):
        cases = {404: "github_not_found", 403: "github_rate_limited", 500: "github_error"}
        for status, code in cases.items():
            with self.subTest(code=code):
                ImportJob.objects.all().delete()
                job = self.import_user(session=FakeSession({"/users/": FakeResponse(status)}))
                self.assertEqual((job.status, job.error_code, job.payload), ("failed", code, {}))
                self.assertTrue(service.error_message(code))

    def test_a_profile_with_nothing_usable_is_nothing_found(self):
        job = self.import_user(session=session_for(user={"login": "octocat", "html_url": "https://github.com/x", "blog": ""}, repos=[]))
        self.assertEqual((job.status, job.error_code), ("failed", "nothing_found"))

    def test_P3_an_unexpected_error_is_coded_and_its_text_is_never_logged(self):
        class Boom(github.GitHubClient):
            def fetch(self, username):
                raise ValueError(SENTINEL)

        with mock.patch("apps.accounts.importing.github.GitHubClient", return_value=Boom(session=FakeSession())):
            with self.assertLogs("apps.accounts.importing.service", level="INFO") as logs, self.captureOnCommitCallbacks(execute=True):
                job = service.start_github_import(self.profile, "octocat")
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), ("failed", "unexpected"))
        self.assertNotIn(SENTINEL, "\n".join(logs.output))
        self.assertIn("ValueError", "\n".join(logs.output))

    def test_A1_one_in_flight_import_per_profile_across_both_kinds(self):
        with mock.patch("apps.accounts.tasks.run_github_import"):
            service.start_github_import(self.profile, "octocat")
            with self.assertRaises(service.ImportInProgress):
                service.start_github_import(self.profile, "octocat")
            service.start_github_import(self.other, "octocat")

    def test_A1_github_imports_have_their_own_hourly_limit(self):
        with mock.patch("apps.accounts.tasks.run_github_import"), self.settings(PROFILE_IMPORT_RATE_PER_HOUR=2):
            for _ in range(2):
                job = service.start_github_import(self.profile, "octocat")
                ImportJob.objects.filter(pk=job.pk).update(status=ImportJob.Status.APPLIED)
            with self.assertRaises(service.ImportRateLimited):
                service.start_github_import(self.profile, "octocat")
            service.check_rate_limit(self.profile.user_id, "document")  # documents are counted separately

    def test_the_task_is_idempotent_and_wired(self):
        job = self.import_user()
        with self.fake_github():
            self.assertIsNone(tasks.run_github_import(str(job.public_id), "octocat"))
        self.assertEqual(tasks.run_github_import.name, "apps.accounts.run_github_import")

    def test_P4_a_github_import_never_creates_learning_data(self):
        from apps.accounts.models import AnswerBank, AnswerObservation, ProfileSuggestion

        self.import_user()
        for model in (AnswerBank, AnswerObservation, ProfileSuggestion):
            self.assertEqual(model.objects.count(), 0)


class ReviewAndApplyTests(_Base):
    def post_for(self, job):
        post = {}
        for key, proposal in job.payload["fields"].items():
            value = proposal["value"]
            post[f"decision__{key}"] = "accept"
            post[f"value__{key}"] = ", ".join(value) if isinstance(value, list) else value
        return post

    def test_accepted_github_values_are_saved_as_imported_with_one_rematch(self):
        job = self.import_user()
        outcome = review.apply_review(job, self.post_for(job))
        profile = Profile.objects.get(pk=self.profile.pk)
        self.assertEqual(profile.github_url, "https://github.com/octocat")
        self.assertEqual(profile.portfolio_url, "https://octo.dev")
        self.assertEqual(profile.target_tags, ["python"])
        self.assertEqual({profile.field_provenance[k]["source"] for k in ("github_url", "portfolio_url", "target_tags")}, {"imported"})
        self.assertEqual(len(outcome.applied), 3)
        self.assertEqual(self.schedule.call_count, 1)

    def test_values_the_user_already_set_are_kept(self):
        Profile.objects.filter(pk=self.profile.pk).update(portfolio_url="https://mine.example", target_tags=["rust"])
        job = self.import_user()
        review.apply_review(job, self.post_for(job))
        profile = Profile.objects.get(pk=self.profile.pk)
        self.assertEqual((profile.portfolio_url, profile.target_tags), ("https://mine.example", ["rust"]))
        self.assertEqual(profile.github_url, "https://github.com/octocat")

    def test_the_review_page_rows_for_a_github_job_have_no_entries(self):
        job = self.import_user()
        built = review.build_review(job)
        self.assertEqual(built.entries, [])
        self.assertEqual({row.key for row in built.fields}, {"github_url", "target_tags", "portfolio_url"})
