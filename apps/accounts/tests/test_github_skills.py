"""Skills and projects derived from GitHub data (rules GHX3-GHX7), all pure."""
import json
import time
from datetime import date

from django.test import SimpleTestCase

from apps.accounts.importing import github_skills as gs
from apps.accounts.importing import skills_lexicon as lex
from apps.accounts.services import profile_skills
from apps.accounts.services.resume_facts import clean_entry

TODAY = date(2026, 10, 5)
LOGIN = "alice"


def repo(name, **extra):
    data = {
        "name": name, "html_url": f"https://github.com/{LOGIN}/{name}", "fork": False, "archived": False,
        "is_template": False, "language": "Python", "topics": [], "description": "A useful project.",
        "stargazers_count": 0, "size": 120, "created_at": "2021-03-14T10:00:00Z", "pushed_at": "2026-09-02T10:00:00Z",
    }
    data.update(extra)
    return data


def skill_named(result, name):
    return next((s for s in result["skills"] if s["name"] == name), None)


class EligibilityTests(SimpleTestCase):
    def test_GHX3_forks_archived_templates_and_tiny_repos_never_count(self):
        repos = [
            repo("good"), repo("forked", fork=True), repo("old", archived=True),
            repo("tmpl", is_template=True), repo("tiny", size=1), repo("empty", size=0),
        ]
        self.assertEqual([r["name"] for r in gs.eligible_repos(LOGIN, repos)], ["good"])

    def test_GHX3_bad_names_and_missing_dates_are_dropped(self):
        repos = [repo("../evil"), repo("has space"), repo("ok-1"), repo("nodates", created_at=None), repo("baddate", pushed_at="soon")]
        self.assertEqual([r["name"] for r in gs.eligible_repos(LOGIN, repos)], ["ok-1"])

    def test_GHX3_non_dict_entries_are_ignored(self):
        self.assertEqual(gs.eligible_repos(LOGIN, [None, "x", 3, repo("fine")])[0]["name"], "fine")


class SkillEvidenceTests(SimpleTestCase):
    def derive(self, repos, **extra):
        return gs.derive({"user": {"login": LOGIN}, "repos": repos, **extra}, today=TODAY)

    def test_GHX6_primary_languages_and_topics_become_skills_with_evidence(self):
        repos = [repo("a", topics=["django"]), repo("b", language="Go"), repo("c", created_at="2019-03-01T00:00:00Z")]
        result = self.derive(repos)
        python = skill_named(result, "Python")
        self.assertEqual(python["evidence"]["repo_count"], 2)
        self.assertEqual((python["evidence"]["first_seen"], python["evidence"]["last_seen"]), ("2019-03", "2026-09"))
        self.assertEqual((python["evidence"]["scope"], python["evidence"]["scope_size"]), ("recent", 3))
        self.assertIsNotNone(skill_named(result, "Django"))
        self.assertEqual(skill_named(result, "Django")["evidence"]["sources"], ["topic"])

    def test_GHX6_the_proof_line_states_what_it_was_counted_over(self):
        result = self.derive([repo("a"), repo("b", created_at="2019-03-01T00:00:00Z")])
        self.assertEqual(skill_named(result, "Python")["proof"], "Python · 2 of 2 recent repos · first 2019 · last 2026")

    def test_GHX6_a_skill_in_two_or_more_repos_starts_ticked_and_one_does_not(self):
        result = self.derive([repo("a"), repo("b"), repo("c", language="Go")])
        self.assertTrue(skill_named(result, "Python")["default_accept"])
        self.assertFalse(skill_named(result, "Go")["default_accept"])

    def test_GHX6_a_removed_skill_is_flagged_and_never_ticked(self):
        result = gs.derive({"user": {"login": LOGIN}, "repos": [repo("a"), repo("b")]}, dismissed={"python"}, today=TODAY)
        python = skill_named(result, "Python")
        self.assertTrue(python["dismissed"])
        self.assertFalse(python["default_accept"])

    def test_GHX6_evidence_always_satisfies_the_storage_schema(self):
        result = self.derive([repo("a", topics=["react", "docker"]), repo("b")])
        for item in result["skills"]:
            profile_skills.clean_evidence(item["evidence"])

    def test_GHX6_skills_are_ranked_by_repo_count_then_recency_and_capped(self):
        repos = [repo("a"), repo("b"), repo("c", language="Go")]
        names = [s["name"] for s in self.derive(repos)["skills"]]
        self.assertEqual(names, ["Python", "Go"])
        many = [repo(f"r{i}", language=lang) for i, lang in enumerate(list(lex.LEXICON)[:60])]
        self.assertLessEqual(len(self.derive(many)["skills"]), gs.MAX_SKILLS)

    def test_GHX3_noise_and_unknown_languages_propose_nothing(self):
        repos = [repo("a", language=lang) for lang in ("Makefile", "Batchfile", "Procfile", "Totally Unknown Lang", None)]
        self.assertEqual(self.derive(repos)["skills"], [])

    def test_GHX3_github_language_names_map_to_skills(self):
        repos = [repo("a", language="Shell"), repo("b", language="Jupyter Notebook"), repo("c", language="HCL")]
        names = {s["name"] for s in self.derive(repos)["skills"]}
        self.assertEqual(names, {"Bash", "Python", "Terraform"})

    def test_GHX3_an_ineligible_repo_contributes_no_skill(self):
        result = self.derive([repo("fork", fork=True, language="Rust"), repo("tiny", size=0, language="Rust")])
        self.assertEqual(result["skills"], [])

    def test_GHX3_topics_outside_the_lexicon_propose_nothing(self):
        result = self.derive([repo("a", language=None, topics=["hacktoberfest", "my-cool-thing"])])
        self.assertEqual(result["skills"], [])


class EnrichmentTests(SimpleTestCase):
    def derive(self, enrichment, repos=None):
        repos = repos or [repo("a"), repo("b"), repo("c", language="Go")]
        return gs.derive({"user": {"login": LOGIN}, "repos": repos, "enrichment": enrichment}, today=TODAY)

    def test_GHX3_a_language_below_ten_percent_of_a_repo_is_ignored(self):
        result = self.derive({"a": {"languages": {"Python": 9000, "Shell": 500, "Ruby": 1500}}}, [repo("a", language="Python")])
        names = {s["name"] for s in result["skills"]}
        self.assertIn("Ruby", names)
        self.assertNotIn("Bash", names)

    def test_GHX5_framework_skills_come_from_dependency_files_over_the_shortlist(self):
        manifest = json.dumps({"dependencies": {"react": "18", "left-pad": "1"}, "devDependencies": {"jest": "29"}})
        enrichment = {"a": {"manifests": {"package.json": manifest}}, "b": {"manifests": {"package.json": manifest}}}
        result = self.derive(enrichment)
        react = skill_named(result, "React")
        self.assertEqual((react["evidence"]["repo_count"], react["evidence"]["scope"], react["evidence"]["scope_size"]), (2, "top", 2))
        self.assertEqual(react["proof"], "React · 2 of your top 2 repos · first 2021 · last 2026")
        self.assertTrue(react["default_accept"])
        self.assertIsNotNone(skill_named(result, "Jest"))
        self.assertIsNone(skill_named(result, "left-pad"))

    def test_GHX5_a_dockerfile_in_the_root_proposes_docker(self):
        result = self.derive({"a": {"files": ["Dockerfile", "README.md"]}})
        self.assertEqual(skill_named(result, "Docker")["evidence"]["sources"], ["package"])

    def test_GHX6_a_skill_seen_by_language_and_manifest_is_counted_over_one_set(self):
        manifest = "django==4.2\n"
        result = self.derive({"a": {"manifests": {"requirements.txt": manifest}}})
        python = skill_named(result, "Python")
        self.assertEqual(python["evidence"]["scope"], "recent")
        self.assertLessEqual(python["evidence"]["repo_count"], python["evidence"]["scope_size"])
        self.assertEqual(skill_named(result, "Django")["evidence"]["scope"], "top")

    def test_GHX5_every_package_maps_to_a_known_lexicon_skill(self):
        for package, skill in lex.PACKAGE_SKILLS.items():
            self.assertIn(skill, lex.LEXICON, package)
        for skill in list(lex.FILE_SKILLS.values()) + list(lex.LANGUAGE_SKILLS.values()):
            self.assertIn(skill, lex.LEXICON)

    def test_GHX5_package_names_match_exactly_or_by_go_module_prefix(self):
        self.assertEqual(lex.package_skill("Django"), "Django")
        self.assertEqual(lex.package_skill("scikit_learn"), "scikit-learn")
        self.assertEqual(lex.package_skill("github.com/aws/aws-sdk-go/service/s3"), "AWS")
        self.assertIsNone(lex.package_skill("django-extra-unknown"))
        self.assertIsNone(lex.package_skill("evil.com/aws/aws-sdk-go/x"))
        self.assertIsNone(lex.package_skill(""))


class ManifestParsingTests(SimpleTestCase):
    def names(self, filename, text):
        return gs.parse_manifest(filename, text)

    def test_GHX4_each_manifest_type_is_read(self):
        self.assertEqual(sorted(self.names("package.json", json.dumps({"dependencies": {"a": "1"}, "devDependencies": {"b": "1"}}))), ["a", "b"])
        self.assertEqual(self.names("composer.json", json.dumps({"require": {"laravel/framework": "^10"}})), ["laravel/framework"])
        self.assertEqual(self.names("requirements.txt", "Django>=4.2\n# note\n-r other.txt\nrequests[security]==2\n"), ["Django", "requests"])
        toml = '[project]\ndependencies = ["fastapi>=0.1", "uvicorn[standard]"]\n[tool.poetry.dependencies]\npython = "^3.11"\nflask = "*"\n'
        self.assertEqual(sorted(self.names("pyproject.toml", toml)), ["fastapi", "flask", "uvicorn"])
        gomod = "module x\n\nrequire (\n\tgithub.com/lib/pq v1.10.0\n\tgoogle.golang.org/grpc v1.5.0 // indirect\n)\n"
        self.assertEqual(self.names("go.mod", gomod), ["github.com/lib/pq", "google.golang.org/grpc"])
        self.assertEqual(self.names("Gemfile", "source 'x'\ngem 'rails', '~> 7'\n  gem \"pg\"\n"), ["rails", "pg"])
        self.assertEqual(self.names("pom.xml", "<project><artifactId>app</artifactId><artifactId>junit</artifactId></project>"), ["app", "junit"])
        self.assertEqual(self.names("build.gradle", "implementation 'org.springframework.boot:spring-boot-starter:3.1'\n"), ["spring-boot-starter"])

    def test_GHX4_malformed_input_yields_nothing_not_an_error(self):
        for filename, text in (
            ("package.json", "{not json"), ("package.json", "[1, 2]"), ("package.json", json.dumps({"dependencies": ["x"]})),
            ("pyproject.toml", "= = ="), ("pyproject.toml", "[project]\ndependencies = 5\n"), ("go.mod", "\x00\x01"),
            ("package.json", None), ("package.json", 5), ("unknown.txt", "x"),
        ):
            self.assertEqual(self.names(filename, text), [], f"{filename}: {text!r}")

    def test_GHX4_hostile_nesting_and_huge_lines_are_rejected_quickly(self):
        started = time.monotonic()
        self.assertEqual(self.names("package.json", "[" * 60000), [])
        self.assertEqual(self.names("package.json", '{"a":' * 5000 + "1" + "}" * 5000), [])
        self.assertEqual(self.names("pyproject.toml", "a = " + "[" * 30000), [])
        self.assertEqual(self.names("requirements.txt", "a" * (gs.MAX_LINE + 1)), [])
        self.assertEqual(self.names("go.mod", "github.com/" + "a/" * 20000), [])
        self.assertEqual(self.names("pom.xml", "<artifactId>" * 5000), [])
        self.assertLess(time.monotonic() - started, 2)

    def test_GHX4_regex_stress_input_is_linear(self):
        started = time.monotonic()
        text = ("gem '" + "a" * 1900 + "\n") * 30
        self.assertEqual(self.names("Gemfile", text), [])
        text = "implementation '" + "a:" * 900 + "\n"
        self.assertEqual(self.names("build.gradle", text), [])
        self.assertLess(time.monotonic() - started, 1)

    def test_GHX4_an_oversized_manifest_is_ignored(self):
        self.assertEqual(self.names("requirements.txt", "django\n" * 20000), [])

    def test_GHX4_manifest_skills_ignore_unknown_packages_and_dedupe(self):
        self.assertEqual(gs.manifest_skills("requirements.txt", "django\nDjango==4\nunknown-lib\nflask\n"), ["Django", "Flask"])


class ProjectTests(SimpleTestCase):
    def projects(self, repos, **extra):
        data = {"user": {"login": LOGIN}, "repos": repos, **extra}
        return gs.derive(data, today=TODAY)["entries"]

    def test_GHX7_projects_are_own_repos_only_and_start_unticked(self):
        entries = self.projects([repo("good"), repo("forked", fork=True), repo("old", archived=True)])
        self.assertEqual([e["title"] for e in entries], ["good"])
        self.assertFalse(entries[0]["default_accept"])
        self.assertEqual((entries[0]["kind"], entries[0]["organization"], entries[0]["extractor"]), ("project", "", "github"))

    def test_GHX7_pinned_first_then_stars_and_recency(self):
        repos = [
            repo("quiet"), repo("starred", stargazers_count=500),
            repo("stale", pushed_at="2020-01-01T00:00:00Z", stargazers_count=500), repo("pinned"),
        ]
        order = [e["title"] for e in self.projects(repos, pinned=["pinned"])]
        # pinned leads; many stars outweigh being a year stale; recency breaks the rest
        self.assertEqual(order, ["pinned", "starred", "stale", "quiet"])

    def test_GHX7_a_pinned_repo_that_is_not_eligible_is_ignored(self):
        order = [e["title"] for e in self.projects([repo("aa"), repo("forked", fork=True)], pinned=["forked"])]
        self.assertEqual(order, ["aa"])

    def test_GHX7_at_most_25_projects(self):
        self.assertEqual(len(self.projects([repo(f"repo-{i:02d}") for i in range(40)])), gs.MAX_PROJECTS)

    def test_GHX7_the_url_is_rebuilt_never_copied_from_the_response(self):
        entry = self.projects([repo("site", html_url="https://evil.example/phish")])[0]
        self.assertEqual(entry["url"], "https://github.com/alice/site")

    def test_GHX7_dates_come_from_creation_and_last_push_and_end_never_precedes_start(self):
        entry = self.projects([repo("aa", created_at="2022-05-01T00:00:00Z", pushed_at="2023-02-10T00:00:00Z")])[0]
        self.assertEqual((entry["start"], entry["end"]), ("2022-05", "2023-02"))
        odd = self.projects([repo("bb", created_at="2024-05-01T00:00:00Z", pushed_at="2023-02-10T00:00:00Z")])[0]
        self.assertEqual((odd["start"], odd["end"]), ("2024-05", "2024-05"))

    def test_GHX7_names_that_clean_entry_would_reject_are_dropped(self):
        entries = self.projects([repo("x"), repo("trailing."), repo("a" * 81), repo("fine-name")])
        self.assertEqual([e["title"] for e in entries], ["fine-name"])

    def test_GHX7_every_proposal_passes_clean_entry(self):
        entries = self.projects([repo("aa", description="<b>Hi</b>‮ there"), repo("bb", description=None), repo("cc", topics=["react"])])
        self.assertTrue(entries)
        for entry in entries:
            clean_entry(entry, TODAY)

    def test_GHX7_each_project_lists_only_its_own_skills(self):
        entries = {e["title"]: e for e in self.projects([repo("py"), repo("golang", language="Go", topics=["docker"])])}
        self.assertEqual(entries["py"]["skills"], ["Python"])
        self.assertEqual(entries["golang"]["skills"], ["Docker", "Go"])

    def test_GHX7_the_snippet_summarizes_stars_and_activity(self):
        entry = self.projects([repo("aa", stargazers_count=12)])[0]
        self.assertEqual(entry["snippet"], "★ 12 · updated Sep 2026")
        self.assertEqual(self.projects([repo("bb")])[0]["snippet"], "updated Sep 2026")

    def test_GHX7_a_response_without_a_login_proposes_nothing(self):
        self.assertEqual(gs.derive({"user": {}, "repos": [repo("a")]}), {"skills": [], "entries": []})
