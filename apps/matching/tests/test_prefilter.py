"""Pre-filter tests — pure predicate, no DB."""
from django.test import SimpleTestCase

from apps.accounts.models import Profile
from apps.matching.prefilter import passes_prefilter

from .test_scoring import job, profile


class PrefilterTests(SimpleTestCase):
    def test_passes_by_default(self):
        self.assertTrue(passes_prefilter(profile(), job()))

    def test_excluded_employer_rejected(self):
        p = profile(excluded_employers=["acme"])
        self.assertFalse(passes_prefilter(p, job(employer_slug="acme")))
        self.assertTrue(passes_prefilter(p, job(employer_slug="globex")))

    def test_remote_only_rejects_onsite(self):
        p = profile(remote_pref=Profile.RemotePref.REMOTE_ONLY)
        self.assertFalse(passes_prefilter(p, job(is_remote=False)))
        self.assertTrue(passes_prefilter(p, job(is_remote=True)))

    def test_onsite_only_rejects_remote(self):
        p = profile(remote_pref=Profile.RemotePref.ONSITE_ONLY)
        self.assertFalse(passes_prefilter(p, job(is_remote=True)))
        self.assertTrue(passes_prefilter(p, job(is_remote=False)))

    def test_known_below_min_salary_rejected(self):
        p = profile(min_salary=150000)
        self.assertFalse(passes_prefilter(p, job(salary_min=90000, salary_max=120000)))

    def test_known_above_min_salary_passes(self):
        p = profile(min_salary=150000)
        self.assertTrue(passes_prefilter(p, job(salary_min=160000, salary_max=200000)))

    def test_unknown_salary_never_dropped(self):
        p = profile(min_salary=150000)
        self.assertTrue(passes_prefilter(p, job(salary_min=None, salary_max=None)))
