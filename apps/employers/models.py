"""Employer (company) model — global/shared across all users."""
from django.db import models
from django.utils.text import slugify


class Employer(models.Model):
    """One row per company. Referenced by jobs and (later) contacts."""

    name = models.CharField(max_length=255)
    # Canonical identifier — a Greenhouse board maps 1:1 to an employer, so
    # slug/domain give a stable key independent of how a payload spells the name.
    slug = models.SlugField(max_length=255, unique=True)
    domain = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)
