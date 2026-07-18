"""U4: Celery task-event settings that feed celery-exporter's Prometheus metrics."""
from django.conf import settings
from django.test import SimpleTestCase


class CeleryEventSettingsTests(SimpleTestCase):
    def test_worker_send_task_events_enabled(self):
        self.assertTrue(settings.CELERY_WORKER_SEND_TASK_EVENTS)

    def test_task_send_sent_event_enabled(self):
        self.assertTrue(settings.CELERY_TASK_SEND_SENT_EVENT)
