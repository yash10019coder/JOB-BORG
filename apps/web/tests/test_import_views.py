"""The Import tab: start, wait, review, apply (rules R1-R8, A1, A3, FU7, G3, P4, V4)."""
import tempfile
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.importing.documents import normalize_text
from apps.accounts.importing.pipeline import run_rules
from apps.accounts.importing.service import build_payload
from apps.accounts.models import (
    AnswerBank, AnswerObservation, ImportJob, Profile, ProfileSuggestion, ResumeEntry,
)
from apps.accounts.tests import import_fixtures as fx
from apps.accounts.tests.test_import_documents import build_docx

User = get_user_model()


def ready_payload(text=fx.BULLET_ORG_RESUME):
    normalized = normalize_text(text)
    return build_payload(run_rules(normalized), normalized)


class _Base(TestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        override = override_settings(MEDIA_ROOT=self.media.name)
        override.enable()
        self.addCleanup(override.disable)
        cache.clear()
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        self.schedule = patcher.start()
        self.user = User.objects.create_user(username="alice", password="pw")
        self.profile = self.user.profile
        self.other_user = User.objects.create_user(username="bob", password="pw")
        self.other = self.other_user.profile
        self.client.force_login(self.user)
        self.schedule.reset_mock()

    def ready_job(self, profile=None, text=fx.BULLET_ORG_RESUME, **extra):
        return ImportJob.objects.create(
            profile=profile or self.profile, kind="document", source_kind="resume",
            status=ImportJob.Status.READY, payload=ready_payload(text), **extra,
        )

    def accept_all(self, job, **overrides):
        """POST every proposal as 'accept', the way a user pressing Save would."""
        payload = job.payload
        post = {}
        for key, proposal in payload["fields"].items():
            value = proposal["value"]
            post[f"decision__{key}"] = "accept"
            post[f"value__{key}"] = ", ".join(value) if key == "target_tags" else "\n".join(value) if key == "target_titles" else value
        for index, entry in enumerate(payload["entries"]):
            post[f"entry_decision__{index}"] = "accept"
            post[f"entry_title__{index}"] = entry["title"]
            post[f"entry_org__{index}"] = entry["organization"]
            post[f"entry_start__{index}"] = entry["start"] or ""
            post[f"entry_end__{index}"] = entry["end"] or ""
            if entry["is_current"]:
                post[f"entry_current__{index}"] = "on"
            post[f"entry_skills__{index}"] = ", ".join(entry["skills"])
        post.update(overrides)
        return post

    def apply(self, job, post=None, client=None, **overrides):
        return (client or self.client).post(
            reverse("import_apply", args=[job.public_id]), post if post is not None else self.accept_all(job, **overrides)
        )

    def reload(self):
        self.profile.refresh_from_db()
        return self.profile


class AccessTests(_Base):
    def test_A3_every_import_route_requires_login(self):
        job = self.ready_job()
        entry = ResumeEntry.objects.create(profile=self.profile, kind="experience", title="Eng", natural_key="a")
        anon = Client()
        urls = [
            ("get", reverse("profile_import")), ("post", reverse("import_start")),
            ("get", reverse("import_status", args=[job.public_id])), ("get", reverse("import_review", args=[job.public_id])),
            ("post", reverse("import_apply", args=[job.public_id])), ("post", reverse("import_discard", args=[job.public_id])),
            ("post", reverse("import_entry_delete", args=[entry.pk])),
        ]
        for method, url in urls:
            with self.subTest(url=url):
                response = getattr(anon, method)(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("login", response["Location"])

    def test_A3_write_routes_reject_get(self):
        job = self.ready_job()
        for name, args in (("import_start", []), ("import_apply", [job.public_id]), ("import_discard", [job.public_id])):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 405)

    def test_A3_every_post_needs_a_csrf_token(self):
        job = self.ready_job()
        entry = ResumeEntry.objects.create(profile=self.profile, kind="experience", title="Eng", natural_key="a")
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        for url in (reverse("import_start"), reverse("import_apply", args=[job.public_id]),
                    reverse("import_discard", args=[job.public_id]), reverse("import_entry_delete", args=[entry.pk])):
            with self.subTest(url=url):
                self.assertEqual(strict.post(url, {}).status_code, 403)

    def test_A3_nobody_can_reach_another_users_job(self):
        theirs = self.ready_job(profile=self.other)
        for method, name in (("get", "import_status"), ("get", "import_review"),
                             ("post", "import_apply"), ("post", "import_discard")):
            with self.subTest(name=name):
                response = getattr(self.client, method)(reverse(name, args=[theirs.public_id]))
                self.assertEqual(response.status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, "ready")

    def test_an_unknown_job_id_is_a_404(self):
        self.assertEqual(self.client.get(reverse("import_status", args=[uuid.uuid4()])).status_code, 404)

    def test_the_import_tab_is_linked_from_the_other_profile_pages(self):
        for name in ("profile", "profile_answers", "profile_learning"):
            self.assertIn(reverse("profile_import"), self.client.get(reverse(name)).content.decode())


class StartTests(_Base):
    def upload(self, **extra):
        return self.client.post(
            reverse("import_start"),
            {"source": "resume", "file": SimpleUploadedFile("resume.docx", build_docx(fx.BULLET_ORG_RESUME.split("\n"))), **extra},
        )

    def test_uploading_a_resume_starts_a_job_and_ends_on_the_review_page(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.upload()
        job = ImportJob.objects.get()
        self.assertRedirects(response, reverse("import_status", args=[job.public_id]), fetch_redirect_response=False)
        followed = self.client.get(reverse("import_status", args=[job.public_id]))
        self.assertRedirects(followed, reverse("import_review", args=[job.public_id]), fetch_redirect_response=False)

    def test_a_next_parameter_is_ignored(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.upload(next="https://evil.example/")
        self.assertNotIn("evil", response["Location"])

    def test_the_saved_resume_can_be_imported_without_a_file(self):
        Profile.objects.filter(pk=self.profile.pk).update(resume_text=fx.BULLET_ORG_RESUME)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("import_start"), {"source": "current_resume"})
        self.assertEqual(ImportJob.objects.get().status, "ready")

    def test_bad_input_shows_a_friendly_message_and_creates_no_job(self):
        cases = [
            ({"source": "bogus"}, "Choose what to import"),
            ({"source": "resume"}, "That file is empty"),
            ({"source": "resume", "file": SimpleUploadedFile("x.exe", b"x" * 500)}, "Upload a PDF, DOCX or TXT"),
        ]
        for post, text in cases:
            with self.subTest(text=text):
                response = self.client.post(reverse("import_start"), post, follow=True)
                self.assertContains(response, text)
        self.assertEqual(ImportJob.objects.count(), 0)

    def test_a_second_import_while_one_runs_is_refused_with_a_message(self):
        ImportJob.objects.create(profile=self.profile, kind="document", source_kind="resume", status="running")
        response = self.client.post(
            reverse("import_start"),
            {"source": "resume", "file": SimpleUploadedFile("r.docx", build_docx())}, follow=True,
        )
        self.assertContains(response, "already running")
        self.assertEqual(ImportJob.objects.count(), 1)

    def test_the_hourly_limit_shows_a_message(self):
        with self.settings(PROFILE_IMPORT_RATE_PER_HOUR=1):
            with self.captureOnCommitCallbacks(execute=True):
                self.upload()
            ImportJob.objects.update(status=ImportJob.Status.APPLIED)
            response = self.client.post(
                reverse("import_start"),
                {"source": "resume", "file": SimpleUploadedFile("r.docx", build_docx())}, follow=True,
            )
        self.assertContains(response, "Try again in an hour")


class StatusTests(_Base):
    def job(self, status, **extra):
        return ImportJob.objects.create(profile=self.profile, kind="document", source_kind="resume", status=status, **extra)

    def test_a_running_job_refreshes_itself_in_the_head_and_announces_politely(self):
        html = self.client.get(reverse("import_status", args=[self.job("running").public_id])).content.decode()
        self.assertIn('<meta http-equiv="refresh" content="3">', html)
        self.assertIn('aria-live="polite"', html)

    def test_the_refresh_stops_after_three_minutes(self):
        job = self.job("pending")
        ImportJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(minutes=4))
        html = self.client.get(reverse("import_status", args=[job.public_id])).content.decode()
        self.assertNotIn("http-equiv", html)
        self.assertIn("taking longer", html)

    def test_a_failed_job_shows_a_friendly_message_for_its_code(self):
        job = self.job("failed", error_code="pdf_encrypted")
        self.assertContains(self.client.get(reverse("import_status", args=[job.public_id])), "password-protected")

    def test_applied_discarded_and_expired_jobs_say_so(self):
        for status, text in (("applied", "was applied"), ("discarded", "was discarded"), ("expired", "expired")):
            with self.subTest(status=status):
                job = self.job(status)
                self.assertContains(self.client.get(reverse("import_status", args=[job.public_id])), text)
                job.delete()

    def test_an_expired_ready_job_cannot_be_reviewed(self):
        job = self.ready_job(expires_at=timezone.now() - timedelta(minutes=1))
        response = self.client.get(reverse("import_review", args=[job.public_id]))
        self.assertRedirects(response, reverse("import_status", args=[job.public_id]), fetch_redirect_response=False)

    def test_a_job_that_is_not_ready_has_no_review_page(self):
        job = self.job("running")
        response = self.client.get(reverse("import_review", args=[job.public_id]))
        self.assertRedirects(response, reverse("import_status", args=[job.public_id]), fetch_redirect_response=False)


class ReviewPageTests(_Base):
    def page(self, job=None):
        job = job or self.ready_job()
        return self.client.get(reverse("import_review", args=[job.public_id])).content.decode()

    def test_R1_empty_fields_default_to_save_and_everything_is_listed(self):
        html = self.page()
        self.assertIn("Jane Doe", html)
        self.assertIn("+91 9876543210", html)
        self.assertIn('name="decision__phone" value="accept" checked', html)
        self.assertIn("Senior Backend Engineer", html)
        self.assertIn('name="entry_decision__0" value="accept" checked', html)

    def test_R2_a_user_set_field_is_shown_kept_and_has_no_controls(self):
        Profile.objects.filter(pk=self.profile.pk).update(phone="555 010 0199")
        html = self.page()
        self.assertIn("Kept (you set this)", html)
        self.assertNotIn('name="value__phone"', html)
        self.assertNotIn('name="decision__phone"', html)
        self.assertIn("555 010 0199", html)

    def test_R2_locked_and_learned_fields_say_why(self):
        Profile.objects.filter(pk=self.profile.pk).update(
            full_name="Real Name", headline="Learned",
            field_provenance={"full_name": {"source": "user", "locked": True, "updated_at": "x", "detail": ""}},
        )
        self.assertIn("Kept (locked)", self.page())

    def test_R2_an_identical_previously_imported_value_shows_unchanged(self):
        job = self.ready_job()
        self.apply(job)
        again = self.ready_job()
        self.assertIn("Unchanged", self.page(again))

    def test_R1_an_earlier_imported_value_that_differs_defaults_to_replace(self):
        self.apply(self.ready_job())
        changed = fx.BULLET_ORG_RESUME.replace("+91 9876543210", "+91 9123456780")
        html = self.page(self.ready_job(text=changed))
        self.assertIn('name="decision__phone" value="accept" checked', html)
        self.assertIn("+91 9123456780", html)

    def test_provisional_linkedin_values_default_to_skip(self):
        job = self.ready_job()
        job.payload["fields"]["headline"] = {"value": "Staff Engineer", "snippet": "x", "extractor": "rule", "provisional": True}
        job.save()
        html = self.page(job)
        self.assertIn('name="decision__headline" value="reject" checked', html)
        self.assertIn("Layout not verified", html)

    def test_FU7_links_are_plain_text_never_clickable(self):
        html = self.page()
        self.assertIn("https://janedoe.dev", html)
        self.assertNotIn('href="https://janedoe.dev', html)
        self.assertNotIn('href="https://github.com/janedoe', html)

    def test_G3_hostile_snippets_are_escaped(self):
        text = "Jane Doe\n<script>alert(1)</script> | +91 9876543210 | jane@example.com\nSkills\nPython\n"
        html = self.page(self.ready_job(text=text))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_entries_show_their_flags_and_existing_state(self):
        job = self.ready_job()
        job.payload["entries"][0]["needs_check"] = True
        job.payload["entries"][0]["layout_inferred"] = True
        job.save()
        html = self.page(job)
        self.assertIn("check this", html)
        self.assertIn("layout guessed", html)

    def test_R7_an_entry_already_saved_or_edited_by_the_user_has_no_controls(self):
        job = self.ready_job()
        self.apply(job, client=self.client)
        ResumeEntry.objects.filter(organization="Globex Data").update(source="user")
        html = self.page(self.ready_job())
        self.assertIn("Already saved", html)
        self.assertIn("Kept (you edited this entry before)", html)


class ApplyTests(_Base):
    def test_R1_accepting_everything_saves_fields_and_entries(self):
        job = self.ready_job()
        response = self.apply(job)
        self.assertRedirects(response, reverse("profile_import"), fetch_redirect_response=False)
        profile = self.reload()
        self.assertEqual((profile.full_name, profile.phone), ("Jane Doe", "+91 9876543210"))
        self.assertEqual(profile.linkedin_url, "https://www.linkedin.com/in/jane-doe-12345")
        self.assertEqual(profile.current_employer, "Acme Payments")
        self.assertEqual(profile.target_tags, ["python", "golang", "kubernetes"])
        self.assertEqual(profile.field_provenance["phone"]["source"], "imported")
        self.assertEqual(profile.field_provenance["phone"]["detail"], str(job.public_id))
        self.assertEqual(ResumeEntry.objects.count(), 2)
        job.refresh_from_db()
        self.assertEqual((job.status, job.payload), ("applied", {}))
        self.assertIsNotNone(job.applied_at)

    def test_R3_a_skipped_field_is_not_saved(self):
        job = self.ready_job()
        self.apply(job, decision__phone="reject")
        self.assertEqual(self.reload().phone, "")

    def test_R3_a_value_edited_in_the_review_is_stored_as_the_users(self):
        job = self.ready_job()
        self.apply(job, value__phone="+91 9000000000")
        profile = self.reload()
        self.assertEqual(profile.phone, "+91 9000000000")
        self.assertEqual(profile.field_provenance["phone"]["source"], "user")
        self.assertEqual(profile.field_provenance["full_name"]["source"], "imported")

    def test_R3_an_emptied_value_is_skipped(self):
        self.apply(self.ready_job(), value__phone="   ")
        self.assertEqual(self.reload().phone, "")

    def test_R3_an_edited_entry_is_stored_as_user_and_an_unedited_one_as_imported(self):
        job = self.ready_job()
        self.apply(job, entry_title__0="Principal Backend Engineer")
        edited = ResumeEntry.objects.get(title="Principal Backend Engineer")
        plain = ResumeEntry.objects.get(title="Software Engineer")
        self.assertEqual((edited.source, plain.source), ("user", "imported"))

    def test_R5_a_kept_field_is_never_overwritten_even_if_the_request_says_save(self):
        Profile.objects.filter(pk=self.profile.pk).update(phone="555 010 0199")
        self.apply(self.ready_job(), decision__phone="accept", value__phone="+91 9876543210")
        self.assertEqual(self.reload().phone, "555 010 0199")

    def test_R5_a_field_the_user_filled_after_the_page_was_built_is_kept(self):
        job = self.ready_job()
        post = self.accept_all(job)
        Profile.objects.filter(pk=self.profile.pk).update(phone="555 010 0199")  # edited in another tab
        self.apply(job, post=post)
        self.assertEqual(self.reload().phone, "555 010 0199")
        self.assertEqual(self.reload().full_name, "Jane Doe")

    def test_unknown_fields_in_the_post_are_ignored(self):
        job = self.ready_job()
        self.apply(job, decision__email="accept", value__email="x@y.co", decision__visa_status_by_country="accept",
                   value__visa_status_by_country="citizen", value__salary_by_region="1")
        profile = self.reload()
        self.assertEqual(profile.visa_status_by_country, {})
        self.assertEqual(profile.salary_by_region, {})
        self.assertEqual(profile.full_name, "Jane Doe")

    def test_V4_an_invalid_value_re_renders_with_the_error_and_saves_nothing(self):
        job = self.ready_job()
        response = self.apply(job, value__portfolio_url="javascript:alert(1)")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("This value is not valid", html)
        self.assertIn("javascript:alert(1)", html.replace("&amp;", "&"))  # the user's input is kept
        profile = self.reload()
        self.assertEqual((profile.full_name, profile.phone, profile.field_provenance), ("", "", {}))
        self.assertEqual(ResumeEntry.objects.count(), 0)
        job.refresh_from_db()
        self.assertEqual(job.status, "ready")

    def test_R4_an_invalid_entry_rolls_back_the_valid_fields_too(self):
        job = self.ready_job()
        response = self.apply(job, entry_start__1="2024-01", entry_end__1="2020-01")
        self.assertEqual(response.status_code, 200)
        self.assertIn("This entry is not valid", response.content.decode())
        profile = self.reload()
        self.assertEqual((profile.full_name, profile.phone), ("", ""))
        self.assertEqual(ResumeEntry.objects.count(), 0)
        self.assertEqual(ImportJob.objects.get(pk=job.pk).status, "ready")

    def test_R4_a_matching_field_means_exactly_one_rematch(self):
        self.apply(self.ready_job())
        self.assertEqual(self.schedule.call_count, 1)

    def test_R4_non_matching_fields_alone_cause_no_rematch(self):
        job = self.ready_job()
        post = {"decision__phone": "accept", "value__phone": "+91 9876543210"}
        self.apply(job, post=post)
        self.schedule.assert_not_called()

    def test_R6_a_second_apply_does_nothing(self):
        job = self.ready_job()
        self.apply(job)
        Profile.objects.filter(pk=self.profile.pk).update(phone="")  # would be re-filled if applied twice
        response = self.apply(job, client=self.client)
        self.assertRedirects(response, reverse("import_status", args=[job.public_id]), fetch_redirect_response=False)
        self.assertEqual(self.reload().phone, "")

    def test_R6_an_expired_job_cannot_be_applied(self):
        job = self.ready_job(expires_at=timezone.now() - timedelta(minutes=1))
        self.apply(job)
        self.assertEqual(self.reload().full_name, "")

    def test_R6_discarding_clears_the_proposals_and_changes_nothing(self):
        job = self.ready_job()
        response = self.client.post(reverse("import_discard", args=[job.public_id]))
        self.assertRedirects(response, reverse("profile_import"), fetch_redirect_response=False)
        job.refresh_from_db()
        self.assertEqual((job.status, job.payload), ("discarded", {}))
        self.assertEqual(self.reload().full_name, "")
        self.assertEqual(self.client.post(reverse("import_discard", args=[job.public_id]), follow=True).status_code, 200)

    def test_P4_importing_never_feeds_the_learning_loop(self):
        self.apply(self.ready_job())
        for model in (AnswerObservation, AnswerBank, ProfileSuggestion):
            self.assertEqual(model.objects.count(), 0)

    def test_the_result_message_summarises_what_happened(self):
        Profile.objects.filter(pk=self.profile.pk).update(phone="555 010 0199")
        job = self.ready_job()
        response = self.client.post(reverse("import_apply", args=[job.public_id]), self.accept_all(job), follow=True)
        self.assertContains(response, "profile field")
        self.assertContains(response, "kept 1 you had already set")

    def test_re_importing_updates_imported_entries_and_deletes_nothing(self):
        self.apply(self.ready_job())
        changed = fx.BULLET_ORG_RESUME.replace("Senior Backend Engineer", "Staff Backend Engineer")
        second = self.ready_job(text=changed)
        self.apply(second)
        self.assertEqual(ResumeEntry.objects.count(), 3)  # the old title stays; nothing is deleted


class SavedEntriesTests(_Base):
    def test_years_by_skill_and_entries_are_shown_after_an_import(self):
        self.apply(self.ready_job())
        html = self.client.get(reverse("profile_import")).content.decode()
        self.assertIn("Years by skill", html)
        self.assertIn("Senior Backend Engineer", html)
        self.assertIn("Globex Data", html)

    def test_R8_an_entry_can_be_deleted_by_its_owner_only(self):
        self.apply(self.ready_job())
        entry = ResumeEntry.objects.first()
        mine = self.client.post(reverse("import_entry_delete", args=[entry.pk]))
        self.assertRedirects(mine, reverse("profile_import"), fetch_redirect_response=False)
        self.assertFalse(ResumeEntry.objects.filter(pk=entry.pk).exists())
        theirs = ResumeEntry.objects.create(profile=self.other, kind="experience", title="Eng", natural_key="z")
        self.assertEqual(self.client.post(reverse("import_entry_delete", args=[theirs.pk])).status_code, 404)
        self.assertTrue(ResumeEntry.objects.filter(pk=theirs.pk).exists())

    def test_the_home_page_lists_recent_imports_and_disables_the_saved_resume_option_when_empty(self):
        self.ready_job()
        html = self.client.get(reverse("profile_import")).content.decode()
        self.assertIn("Recent imports", html)
        self.assertIn('value="current_resume" disabled', html)
        Profile.objects.filter(pk=self.profile.pk).update(resume_text=fx.BULLET_ORG_RESUME)
        self.assertNotIn('value="current_resume" disabled', self.client.get(reverse("profile_import")).content.decode())


AI_ENABLED = dict(
    PROFILE_IMPORT_LLM_ALLOWED_PROVIDERS=["openai"],
    PROFILE_IMPORT_LLM_PROVIDER="openai",
    OPENAI_API_KEY="sk-test",
    PROFILE_IMPORT_CONSENT_VERSION="v1",
)


class ConsentTests(_Base):
    def page(self):
        return self.client.get(reverse("profile_import")).content.decode()

    def test_L2_the_control_is_not_shown_unless_an_operator_enabled_a_provider(self):
        self.assertNotIn("AI-assisted import", self.page())

    @override_settings(**AI_ENABLED)
    def test_L2_when_enabled_it_is_shown_unticked_and_names_what_is_sent(self):
        html = self.page()
        self.assertIn("AI-assisted import", html)
        self.assertIn('name="consent"', html)
        self.assertNotIn('name="consent" checked', html)
        self.assertIn("openai", html)
        self.assertIn("nothing else from your account", html)

    def test_L2_posting_while_no_provider_is_enabled_changes_nothing(self):
        response = self.client.post(reverse("import_consent"), {"consent": "on"}, follow=True)
        self.assertContains(response, "not available")
        self.assertIsNone(self.reload().llm_import_consent_at)

    @override_settings(**AI_ENABLED)
    def test_L2_ticking_records_the_time_and_the_current_version(self):
        self.client.post(reverse("import_consent"), {"consent": "on"})
        profile = self.reload()
        self.assertIsNotNone(profile.llm_import_consent_at)
        self.assertEqual(profile.llm_import_consent_version, "v1")
        self.assertIn('name="consent" checked', self.page())

    @override_settings(**AI_ENABLED)
    def test_L2_unticking_withdraws_consent(self):
        self.client.post(reverse("import_consent"), {"consent": "on"})
        self.client.post(reverse("import_consent"), {})
        profile = self.reload()
        self.assertEqual((profile.llm_import_consent_at, profile.llm_import_consent_version), (None, ""))

    @override_settings(**AI_ENABLED)
    def test_L2_a_version_bump_asks_again_and_does_not_count_as_consent(self):
        self.client.post(reverse("import_consent"), {"consent": "on"})
        with self.settings(PROFILE_IMPORT_CONSENT_VERSION="v2"):
            html = self.page()
        self.assertIn("The terms changed", html)
        self.assertNotIn('name="consent" checked', html)

    @override_settings(**AI_ENABLED)
    def test_saving_consent_never_triggers_a_rematch(self):
        self.client.post(reverse("import_consent"), {"consent": "on"})
        self.client.post(reverse("import_consent"), {})
        self.schedule.assert_not_called()

    def test_consent_needs_login_and_a_csrf_token(self):
        self.assertEqual(Client().post(reverse("import_consent")).status_code, 302)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(reverse("import_consent"), {}).status_code, 403)
        self.assertEqual(self.client.get(reverse("import_consent")).status_code, 405)

    @override_settings(**AI_ENABLED)
    def test_one_users_consent_never_affects_another(self):
        self.client.post(reverse("import_consent"), {"consent": "on"})
        self.other.refresh_from_db()
        self.assertIsNone(self.other.llm_import_consent_at)


class GitHubViewTests(_Base):
    def fake_github(self, fail_with=None):
        from apps.accounts.importing import github as gh
        from apps.accounts.tests.test_import_github import FakeResponse, FakeSession, jr, repo

        user = {"login": "octocat", "html_url": "https://github.com/octocat", "blog": "https://octo.dev"}
        routes = {"/repos": jr([repo("a", "Python", stars=3)]), "/users/": jr(user)}
        if fail_with:
            routes = {"/users/": FakeResponse(fail_with)}
        return mock.patch("apps.accounts.importing.github.GitHubClient", return_value=gh.GitHubClient(session=FakeSession(routes), token=""))

    def start(self, username, **post):
        return self.client.post(reverse("import_github"), {"username": username, **post}, follow=True)

    def test_the_import_page_has_the_github_form(self):
        html = self.client.get(reverse("profile_import")).content.decode()
        self.assertIn('name="username"', html)
        self.assertIn("Reads your public profile and repositories only", html)

    def test_GH1_a_bad_username_shows_a_message_and_makes_no_job(self):
        response = self.start("../etc/passwd")
        self.assertContains(response, "not a valid GitHub username")
        self.assertEqual(ImportJob.objects.count(), 0)

    def test_a_username_flows_through_status_to_review_and_apply(self):
        with self.fake_github(), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse("import_github"), {"username": "https://github.com/octocat"})
        job = ImportJob.objects.get()
        self.assertEqual((job.kind, job.status), ("github", "ready"))
        self.assertRedirects(response, reverse("import_status", args=[job.public_id]), fetch_redirect_response=False)
        page = self.client.get(reverse("import_review", args=[job.public_id])).content.decode()
        self.assertIn("https://github.com/octocat", page)
        self.assertIn("Top languages: Python (1)", page)
        self.assertNotIn("Experience and projects found", page)
        post = {"decision__github_url": "accept", "value__github_url": "https://github.com/octocat",
                "decision__portfolio_url": "accept", "value__portfolio_url": "https://octo.dev"}
        self.client.post(reverse("import_apply", args=[job.public_id]), post)
        profile = self.reload()
        self.assertEqual((profile.github_url, profile.portfolio_url), ("https://github.com/octocat", "https://octo.dev"))

    def test_a_failing_github_call_shows_a_friendly_message_on_the_status_page(self):
        with self.fake_github(fail_with=404), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("import_github"), {"username": "nobody-here"})
        job = ImportJob.objects.get()
        self.assertContains(self.client.get(reverse("import_status", args=[job.public_id])), "No GitHub user with that name")

    def test_an_import_in_flight_and_the_hourly_limit_show_messages(self):
        ImportJob.objects.create(profile=self.profile, kind="github", source_kind="github", status="running")
        self.assertContains(self.start("octocat"), "already running")
        ImportJob.objects.all().delete()
        with self.settings(PROFILE_IMPORT_RATE_PER_HOUR=1), mock.patch("apps.accounts.tasks.run_github_import"):
            self.start("octocat")
            ImportJob.objects.update(status=ImportJob.Status.APPLIED)
            self.assertContains(self.start("octocat"), "Try again in an hour")

    def test_login_csrf_and_method_rules(self):
        self.assertEqual(Client().post(reverse("import_github"), {"username": "octocat"}).status_code, 302)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(reverse("import_github"), {"username": "octocat"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("import_github")).status_code, 405)
