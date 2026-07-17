"""Smoke tests proving the Django + Celery wiring is sound."""
from django.test import SimpleTestCase

from config.celery import app, debug_add


class CelerySmokeTests(SimpleTestCase):
    def test_celery_app_configured_from_django(self):
        """The Celery app reads its broker/backend from Django settings."""
        self.assertEqual(app.main, "jobborg")
        self.assertTrue(app.conf.broker_url)
        self.assertTrue(app.conf.result_backend)

    def test_debug_task_is_registered(self):
        """autodiscover / explicit registration exposes the debug task."""
        self.assertIn("config.debug_add", app.tasks)

    def test_debug_task_round_trips(self):
        """Execute -> result round-trip through Celery's task path returns the sum.

        ``.apply()`` runs the task synchronously in-process via Celery's
        execution machinery (no live broker/worker needed) and returns a
        result object, exercising the same registration + serialization path
        that ``.delay()`` uses against Redis in the deployed stack.
        """
        result = debug_add.apply((2, 3))
        self.assertTrue(result.successful())
        self.assertEqual(result.get(), 5)

    def test_debug_task_direct_call(self):
        self.assertEqual(debug_add.run(40, 2), 42)
