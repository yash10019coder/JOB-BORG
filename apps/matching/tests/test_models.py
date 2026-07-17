from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.employers.models import Employer
from apps.jobs.models import Job
from apps.matching.constants import MatchStatus
from apps.matching.models import UserJobMatch

User = get_user_model()


class UserJobMatchModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.employer = Employer.objects.create(name="Acme", slug="acme")
        self.job = Job.objects.create(
            source_ats="greenhouse",
            source_job_id="1",
            employer=self.employer,
            title="Backend Engineer",
        )

    def test_create_match(self):
        m = UserJobMatch.objects.create(
            user=self.user,
            job=self.job,
            match_score=0.75,
            match_status=MatchStatus.RECOMMENDED,
            matched_tags=["python"],
        )
        self.assertEqual(m.match_status, MatchStatus.RECOMMENDED)
        self.assertEqual(m.matched_tags, ["python"])

    def test_duplicate_user_job_rejected(self):
        UserJobMatch.objects.create(user=self.user, job=self.job, match_score=0.5)
        with self.assertRaises(IntegrityError), transaction.atomic():
            UserJobMatch.objects.create(user=self.user, job=self.job, match_score=0.6)

    def test_ranked_retrieval_by_score(self):
        j2 = Job.objects.create(
            source_ats="greenhouse", source_job_id="2",
            employer=self.employer, title="Staff Engineer",
        )
        UserJobMatch.objects.create(user=self.user, job=self.job, match_score=0.4)
        UserJobMatch.objects.create(user=self.user, job=j2, match_score=0.9)
        ranked = list(
            UserJobMatch.objects.filter(user=self.user).order_by("-match_score")
        )
        self.assertEqual([m.job_id for m in ranked], [j2.id, self.job.id])
