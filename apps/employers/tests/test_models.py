from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.employers.models import Employer


class EmployerModelTests(TestCase):
    def test_slug_autopopulated_from_name(self):
        employer = Employer.objects.create(name="Acme Robotics")
        self.assertEqual(employer.slug, "acme-robotics")

    def test_explicit_slug_preserved(self):
        employer = Employer.objects.create(name="Acme", slug="acme-inc")
        self.assertEqual(employer.slug, "acme-inc")

    def test_slug_unique(self):
        Employer.objects.create(name="Acme", slug="acme")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Employer.objects.create(name="Acme Two", slug="acme")

    def test_str(self):
        self.assertEqual(str(Employer.objects.create(name="Globex")), "Globex")
