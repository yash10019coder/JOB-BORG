"""The enriched GitHub fetch (rules GHX1, GHX2, GHX4): bounded, best-effort, host-pinned."""
import json
from datetime import date
from unittest import mock

from django.test import SimpleTestCase

from apps.accounts.importing import github, github_skills
from apps.accounts.tests.test_import_github import FakeResponse, jr

TODAY = date(2026, 10, 5)
API = "https://api.github.com"
USER = {"login": "octocat", "html_url": "https://github.com/octocat", "blog": "", "email": "private@example.com"}


def repo(name, **extra):
    data = {
        "name": name, "html_url": f"https://github.com/octocat/{name}", "fork": False, "archived": False,
        "language": "Python", "stargazers_count": 0, "homepage": None, "description": "A project.", "topics": ["django"],
        "created_at": "2021-03-14T10:00:00Z", "pushed_at": "2026-09-02T10:00:00Z", "size": 100, "is_template": False,
        "owner": {"x": 1}, "secret": "x",
    }
    data.update(extra)
    return data


class RoutedSession:
    """Answers GET and POST by the longest matching URL fragment; records everything."""

    def __init__(self, routes=None, post=None, default=None):
        self.routes, self.post_reply, self.default = routes or {}, post, default
        self.calls, self.posts = [], []

    def _answer(self, url):
        for fragment in sorted(self.routes, key=len, reverse=True):
            if fragment in url:
                reply = self.routes[fragment]
                return reply() if callable(reply) else reply
        return self.default() if callable(self.default) else (self.default or FakeResponse(404))

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._answer(url)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.post_reply() if callable(self.post_reply) else (self.post_reply or FakeResponse(404))

    def urls(self):
        return [url for url, _ in self.calls] + [url for url, _ in self.posts]


def full_session(repos=None, manifest=None, pinned=None, **extra_routes):
    repos = repos if repos is not None else [repo("alpha"), repo("beta")]
    routes = {
        "/users/octocat": jr(USER),
        "/users/octocat/repos": jr(repos),
        "/languages": jr({"Python": 9000, "Shell": 100}),
        "/contents/": jr([{"name": "package.json", "type": "file"}, {"name": "src", "type": "dir"}, {"name": "Dockerfile", "type": "file"}]),
        "/contents/package.json": FakeResponse(200, (manifest or json.dumps({"dependencies": {"react": "18"}})).encode()),
        **extra_routes,
    }
    reply = jr({"data": {"user": {"pinnedItems": {"nodes": [{"name": n} for n in (pinned or [])]}}}})
    return RoutedSession(routes, post=reply)


def client(session, token="tok"):
    return github.GitHubClient(session=session, token=token)


class LightModeTests(SimpleTestCase):
    def test_GHX1_without_a_token_only_the_user_and_repo_pages_are_read(self):
        session = full_session()
        data = client(session, token="").fetch_profile("octocat", today=TODAY)
        self.assertEqual(data["meta"]["mode"], "light")
        self.assertEqual(data["enrichment"], {})
        self.assertEqual(session.posts, [])
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0][0], f"{API}/users/octocat")
        self.assertEqual(session.calls[1][0], f"{API}/users/octocat/repos?type=owner&sort=pushed&per_page=30&page=1")

    def test_GHX2_asking_for_full_mode_without_a_token_is_still_light(self):
        session = full_session()
        data = client(session, token="").fetch_profile("octocat", full=True)
        self.assertEqual(data["meta"]["mode"], "light")
        self.assertEqual(session.posts, [])

    def test_GHX1_a_full_page_fetches_the_next_one_up_to_three_pages(self):
        page = jr([repo(f"r{i:02d}") for i in range(30)])
        session = RoutedSession({"/users/octocat": jr(USER), "/users/octocat/repos": page})
        data = client(session, token="").fetch_profile("octocat")
        repo_calls = [u for u in session.urls() if "/repos?" in u]
        self.assertEqual(len(repo_calls), 3)
        self.assertTrue(repo_calls[2].endswith("page=3"))
        self.assertEqual(len(data["repos"]), 90)

    def test_GHX1_whitelisted_fields_only_and_types_are_checked(self):
        item = repo("alpha", topics="not-a-list", size=True, description="x" * 5000, created_at=123)
        data = client(full_session([item]), token="").fetch_profile("octocat")
        stored = data["repos"][0]
        self.assertNotIn("secret", stored)
        self.assertNotIn("owner", stored)
        self.assertEqual((stored["topics"], stored["size"], stored["created_at"]), ([], None, None))
        self.assertEqual(len(stored["description"]), 1000)
        self.assertNotIn("email", data["user"])

    def test_GHX1_repos_whose_names_could_change_a_request_path_are_dropped(self):
        repos = [repo("ok-repo"), repo("../etc"), repo("a/b"), repo("x y"), repo(".."), repo("")]
        data = client(full_session(repos), token="").fetch_profile("octocat")
        self.assertEqual([r["name"] for r in data["repos"]], ["ok-repo"])

    def test_GHX1_the_repo_list_cap_is_larger_than_the_single_object_cap(self):
        big = json.dumps([repo(f"r{i:02d}", description="d" * 9000) for i in range(30)]).encode()
        self.assertGreater(len(big), github.MAX_BYTES)
        self.assertLess(len(big), github.LIST_MAX_BYTES)
        session = RoutedSession({"/users/octocat": jr(USER), "/users/octocat/repos": FakeResponse(200, big)})
        self.assertEqual(len(client(session, token="").fetch_profile("octocat")["repos"]), 90)  # 3 pages of 30

    def test_GHX1_a_repo_list_over_the_larger_cap_fails_the_import(self):
        huge = FakeResponse(200, chunks=[b"[" + b" " * github.LIST_MAX_BYTES, b" "])
        session = RoutedSession({"/users/octocat": jr(USER), "/users/octocat/repos": huge})
        with self.assertRaises(github.GitHubError) as caught:
            client(session, token="").fetch_profile("octocat")
        self.assertEqual(caught.exception.code, "github_error")


class FailureSemanticsTests(SimpleTestCase):
    def test_GHX2_the_first_two_calls_failing_fails_the_import(self):
        for routes, code in (
            ({"/users/octocat": FakeResponse(404)}, "github_not_found"),
            ({"/users/octocat": jr(USER), "/users/octocat/repos": FakeResponse(403)}, "github_rate_limited"),
            ({"/users/octocat": jr(USER), "/users/octocat/repos": FakeResponse(500)}, "github_error"),
            ({"/users/octocat": jr([1, 2]), "/users/octocat/repos": jr([])}, "github_error"),
        ):
            with self.subTest(code=code), self.assertRaises(github.GitHubError) as caught:
                client(RoutedSession(routes)).fetch_profile("octocat")
            self.assertEqual(caught.exception.code, code)

    def test_GHX2_one_repo_failing_does_not_fail_the_import(self):
        session = full_session(**{"/repos/octocat/alpha/languages": FakeResponse(404), "/repos/octocat/alpha/contents/": FakeResponse(500)})
        data = client(session).fetch_profile("octocat", today=TODAY)
        self.assertEqual(data["meta"]["mode"], "full")
        self.assertEqual(data["enrichment"]["alpha"]["languages"], {})
        self.assertIn("package.json", data["enrichment"]["beta"]["manifests"])

    def test_GHX2_a_redirect_or_oversized_or_unreadable_manifest_is_simply_not_a_manifest(self):
        for reply in (FakeResponse(302), FakeResponse(200, b"x" * (github_skills.MAX_MANIFEST_BYTES + 1)), FakeResponse(500)):
            session = full_session(**{"/contents/package.json": reply})
            data = client(session).fetch_profile("octocat", today=TODAY)
            self.assertEqual(data["meta"]["mode"], "full")
            self.assertEqual(data["enrichment"]["alpha"]["manifests"], {})

    def test_GHX2_a_rate_limit_during_enrichment_keeps_what_was_gathered_as_partial(self):
        session = full_session(**{"/repos/octocat/beta/languages": FakeResponse(403)})
        data = client(session).fetch_profile("octocat", today=TODAY)
        self.assertEqual(data["meta"]["mode"], "partial")
        self.assertIn("package.json", data["enrichment"]["alpha"]["manifests"])
        self.assertEqual([r["name"] for r in data["repos"]], ["alpha", "beta"])

    def test_GHX2_the_call_cap_ends_enrichment_as_partial(self):
        session = full_session([repo(f"repo-{i}") for i in range(8)])
        with mock.patch.object(github, "MAX_CALLS", 6):
            data = client(session).fetch_profile("octocat", today=TODAY)
        self.assertEqual(data["meta"]["mode"], "partial")
        self.assertLessEqual(len(session.calls) + len(session.posts), 6)

    def test_GHX2_the_time_budget_ends_enrichment_as_partial(self):
        ticks = iter([0, 0, 1, 2, 46, 47, 48, 49, 50, 51, 52])
        data = client(full_session()).fetch_profile("octocat", today=TODAY, clock=lambda: next(ticks))
        self.assertEqual(data["meta"]["mode"], "partial")

    def test_GHX1_low_remaining_quota_stops_enrichment(self):
        low = {"X-RateLimit-Remaining": str(github.MIN_QUOTA - 1)}
        session = full_session(**{"/users/octocat/repos": jr([repo("alpha")], headers=low)})
        data = client(session).fetch_profile("octocat", today=TODAY)
        self.assertEqual(data["meta"]["mode"], "partial")
        self.assertEqual(data["enrichment"], {})

    def test_GHX1_the_remaining_quota_is_exposed_for_the_throttle(self):
        session = full_session(**{"/users/octocat": jr(USER, headers={"X-RateLimit-Remaining": "4321"})})
        c = client(session)
        c.fetch_profile("octocat", today=TODAY)
        self.assertEqual(c.remaining, 4321)


class FullModeTests(SimpleTestCase):
    def test_GHX1_the_shortlist_is_read_in_depth_with_the_documented_calls(self):
        session = full_session()
        data = client(session).fetch_profile("octocat", today=TODAY)
        self.assertEqual(data["meta"]["mode"], "full")
        beta = data["enrichment"]["beta"]
        self.assertEqual(beta["languages"], {"Python": 9000, "Shell": 100})
        self.assertEqual(beta["files"], ["package.json", "Dockerfile"])
        self.assertEqual(list(beta["manifests"]), ["package.json"])
        urls = session.urls()
        self.assertIn(f"{API}/repos/octocat/beta/languages", urls)
        self.assertIn(f"{API}/repos/octocat/beta/contents/", urls)
        self.assertIn(f"{API}/repos/octocat/beta/contents/package.json", urls)
        self.assertEqual(data["meta"]["calls"], len(session.calls) + len(session.posts))

    def test_GHX1_manifests_are_requested_raw_with_the_small_cap(self):
        session = full_session()
        client(session).fetch_profile("octocat", today=TODAY)
        headers = [kw["headers"]["Accept"] for url, kw in session.calls if url.endswith("/package.json")]
        self.assertEqual(headers, ["application/vnd.github.raw+json"] * 2)

    def test_GHX1_at_most_eight_repos_are_read_in_depth(self):
        session = full_session([repo(f"repo-{i:02d}") for i in range(20)])
        data = client(session).fetch_profile("octocat", today=TODAY)
        self.assertEqual(len(data["enrichment"]), github_skills.ENRICH_REPOS)

    def test_GHX1_pinned_repos_come_from_one_graphql_call_and_lead_the_shortlist(self):
        repos = [repo(f"repo-{i:02d}", stargazers_count=100 - i) for i in range(12)] + [repo("pinned-one")]
        session = full_session(repos, pinned=["pinned-one", "../bad"])
        data = client(session).fetch_profile("octocat", today=TODAY)
        self.assertEqual(data["pinned"], ["pinned-one"])
        self.assertEqual(len(session.posts), 1)
        url, kwargs = session.posts[0]
        self.assertEqual(url, f"{API}/graphql")
        self.assertEqual(kwargs["json"]["variables"], {"l": "octocat"})
        self.assertNotIn("octocat", kwargs["json"]["query"])
        self.assertIn("pinned-one", data["enrichment"])

    def test_GHX1_a_broken_graphql_answer_just_means_no_pinned_repos(self):
        for reply in (jr({"errors": [{"message": "x"}]}), jr({"data": None}), FakeResponse(500), jr([1])):
            session = full_session()
            session.post_reply = reply
            data = client(session).fetch_profile("octocat", today=TODAY)
            self.assertEqual(data["pinned"], [])
            self.assertEqual(data["meta"]["mode"], "full")

    def test_GHX1_graphql_is_never_called_without_a_token(self):
        session = full_session()
        client(session, token="").fetch_profile("octocat")
        self.assertEqual(session.posts, [])


class HostSafetyTests(SimpleTestCase):
    def test_GHX1_every_request_goes_to_the_api_host_and_response_urls_are_never_followed(self):
        listing = [
            {"name": "package.json", "type": "file", "download_url": "https://evil.example/steal", "url": "https://evil.example/u",
             "html_url": "https://evil.example/h", "git_url": "https://evil.example/g"},
        ]
        session = full_session(**{"/contents/": jr(listing)})
        client(session, token="secret-token").fetch_profile("octocat", today=TODAY)
        self.assertTrue(session.calls)
        for url in session.urls():
            self.assertTrue(url.startswith(f"{API}/"), url)
        self.assertFalse([u for u in session.urls() if "evil.example" in u])

    def test_GHX10_the_token_goes_in_the_authorization_header_of_api_requests_only(self):
        session = full_session()
        client(session, token="secret-token").fetch_profile("octocat", today=TODAY)
        for url, kwargs in session.calls + session.posts:
            self.assertTrue(url.startswith(API))
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-token")
            self.assertNotIn("secret-token", url)

    def test_GHX1_redirects_are_never_followed_on_any_call(self):
        session = full_session()
        client(session).fetch_profile("octocat", today=TODAY)
        for _, kwargs in session.calls + session.posts:
            self.assertIs(kwargs["allow_redirects"], False)
            self.assertEqual(kwargs["timeout"], (3, 5))

    def test_GHX1_repo_names_are_quoted_in_request_paths(self):
        session = full_session([repo("we.ird_name-1")])
        client(session).fetch_profile("octocat", today=TODAY)
        self.assertIn(f"{API}/repos/octocat/we.ird_name-1/languages", session.urls())


class EndToEndDerivationTests(SimpleTestCase):
    def test_GHX5_a_fetched_package_json_becomes_a_framework_skill_with_scoped_evidence(self):
        data = client(full_session()).fetch_profile("octocat", today=TODAY)
        result = github_skills.derive(data, today=TODAY)
        react = next(s for s in result["skills"] if s["name"] == "React")
        self.assertEqual(react["proof"], "React · 2 of your top 2 repos · first 2021 · last 2026")
        self.assertTrue(react["default_accept"])
        self.assertIn("Docker", {s["name"] for s in result["skills"]})
        self.assertEqual(sorted(e["title"] for e in result["entries"]), ["alpha", "beta"])
