"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Exposes GET /metrics in Prometheus exposition format.
    path("", include("django_prometheus.urls")),
    path("", include("apps.web.urls")),
]
