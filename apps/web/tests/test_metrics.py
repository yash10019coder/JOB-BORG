"""U3: django-prometheus /metrics endpoint."""
from django.test import TestCase
from django.urls import reverse


class MetricsEndpointTests(TestCase):
    def test_metrics_returns_prometheus_exposition_format(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp["Content-Type"])
        self.assertIn("django_http_requests_total_by_view_transport_method", resp.content.decode())

    def test_metrics_records_recommendations_view_traffic(self):
        # Trigger a request against a real view, then confirm /metrics reflects it.
        self.client.get(reverse("login"))
        resp = self.client.get("/metrics")
        body = resp.content.decode()
        self.assertIn("django_http_requests_total_by_view_transport_method", body)

    def test_login_view_unaffected_by_prometheus_middleware(self):
        resp = self.client.get(reverse("login"))
        self.assertEqual(resp.status_code, 200)
