from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.applications.models import JobApplication
from apps.auto_apply.models import AutoApplyDraft, ExplicitAnswer
from apps.employers.models import Employer
from apps.jobs.models import Job

User = get_user_model()


class AutoApplyDraftModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self.job = Job.objects.create(
            source_ats="greenhouse", source_job_id="1",
            employer=self.employer, title="Backend Engineer",
        )

    def test_create_draft_with_valid_user_job_answers_succeeds(self):
        draft = AutoApplyDraft.objects.create(
            user=self.user,
            job=self.job,
            answers={"email": {"value": "alice@example.com", "needs_review": False}},
        )
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)
        self.assertEqual(draft.answers_schema_version, 1)
        self.assertIsNone(draft.job_application)
        self.assertIsNone(draft.exclusion_reason)
        self.assertIsNone(draft.error_message)

    def test_second_drafted_draft_for_same_user_job_violates_constraint(self):
        AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.DRAFTED
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            AutoApplyDraft.objects.create(
                user=self.user, job=self.job, status=AutoApplyDraft.Status.DRAFTED
            )

    def test_sending_draft_blocks_a_second_drafted_draft(self):
        AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.SENDING
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            AutoApplyDraft.objects.create(
                user=self.user, job=self.job, status=AutoApplyDraft.Status.DRAFTED
            )

    def test_new_draft_allowed_after_failed_draft_for_same_user_job(self):
        AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.FAILED
        )
        draft = AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.DRAFTED
        )
        self.assertEqual(
            AutoApplyDraft.objects.filter(user=self.user, job=self.job).count(), 2
        )
        self.assertEqual(draft.status, AutoApplyDraft.Status.DRAFTED)

    def test_new_draft_allowed_after_stale_draft_for_same_user_job(self):
        AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.STALE
        )
        AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.DRAFTED
        )
        self.assertEqual(
            AutoApplyDraft.objects.filter(user=self.user, job=self.job).count(), 2
        )

    def test_new_draft_allowed_after_excluded_draft_for_same_user_job(self):
        AutoApplyDraft.objects.create(
            user=self.user,
            job=self.job,
            status=AutoApplyDraft.Status.EXCLUDED,
            exclusion_reason="Unsupported field type: multi_select",
        )
        AutoApplyDraft.objects.create(
            user=self.user, job=self.job, status=AutoApplyDraft.Status.DRAFTED
        )
        self.assertEqual(
            AutoApplyDraft.objects.filter(user=self.user, job=self.job).count(), 2
        )

    def test_job_application_link_protected_on_delete(self):
        application = JobApplication.objects.create(
            user=self.user, job=self.job, status=JobApplication.Status.APPLIED
        )
        AutoApplyDraft.objects.create(
            user=self.user,
            job=self.job,
            status=AutoApplyDraft.Status.APPLIED,
            job_application=application,
        )
        with self.assertRaises(Exception):
            application.delete()


class ExplicitAnswerModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="bob", password="pw")

    def test_create_explicit_answer_succeeds(self):
        answer = ExplicitAnswer.objects.create(
            user=self.user,
            category=ExplicitAnswer.Category.WORK_AUTHORIZATION,
            answer_text="US Citizen",
        )
        self.assertEqual(answer.category, ExplicitAnswer.Category.WORK_AUTHORIZATION)

    def test_duplicate_user_category_rejected(self):
        ExplicitAnswer.objects.create(
            user=self.user,
            category=ExplicitAnswer.Category.SPONSORSHIP,
            answer_text="No",
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ExplicitAnswer.objects.create(
                user=self.user,
                category=ExplicitAnswer.Category.SPONSORSHIP,
                answer_text="Yes",
            )

    def test_same_category_allowed_for_different_users(self):
        other_user = User.objects.create_user(username="carol", password="pw")
        ExplicitAnswer.objects.create(
            user=self.user,
            category=ExplicitAnswer.Category.SALARY_EXPECTATION,
            answer_text="$150k",
        )
        ExplicitAnswer.objects.create(
            user=other_user,
            category=ExplicitAnswer.Category.SALARY_EXPECTATION,
            answer_text="$140k",
        )
        self.assertEqual(
            ExplicitAnswer.objects.filter(
                category=ExplicitAnswer.Category.SALARY_EXPECTATION
            ).count(),
            2,
        )
