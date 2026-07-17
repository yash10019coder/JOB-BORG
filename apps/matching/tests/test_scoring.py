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
        "is_remote": True,
        "salary_min": None,
        "salary_max": None,
        "employer_slug": "acme",
    }
    base.update(overrides)
    return base


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
