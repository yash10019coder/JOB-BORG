from django.contrib import admin

from .models import Employer


@admin.register(Employer)
class EmployerAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "domain", "created_at")
    search_fields = ("name", "slug", "domain")
    prepopulated_fields = {"slug": ("name",)}
