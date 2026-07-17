from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.applications.models import JobApplication
from apps.employers.models import Employer
from apps.jobs.models import Job

User = get_user_model()


class JobApplicationModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self.job = Job.objects.create(
            source_ats="greenhouse", source_job_id="1",
            employer=self.employer, title="Backend Engineer",
        )

    def test_default_status_saved(self):
        app = JobApplication.objects.create(user=self.user, job=self.job)
        self.assertEqual(app.status, JobApplication.Status.SAVED)

    def test_duplicate_user_job_rejected(self):
        JobApplication.objects.create(user=self.user, job=self.job)
        with self.assertRaises(IntegrityError), transaction.atomic():
            JobApplication.objects.create(user=self.user, job=self.job)

    def test_save_then_applied_updates_same_row(self):
        app, _ = JobApplication.objects.update_or_create(
            user=self.user, job=self.job,
            defaults={"status": JobApplication.Status.SAVED},
        )
        app2, created = JobApplication.objects.update_or_create(
            user=self.user, job=self.job,
            defaults={"status": JobApplication.Status.APPLIED},
        )
        self.assertFalse(created)
        self.assertEqual(app.pk, app2.pk)
        self.assertEqual(
            JobApplication.objects.filter(user=self.user, job=self.job).count(), 1
        )
        self.assertEqual(app2.status, JobApplication.Status.APPLIED)
