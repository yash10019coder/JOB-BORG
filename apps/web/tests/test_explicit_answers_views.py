"""Tests for ExplicitAnswersForm and explicit_answers view."""
import json
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.auto_apply.models import ExplicitAnswer
from apps.web.forms import ExplicitAnswersForm


User = get_user_model()


class ExplicitAnswersFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")

    def test_form_initializes_from_existing_explicit_answers(self):
        ExplicitAnswer.objects.create(
            user=self.user, category="work_authorization", answer_text="Yes, authorized"
        )
        ExplicitAnswer.objects.create(
            user=self.user, category="sponsorship", answer_text="No sponsorship needed"
        )

        form = ExplicitAnswersForm(user=self.user)
        self.assertEqual(form.initial["work_authorization"], "Yes, authorized")
        self.assertEqual(form.initial["sponsorship"], "No sponsorship needed")

    def test_form_initializes_empty_for_new_user(self):
        form = ExplicitAnswersForm(user=self.user)
        self.assertEqual(form.initial.get("work_authorization"), "")
        self.assertEqual(form.initial.get("sponsorship"), "")

    def test_save_creates_explicit_answers(self):
        form = ExplicitAnswersForm(
            data={
                "work_authorization": "yes_authorized",
                "sponsorship": "no",
                "salary_expectation": "175-200k",
                "other": "Some notes",
            },
            user=self.user,
        )
        self.assertTrue(form.is_valid())
        form.save()

        self.assertEqual(ExplicitAnswer.objects.filter(user=self.user).count(), 4)
        wa = ExplicitAnswer.objects.get(user=self.user, category="work_authorization")
        self.assertEqual(wa.answer_text, "yes_authorized")

    def test_save_deletes_blanked_category(self):
        ExplicitAnswer.objects.create(
            user=self.user, category="work_authorization", answer_text="Old value"
        )

        form = ExplicitAnswersForm(
            data={
                "work_authorization": "",
                "sponsorship": "no",
                "salary_expectation": "",
                "other": "",
            },
            user=self.user,
        )
        self.assertTrue(form.is_valid())
        form.save()

        self.assertFalse(
            ExplicitAnswer.objects.filter(
                user=self.user, category="work_authorization"
            ).exists()
        )
        self.assertTrue(
            ExplicitAnswer.objects.filter(
                user=self.user, category="sponsorship"
            ).exists()
        )

    def test_form_does_not_see_other_users_answers(self):
        other_user = User.objects.create_user(username="other", password="pass")
        ExplicitAnswer.objects.create(
            user=other_user, category="work_authorization", answer_text="Other's answer"
        )

        form = ExplicitAnswersForm(user=self.user)
        # Form initializes all fields to empty string for current user
        self.assertEqual(form.initial.get("work_authorization"), "")


class ExplicitAnswersViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.client.force_login(self.user)

    def test_get_renders_form_prefilled(self):
        ExplicitAnswer.objects.create(
            user=self.user, category="work_authorization", answer_text="yes_authorized"
        )
        ExplicitAnswer.objects.create(
            user=self.user, category="sponsorship", answer_text="no"
        )

        response = self.client.get(reverse("explicit_answers"))
        self.assertEqual(response.status_code, 200)
        # Form renders select with the stored key as selected value
        self.assertContains(response, 'value="yes_authorized"')
        self.assertContains(response, 'value="no"')

    def test_post_upserts_explicit_answers(self):
        response = self.client.post(
            reverse("explicit_answers"),
            {
                "work_authorization": "yes_h1b",
                "sponsorship": "yes_opt",
                "salary_expectation": "100-125k",
                "other": "New notes",
            },
        )
        self.assertRedirects(response, reverse("profile"))

        wa = ExplicitAnswer.objects.get(user=self.user, category="work_authorization")
        self.assertEqual(wa.answer_text, "yes_h1b")
        sp = ExplicitAnswer.objects.get(user=self.user, category="sponsorship")
        self.assertEqual(sp.answer_text, "yes_opt")
        sa = ExplicitAnswer.objects.get(user=self.user, category="salary_expectation")
        self.assertEqual(sa.answer_text, "100-125k")

    def test_post_blanks_deletes_category(self):
        ExplicitAnswer.objects.create(
            user=self.user, category="salary_expectation", answer_text="Old salary"
        )

        response = self.client.post(
            reverse("explicit_answers"),
            {
                "work_authorization": "yes_authorized",
                "sponsorship": "no",
                "salary_expectation": "",
                "other": "",
            },
        )
        self.assertRedirects(response, reverse("profile"))

        self.assertFalse(
            ExplicitAnswer.objects.filter(
                user=self.user, category="salary_expectation"
            ).exists()
        )

    def test_post_saves_salary_by_region_to_profile(self):
        """Test that explicit_answers view no longer handles salary_by_region.

        The salary_by_region field is now managed by ProfileForm with per-region
        salary dropdowns. The explicit_answers view only saves free-text
        ExplicitAnswer rows.
        """
        response = self.client.post(
            reverse("explicit_answers"),
            {
                "work_authorization": "yes_authorized",
                "sponsorship": "no",
                "salary_expectation": "175-200k",
                "salary_by_region": json.dumps({"US": "175-200k", "IN": "10-15L"}),
            },
        )
        self.assertRedirects(response, reverse("profile"))

        # salary_by_region should NOT be updated by explicit_answers view anymore
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.salary_by_region, {})


class ProfileFormIncludesExplicitAnswersFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="testpass")
        self.client.force_login(self.user)

    def test_profile_view_passes_explicit_answers_form(self):
        response = self.client.get(reverse("profile"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("explicit_answers_form", response.context)
        self.assertIsInstance(response.context["explicit_answers_form"], ExplicitAnswersForm)

    def test_profile_form_and_explicit_answers_form_both_render(self):
        response = self.client.get(reverse("profile"))
        self.assertContains(response, "Save profile")
        self.assertContains(response, "Save answers")