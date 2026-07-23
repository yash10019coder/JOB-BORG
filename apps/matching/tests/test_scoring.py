"""Scorer tests — pure, deterministic, no DB (SimpleTestCase)."""
from django.test import SimpleTestCase

from apps.accounts.models import Profile
from apps.matching.constants import MATCH_SCORE_THRESHOLD, MatchStatus
from apps.matching.scoring import score_job


def profile(**overrides):
    base = {
        "target_titles": [],
        "target_tags": [],
        "target_locations": [],
        "target_locations_normalized": [],
        "excluded_employers": [],
        "min_salary": None,
        "remote_pref": Profile.RemotePref.ANY,
    }
    base.update(overrides)
    return base


def job(**overrides):
    base = {
        "title": "Backend Engineer",
        "classification_tags": [],
        "location": "Remote - US",
        "location_city": "",
        "location_region": "",
        "location_country": "",
        "location_resolved": False,
        "is_remote": True,
        "salary_min": None,
        "salary_max": None,
        "employer_slug": "acme",
    }
    base.update(overrides)
    return base


def resolved_target(*, city=None, region=None, country=None, raw=""):
    return {"raw": raw, "city": city, "region": region, "country": country, "resolved": True}


def unresolved_target(raw):
    return {"raw": raw, "city": None, "region": None, "country": None, "resolved": False}


def no_place_info_target(raw):
    # apps/locations' defined "no place info" state for a bare remote/hybrid
    # target string (e.g. a user typing "Remote" into target_locations) --
    # resolved=True, but every field unset. Distinct from unresolved_target,
    # which apps/locations' disambiguation rules could not resolve at all.
    return {"raw": raw, "city": None, "region": None, "country": None, "resolved": True}


class ScoringTests(SimpleTestCase):
    def test_strong_match_scores_above_threshold_with_correct_matched_tags(self):
        p = profile(
            target_titles=["Backend Engineer"],
            target_tags=["python", "kubernetes"],
            min_salary=100000,
        )
        j = job(
            title="Senior Backend Engineer",
            classification_tags=["python", "kubernetes", "backend"],
            salary_min=160000,
        )
        result = score_job(p, j)
        self.assertGreaterEqual(result.score, MATCH_SCORE_THRESHOLD)
        self.assertEqual(result.status, MatchStatus.RECOMMENDED)
        # matched_tags is exactly the intersection, sorted, no extras.
        self.assertEqual(result.matched_tags, ["kubernetes", "python"])

    def test_zero_tag_overlap_scores_low_below_threshold(self):
        p = profile(target_tags=["rust", "elixir"])
        j = job(title="Marketing Lead", classification_tags=["design"], is_remote=False,
                location="Berlin")
        result = score_job(p, j)
        self.assertEqual(result.matched_tags, [])
        self.assertEqual(result.status, MatchStatus.BELOW_THRESHOLD)

    def test_matched_tags_never_include_tags_absent_from_job(self):
        p = profile(target_tags=["python", "golang", "rust"])
        j = job(classification_tags=["python", "backend"])
        result = score_job(p, j)
        self.assertEqual(result.matched_tags, ["python"])

    def test_score_scales_monotonically_with_tag_overlap(self):
        p = profile(target_tags=["a", "b", "c", "d"])
        scores = []
        for n in range(5):
            j = job(classification_tags=[t for t in ["a", "b", "c", "d"][:n]])
            scores.append(score_job(p, j).score)
        self.assertEqual(scores, sorted(scores))
        self.assertLess(scores[0], scores[-1])

    def test_empty_target_tags_does_not_crash_and_scores_defined(self):
        p = profile(target_tags=[])
        result = score_job(p, job())
        self.assertIsInstance(result.score, float)
        self.assertEqual(result.matched_tags, [])

    def test_below_min_salary_penalized(self):
        p = profile(target_tags=["python"], min_salary=150000)
        low = score_job(p, job(classification_tags=["python"], salary_min=90000))
        high = score_job(p, job(classification_tags=["python"], salary_min=180000))
        self.assertLess(low.score, high.score)

    def test_unknown_salary_handled_neutrally(self):
        p = profile(target_tags=["python"], min_salary=150000)
        unknown = score_job(p, job(classification_tags=["python"], salary_min=None))
        below = score_job(p, job(classification_tags=["python"], salary_min=90000))
        above = score_job(p, job(classification_tags=["python"], salary_min=180000))
        # Unknown sits strictly between a known-below and a known-above job.
        self.assertLess(below.score, unknown.score)
        self.assertLess(unknown.score, above.score)

    def test_remote_only_profile_vs_onsite_job_scores_zero_location(self):
        p = profile(target_tags=["python"], remote_pref=Profile.RemotePref.REMOTE_ONLY)
        remote = score_job(p, job(classification_tags=["python"], is_remote=True))
        onsite = score_job(p, job(classification_tags=["python"], is_remote=False))
        self.assertLess(onsite.score, remote.score)

    def test_identical_inputs_yield_identical_outputs(self):
        p = profile(target_tags=["python"], target_titles=["Backend"])
        j = job(classification_tags=["python"])
        self.assertEqual(score_job(p, j), score_job(p, j))

    def test_score_bounded_between_zero_and_one(self):
        p = profile(
            target_titles=["Backend Engineer"],
            target_tags=["python"],
            min_salary=100000,
        )
        j = job(
            title="Backend Engineer",
            classification_tags=["python"],
            salary_min=200000,
        )
        result = score_job(p, j)
        self.assertGreaterEqual(result.score, 0.0)
        self.assertLessEqual(result.score, 1.0)


class LocationComponentTests(SimpleTestCase):
    """_location_component via score_job -- structured hierarchy matching,
    the ONSITE_ONLY bug fix, and the substring-fallback boundary."""

    def test_onsite_only_now_honors_target_locations(self):
        # The originally-reported bug: ONSITE_ONLY used to ignore
        # target_locations entirely and give full credit to any onsite job.
        p = profile(
            target_tags=["python"],
            remote_pref=Profile.RemotePref.ONSITE_ONLY,
            target_locations_normalized=[resolved_target(country="US", raw="US")],
        )
        matching = score_job(p, job(classification_tags=["python"], is_remote=False,
                                     location_country="US", location_resolved=True))
        other_country = score_job(p, job(classification_tags=["python"], is_remote=False,
                                          location_country="UK", location_resolved=True))
        self.assertLess(other_country.score, matching.score)

    def test_onsite_only_still_zeroes_remote_jobs(self):
        p = profile(target_tags=["python"], remote_pref=Profile.RemotePref.ONSITE_ONLY,
                    target_locations_normalized=[resolved_target(country="US", raw="US")])
        remote_job = score_job(p, job(classification_tags=["python"], is_remote=True))
        onsite_match = score_job(p, job(classification_tags=["python"], is_remote=False,
                                         location_country="US", location_resolved=True))
        self.assertLess(remote_job.score, onsite_match.score)

    def test_remote_only_ignores_target_locations_unchanged(self):
        p = profile(target_tags=["python"], remote_pref=Profile.RemotePref.REMOTE_ONLY)
        remote = score_job(p, job(classification_tags=["python"], is_remote=True))
        onsite = score_job(p, job(classification_tags=["python"], is_remote=False))
        self.assertLess(onsite.score, remote.score)

    def test_any_city_level_target_matches_same_city_only(self):
        p = profile(target_tags=["python"],
                    target_locations_normalized=[resolved_target(
                        city="New York", region="NY", country="US", raw="New York")])
        same_city = score_job(p, job(classification_tags=["python"], is_remote=False,
                                      location_city="New York", location_region="NY",
                                      location_country="US", location_resolved=True))
        other_city_same_region = score_job(p, job(
            classification_tags=["python"], is_remote=False,
            location_city="Albany", location_region="NY", location_country="US",
            location_resolved=True))
        self.assertLess(other_city_same_region.score, same_city.score)

    def test_region_only_target_matches_regardless_of_city(self):
        p = profile(target_tags=["python"],
                    target_locations_normalized=[resolved_target(
                        region="CA", country="US", raw="California")])
        sf = score_job(p, job(classification_tags=["python"], is_remote=False,
                               location_city="San Francisco", location_region="CA",
                               location_country="US", location_resolved=True))
        la = score_job(p, job(classification_tags=["python"], is_remote=False,
                               location_city="Los Angeles", location_region="CA",
                               location_country="US", location_resolved=True))
        other_region = score_job(p, job(classification_tags=["python"], is_remote=False,
                                         location_city="Austin", location_region="TX",
                                         location_country="US", location_resolved=True))
        self.assertEqual(sf.score, la.score)
        self.assertLess(other_region.score, sf.score)

    def test_country_only_target_matches_any_city_in_country(self):
        # The thin-non-US-coverage case: country resolves even when city/region don't.
        p = profile(target_tags=["python"],
                    target_locations_normalized=[resolved_target(country="UK", raw="UK")])
        london = score_job(p, job(classification_tags=["python"], is_remote=False,
                                   location_country="UK", location_resolved=True))
        germany = score_job(p, job(classification_tags=["python"], is_remote=False,
                                    location_country="Germany", location_resolved=True))
        self.assertLess(germany.score, london.score)

    def test_empty_target_locations_normalized_scores_full_location_credit(self):
        p = profile(target_tags=["python"])  # target_locations_normalized=[] by default
        result = score_job(p, job(classification_tags=["python"], is_remote=False,
                                   location_country="US", location_resolved=True))
        # No location constraint stated -- location contributes full credit.
        full_credit = score_job(profile(target_tags=["python"]),
                                 job(classification_tags=["python"], is_remote=True))
        self.assertEqual(result.score, full_credit.score)

    def test_unresolved_target_alongside_resolved_is_inert_not_blocking(self):
        p = profile(target_tags=["python"],
                    target_locations_normalized=[
                        unresolved_target("Xyzzyville"),
                        resolved_target(country="US", raw="US"),
                    ])
        matching = score_job(p, job(classification_tags=["python"], is_remote=False,
                                     location_country="US", location_resolved=True))
        other_country = score_job(p, job(classification_tags=["python"], is_remote=False,
                                          location_country="UK", location_resolved=True))
        self.assertLess(other_country.score, matching.score)

    def test_bare_abbreviation_target_does_not_substring_match_resolved_job(self):
        # Adversarial-review regression: a bare "NY" target (marked
        # resolved=False by apps.locations' disambiguation rules) must NOT
        # fall back to substring-matching "ny" inside "albany" once the job
        # itself is structurally resolved -- that reintroduces the exact bug
        # this scorer exists to fix.
        p = profile(target_tags=["python"],
                    target_locations=["NY"],
                    target_locations_normalized=[unresolved_target("NY")])
        albany_job = score_job(p, job(classification_tags=["python"], is_remote=False,
                                       location="Albany, NY, US", location_city="Albany",
                                       location_region="NY", location_country="US",
                                       location_resolved=True))
        no_location_targets = score_job(profile(target_tags=["python"]),
                                         job(classification_tags=["python"], is_remote=True))
        self.assertLess(albany_job.score, no_location_targets.score)

    def test_both_unresolved_falls_back_to_substring_matching(self):
        p = profile(target_tags=["python"],
                    target_locations=["small town"],
                    target_locations_normalized=[unresolved_target("small town")])
        matching = score_job(p, job(classification_tags=["python"], is_remote=False,
                                     location="Small Town, Nowhere",
                                     location_resolved=False))
        not_matching = score_job(p, job(classification_tags=["python"], is_remote=False,
                                         location="Somewhere Else",
                                         location_resolved=False))
        self.assertLess(not_matching.score, matching.score)

    def test_no_place_info_target_alone_is_treated_as_no_constraint(self):
        # Code-review regression: a profile whose ONLY target_locations
        # entry is a bare remote/hybrid string (e.g. "Remote") normalizes
        # to resolved=True with every field unset. Without a guard,
        # _hierarchy_match treats an all-unset target as a vacuous wildcard
        # (matches any job) rather than "no constraint stated" -- but the
        # correct behavior is the latter, matching the empty-list semantic.
        p = profile(target_tags=["python"],
                    target_locations=["Remote"],
                    target_locations_normalized=[no_place_info_target("Remote")])
        result = score_job(p, job(classification_tags=["python"], is_remote=False,
                                   location_country="US", location_resolved=True))
        no_targets = score_job(profile(target_tags=["python"]),
                                job(classification_tags=["python"], is_remote=False,
                                    location_country="US", location_resolved=True))
        self.assertEqual(result.score, no_targets.score)

    def test_no_place_info_target_does_not_mask_a_real_target_in_the_same_list(self):
        # The dangerous shape: a "no place info" entry alongside a REAL
        # target location. Without the fix, the vacuous entry's unconditional
        # _hierarchy_match(True) would satisfy `any(...)`, silently matching
        # every job and defeating the user's actual "US"-only preference.
        p = profile(target_tags=["python"],
                    target_locations=["Remote", "US"],
                    target_locations_normalized=[
                        no_place_info_target("Remote"),
                        resolved_target(country="US", raw="US"),
                    ])
        us_job = score_job(p, job(classification_tags=["python"], is_remote=False,
                                   location_country="US", location_resolved=True))
        uk_job = score_job(p, job(classification_tags=["python"], is_remote=False,
                                   location_country="UK", location_resolved=True))
        self.assertLess(uk_job.score, us_job.score)

    def test_job_unresolved_target_resolved_falls_back_to_substring(self):
        p = profile(target_tags=["python"],
                    target_locations=["small town"],
                    target_locations_normalized=[resolved_target(
                        city="Small Town", country="Nowhere", raw="small town")])
        matching = score_job(p, job(classification_tags=["python"], is_remote=False,
                                     location="Small Town, Nowhere",
                                     location_resolved=False))
        not_matching = score_job(p, job(classification_tags=["python"], is_remote=False,
                                         location="Somewhere Else",
                                         location_resolved=False))
        self.assertLess(not_matching.score, matching.score)
