from unittest import mock

from django.test import TestCase

from apps.employers.models import Employer
from apps.jobs.ingestion.exceptions import GreenhouseUnavailable
from apps.jobs.ingestion.upsert import upsert_jobs
from apps.jobs.models import Job, JobSource


def make_normalized(source_job_id, **overrides):
    data = {
        "source_ats": "greenhouse",
        "source_job_id": str(source_job_id),
        "title": f"Job {source_job_id}",
        "description": "desc",
        "location": "Remote - US",
        "is_remote": True,
        "location_city": "",
        "location_region": "",
        "location_country": "US",
        "location_resolved": True,
        "location_alias_version": "v1",
        "salary_min": None,
        "salary_max": None,
        "source_url": f"https://example.com/{source_job_id}",
    }
    data.update(overrides)
    return data


class UpsertJobsTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self.source = JobSource.objects.create(
            ats=JobSource.ATS.GREENHOUSE, board_token="acme", employer=self.employer
        )

    def test_first_ingest_creates_all_flagged_for_classification(self):
        result = upsert_jobs(self.source, [make_normalized(1), make_normalized(2)])
        self.assertEqual(len(result.created_ids), 2)
        self.assertEqual(Job.objects.count(), 2)
        self.assertEqual(Job.objects.filter(needs_classification=True).count(), 2)
        # Employer resolved via the JobSource FK, not the payload.
        self.assertTrue(all(j.employer_id == self.employer.id for j in Job.objects.all()))

    def test_structured_location_fields_persisted_on_create(self):
        upsert_jobs(
            self.source,
            [make_normalized(1, location="New York, NY, US", location_city="New York",
                              location_region="NY", location_country="US",
                              location_resolved=True, location_alias_version="v1")],
        )
        job = Job.objects.get(source_job_id="1")
        self.assertEqual(job.location_city, "New York")
        self.assertEqual(job.location_region, "NY")
        self.assertEqual(job.location_country, "US")
        self.assertTrue(job.location_resolved)
        self.assertEqual(job.location_alias_version, "v1")

    def test_structured_location_recomputed_when_location_changes(self):
        upsert_jobs(self.source, [make_normalized(1, location="Remote - US")])
        Job.objects.update(needs_classification=False)

        upsert_jobs(
            self.source,
            [make_normalized(1, location="Austin, TX, US", location_city="Austin",
                              location_region="TX", location_country="US",
                              location_resolved=True)],
        )
        job = Job.objects.get(source_job_id="1")
        self.assertEqual(job.location_city, "Austin")
        self.assertEqual(job.location_region, "TX")

    def test_structured_location_untouched_when_location_unchanged(self):
        upsert_jobs(
            self.source,
            [make_normalized(1, location_city="New York", location_region="NY")],
        )
        Job.objects.update(needs_classification=False)

        # Re-ingest with identical content -- content_hash matches, row takes
        # the unchanged_ids path, structured fields must not be touched.
        result = upsert_jobs(
            self.source,
            [make_normalized(1, location_city="New York", location_region="NY")],
        )
        self.assertEqual(len(result.unchanged_ids), 1)
        job = Job.objects.get(source_job_id="1")
        self.assertEqual(job.location_city, "New York")

    def test_reingest_identical_is_idempotent(self):
        jobs = [make_normalized(1), make_normalized(2)]
        upsert_jobs(self.source, jobs)
        # Simulate classification having cleared the flags.
        Job.objects.update(needs_classification=False)

        result = upsert_jobs(self.source, jobs)
        self.assertEqual(result.created_ids, [])
        self.assertEqual(result.updated_ids, [])
        self.assertEqual(len(result.unchanged_ids), 2)
        self.assertEqual(Job.objects.count(), 2)
        # Nothing re-flagged.
        self.assertEqual(Job.objects.filter(needs_classification=True).count(), 0)

    def test_changed_job_updates_and_reflags(self):
        upsert_jobs(self.source, [make_normalized(1)])
        Job.objects.update(needs_classification=False)

        result = upsert_jobs(
            self.source, [make_normalized(1, description="new description")]
        )
        self.assertEqual(len(result.updated_ids), 1)
        job = Job.objects.get(source_job_id="1")
        self.assertEqual(job.description, "new description")
        self.assertTrue(job.needs_classification)

    def test_new_job_in_second_run_creates_only_that_row(self):
        upsert_jobs(self.source, [make_normalized(1)])
        result = upsert_jobs(self.source, [make_normalized(1), make_normalized(2)])
        self.assertEqual(len(result.created_ids), 1)
        self.assertEqual(len(result.unchanged_ids), 1)
        self.assertEqual(Job.objects.count(), 2)

    def test_absent_job_is_marked_closed(self):
        upsert_jobs(self.source, [make_normalized(1), make_normalized(2)])
        result = upsert_jobs(self.source, [make_normalized(1)])
        self.assertEqual(len(result.closed_ids), 1)
        self.assertEqual(Job.objects.get(source_job_id="2").status, Job.Status.CLOSED)
        self.assertEqual(Job.objects.get(source_job_id="1").status, Job.Status.OPEN)

    def test_reappearing_job_is_reopened_and_reprocessed(self):
        upsert_jobs(self.source, [make_normalized(1), make_normalized(2)])
        # Give job 2 tags, then close it by omitting it.
        j2 = Job.objects.get(source_job_id="2")
        j2.classification_tags = ["python"]
        j2.needs_classification = False
        j2.save()
        upsert_jobs(self.source, [make_normalized(1)])
        self.assertEqual(Job.objects.get(source_job_id="2").status, Job.Status.CLOSED)

        # Reappears -> reopened, tags cleared, re-flagged for classification.
        result = upsert_jobs(self.source, [make_normalized(1), make_normalized(2)])
        self.assertEqual(len(result.reopened_ids), 1)
        j2 = Job.objects.get(source_job_id="2")
        self.assertEqual(j2.status, Job.Status.OPEN)
        self.assertEqual(j2.classification_tags, [])
        self.assertTrue(j2.needs_classification)

    def test_closure_scoped_to_this_source_only(self):
        # A second employer/source's open jobs must not be closed by this run.
        other_employer = Employer.objects.create(name="Globex", slug="globex")
        other_source = JobSource.objects.create(
            ats=JobSource.ATS.GREENHOUSE, board_token="globex", employer=other_employer
        )
        upsert_jobs(other_source, [make_normalized(99)])
        upsert_jobs(self.source, [make_normalized(1)])

        # Re-ingest self.source with job 1 still present; job 99 belongs to the
        # other source and must stay open.
        upsert_jobs(self.source, [make_normalized(1)])
        self.assertEqual(Job.objects.get(source_job_id="99").status, Job.Status.OPEN)

    def test_mid_batch_error_rolls_back(self):
        bad = make_normalized(2)
        del bad["title"]  # triggers KeyError inside _apply_content
        with self.assertRaises(KeyError):
            upsert_jobs(self.source, [make_normalized(1), bad])
        # Atomic: the first job's create was rolled back too.
        self.assertEqual(Job.objects.count(), 0)


class _FakeClient:
    """Stands in for GreenhouseClient; behavior keyed on board token."""

    def __init__(self, by_board):
        self._by_board = by_board

    def fetch_jobs(self, board_token):
        value = self._by_board[board_token]
        if isinstance(value, Exception):
            raise value
        return value


class IngestTaskTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self.source = JobSource.objects.create(
            ats=JobSource.ATS.GREENHOUSE, board_token="acme", employer=self.employer
        )

    def test_ingest_source_enqueues_classification_once_per_changed_job(self):
        from apps.jobs import tasks

        fake = _FakeClient({"acme": [make_normalized(1), make_normalized(2)]})
        with mock.patch("apps.jobs.tasks.get_client", return_value=fake), \
             mock.patch("apps.jobs.tasks.enqueue_classification") as enq:
            stats = tasks.ingest_source(self.source.id)
        self.assertEqual(stats["created"], 2)
        enq.assert_called_once()
        enqueued_ids = list(enq.call_args.args[0])
        self.assertEqual(len(enqueued_ids), 2)
        self.assertEqual(len(set(enqueued_ids)), 2)  # each id exactly once

    def test_one_board_failure_does_not_abort_others(self):
        other_employer = Employer.objects.create(name="Globex", slug="globex")
        JobSource.objects.create(
            ats=JobSource.ATS.GREENHOUSE, board_token="globex", employer=other_employer
        )
        fake = _FakeClient(
            {
                "acme": GreenhouseUnavailable("down"),
                "globex": [make_normalized(50)],
            }
        )
        from apps.jobs import tasks

        with mock.patch("apps.jobs.tasks.get_client", return_value=fake), \
             mock.patch("apps.jobs.tasks.enqueue_classification"):
            results = tasks.ingest_all_active_sources()

        # The failing board is reported as an error; the healthy one still ran.
        self.assertTrue(any(r.get("error") for r in results))
        self.assertEqual(Job.objects.filter(source_job_id="50").count(), 1)

    def test_ingest_source_dispatches_by_ats(self):
        # Covers AE1: a non-Greenhouse JobSource is fetched via the client
        # its own `ats` value resolves to, not a hardcoded GreenhouseClient.
        lever_employer = Employer.objects.create(name="Widget Co", slug="widget-co")
        lever_source = JobSource.objects.create(
            ats=JobSource.ATS.LEVER, board_token="widget-co", employer=lever_employer
        )
        fake_lever_client = _FakeClient({"widget-co": [make_normalized(99)]})

        from apps.jobs import tasks

        def fake_get_client(ats):
            self.assertEqual(ats, JobSource.ATS.LEVER)
            return fake_lever_client

        with mock.patch("apps.jobs.tasks.get_client", side_effect=fake_get_client), \
             mock.patch("apps.jobs.tasks.enqueue_classification"):
            stats = tasks.ingest_source(lever_source.id)

        self.assertEqual(stats["created"], 1)
        self.assertEqual(Job.objects.get(source_job_id="99").employer, lever_employer)
