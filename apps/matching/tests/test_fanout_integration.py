from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from apps.accounts.models import Profile
from apps.matching.models import UserJobMatch
from apps.matching.services import match_job, rematch_profile_obj
from apps.matching.tasks import _rematch_token_key, rematch_profile, schedule_rematch

from .factories import make_employer, make_job, make_profile


class CrossUserIsolationTests(TestCase):
    def setUp(self):
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        patcher.start()
        self.employer = make_employer()

    def test_two_users_different_criteria_get_different_lists(self):
        job = make_job(self.employer, tags=["python", "kubernetes"], is_remote=True)
        py = make_profile("py", tags=["python", "kubernetes"])
        rust = make_profile("rust", tags=["rust"])

        match_job(job)

        py_match = UserJobMatch.objects.get(user=py.user, job=job)
        rust_match = UserJobMatch.objects.get(user=rust.user, job=job)
        # Same job, different per-user scores/explanations — no shared match state.
        self.assertGreater(py_match.match_score, rust_match.match_score)
        self.assertEqual(py_match.matched_tags, ["kubernetes", "python"])
        self.assertEqual(rust_match.matched_tags, [])
        # Neither user can see the other's row.
        self.assertEqual(UserJobMatch.objects.filter(user=py.user).count(), 1)
        self.assertEqual(UserJobMatch.objects.filter(user=rust.user).count(), 1)

    def test_job_and_profile_entrypoints_produce_one_row(self):
        job = make_job(self.employer, tags=["python"])
        p = make_profile("p", tags=["python"])
        # Both entry points touch the same (user, job) pair.
        match_job(job)
        rematch_profile_obj(p)
        self.assertEqual(
            UserJobMatch.objects.filter(user=p.user, job=job).count(), 1
        )


class DebounceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.employer = make_employer()

    def test_stale_token_skips_execution(self):
        with mock.patch("apps.matching.signals.schedule_rematch"):
            p = make_profile("p", tags=["python"])
        make_job(self.employer, tags=["python"])

        # A newer save set the current token to something else.
        cache.set(_rematch_token_key(p.id), "newer-token", timeout=3600)
        result = rematch_profile(profile_id=p.id, token="stale-token")
        self.assertTrue(result["skipped"])
        self.assertEqual(UserJobMatch.objects.filter(user=p.user).count(), 0)

    def test_current_token_runs(self):
        with mock.patch("apps.matching.signals.schedule_rematch"):
            p = make_profile("p", tags=["python"])
        make_job(self.employer, tags=["python"])

        cache.set(_rematch_token_key(p.id), "tok", timeout=3600)
        result = rematch_profile(profile_id=p.id, token="tok")
        self.assertNotIn("skipped", result)
        self.assertEqual(UserJobMatch.objects.filter(user=p.user).count(), 1)

    def test_rapid_saves_coalesce_to_latest_token(self):
        recorded = {}

        def fake_apply_async(kwargs=None, **_):
            recorded["last_token"] = kwargs["token"]

        with mock.patch(
            "apps.matching.tasks.rematch_profile.apply_async",
            side_effect=fake_apply_async,
        ):
            schedule_rematch(42)
            schedule_rematch(42)
        # The cache holds only the most recent token; that is the one a delayed
        # task will accept — earlier scheduled runs find a newer token and skip.
        self.assertEqual(cache.get(_rematch_token_key(42)), recorded["last_token"])
