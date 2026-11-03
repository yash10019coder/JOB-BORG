"""Seed a saved answer the way it exists after the Phase 2 backfill.

The retired ExplicitAnswer page stored form option codes; the resolver now
reads the backfilled ``legacy:*`` AnswerBank rows. Tests that mean "the user
saved this answer on the old page" create the ExplicitAnswer row and run the
real backfill for that user, so they exercise the same translation production
data goes through.
"""
import importlib

from django.apps import apps

from apps.auto_apply.models import ExplicitAnswer


def seed_explicit_answer(*, user, category, answer_text):
    row = ExplicitAnswer.objects.create(user=user, category=category, answer_text=answer_text)
    backfill = importlib.import_module(
        "apps.auto_apply.migrations.0011_backfill_explicit_answers"
    )
    backfill.backfill_explicit_answers(apps, None, user_ids=[user.pk])
    return row
