from unittest import mock

from django.test import TestCase

from apps.classification.tasks import classify_jobs, sweep_unclassified
from apps.employers.models import Employer
from apps.jobs.models import Job

PATCH_MATCH = "apps.classification.tasks.enqueue_matching"


class ClassifyJobsTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self._seq = 0

    def _job(self, **overrides):
        self._seq += 1
        defaults = dict(
            source_ats="greenhouse",
            source_job_id=str(self._seq),
            employer=self.employer,
            title="Senior Backend Engineer",
            description="python kubernetes",
            needs_classification=True,
            classification_tags=[],
        )
        defaults.update(overrides)
        return Job.objects.create(**defaults)

    def test_batch_classifies_flagged_jobs_and_clears_flag(self):
        job = self._job()
        with mock.patch(PATCH_MATCH):
            stats = classify_jobs()
        job.refresh_from_db()
        self.assertEqual(stats["classified"], 1)
        self.assertFalse(job.needs_classification)
        self.assertIn("python", job.classification_tags)
        self.assertEqual(job.ruleset_version, "v1")

    def test_already_classified_unchanged_jobs_are_skipped(self):
        self._job(needs_classification=False, classification_tags=["python"])
        with mock.patch(PATCH_MATCH) as enq:
            stats = classify_jobs()
        self.assertEqual(stats["classified"], 0)
        enq.assert_not_called()

    def test_tag_change_triggers_matching(self):
        job = self._job()  # fresh, tags [] -> will gain tags
        with mock.patch(PATCH_MATCH) as enq:
            classify_jobs()
        enq.assert_called_once_with(job.id)

    def test_identical_tags_do_not_trigger_matching(self):
        # Re-flagged but its stored tags already equal what the engine produces.
        self._job(
            needs_classification=True,
            classification_tags=["backend", "devops", "kubernetes", "python", "senior"],
        )
        with mock.patch(PATCH_MATCH) as enq:
            stats = classify_jobs()
        self.assertEqual(stats["classified"], 1)
        self.assertEqual(stats["matching_enqueued"], 0)
        enq.assert_not_called()

    def test_batch_size_bounds_invocation_and_sweep_drains_remainder(self):
        for _ in range(3):
            self._job()
        with mock.patch(PATCH_MATCH):
            first = sweep_unclassified(batch_size=2)
            second = sweep_unclassified(batch_size=2)
        self.assertEqual(first["classified"], 2)
        self.assertEqual(second["classified"], 1)
        self.assertEqual(Job.objects.filter(needs_classification=True).count(), 0)

    def test_running_twice_with_no_new_data_is_noop(self):
        self._job()
        with mock.patch(PATCH_MATCH) as enq:
            classify_jobs()
            enq.reset_mock()
            stats = classify_jobs()
        self.assertEqual(stats["classified"], 0)
        enq.assert_not_called()

    def test_event_driven_ids_restrict_to_those_jobs(self):
        target = self._job()
        other = self._job()
        with mock.patch(PATCH_MATCH):
            stats = classify_jobs(job_ids=[target.id])
        self.assertEqual(stats["classified"], 1)
        target.refresh_from_db()
        other.refresh_from_db()
        self.assertFalse(target.needs_classification)
        self.assertTrue(other.needs_classification)  # untouched
