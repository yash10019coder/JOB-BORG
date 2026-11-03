"""apply_imported and the shared validators (rules V1-V4, FU, FP, R2-R5, Z1)."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase

from apps.accounts.importing.validators import (
    ImportValueError,
    clean_country,
    clean_field,
    clean_phone,
    clean_tags,
    clean_text,
    clean_titles,
    clean_url,
)
from apps.accounts.models import Profile
from apps.accounts.services import profile_fields
from apps.accounts.services.profile_fields import (
    ImportApplyError,
    ImportState,
    apply_imported,
    import_state,
)

User = get_user_model()


def code(fn, *args, **kwargs):
    with_exc = None
    try:
        fn(*args, **kwargs)
    except ImportValueError as exc:
        with_exc = exc.code
    return with_exc


class UrlValidatorTests(SimpleTestCase):
    def test_FU1_linkedin_is_reduced_to_the_canonical_profile_url(self):
        for raw in (
            "linkedin.com/in/jane-doe-123/",
            "https://www.linkedin.com/in/jane-doe-123?trk=public",
            "in.linkedin.com/in/jane-doe-123#about",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    clean_url(raw, kind="linkedin"), "https://www.linkedin.com/in/jane-doe-123"
                )

    def test_FU1_linkedin_company_school_and_other_hosts_are_rejected(self):
        for raw in ("linkedin.com/company/acme", "linkedin.com/school/mit", "example.com/in/jane-doe"):
            with self.subTest(raw=raw):
                self.assertIsNotNone(code(clean_url, raw, kind="linkedin"))

    def test_FU2_github_profile_only_never_a_repo_or_reserved_name(self):
        self.assertEqual(clean_url("github.com/octocat", kind="github"), "https://github.com/octocat")
        self.assertEqual(clean_url("https://www.github.com/octocat/", kind="github"), "https://github.com/octocat")
        for raw in ("github.com/octocat/hello-world", "github.com/orgs", "github.com/settings", "gitlab.com/octocat"):
            with self.subTest(raw=raw):
                self.assertIsNotNone(code(clean_url, raw, kind="github"))

    def test_FU4_scheme_less_is_assumed_https_and_http_is_rejected(self):
        self.assertEqual(clean_url("jane.dev"), "https://jane.dev")
        self.assertEqual(clean_url("jane.dev/work"), "https://jane.dev/work")
        self.assertEqual(code(clean_url, "http://jane.dev"), "bad_scheme")

    def test_FU4_dangerous_schemes_are_dropped(self):
        for raw in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "ftp://x.com", "mailto:a@b.com", "tel:12345678"):
            with self.subTest(raw=raw):
                self.assertEqual(code(clean_url, raw), "bad_scheme")

    def test_FU5_userinfo_ip_localhost_internal_punycode_and_non_ascii_hosts_are_rejected(self):
        for raw in (
            "https://user:pw@jane.dev",
            "https://127.0.0.1/x",
            "https://[::1]/x",
            "https://localhost/x",
            "https://printer.local",
            "https://intranet.internal",
            "https://xn--jane-9ua.dev",
            "https://jané.dev",
            "https://jane.dev:8080",
            "https://nodot",
        ):
            with self.subTest(raw=raw):
                self.assertIsNotNone(code(clean_url, raw), raw)

    def test_FU5_trailing_punctuation_is_stripped_and_length_capped(self):
        self.assertEqual(clean_url("jane.dev)."), "https://jane.dev")
        self.assertEqual(code(clean_url, "https://jane.dev/" + "a" * 300), "length")

    def test_FU6_a_broken_url_is_dropped_not_repaired(self):
        self.assertIsNotNone(code(clean_url, "https://jane. dev"))
        self.assertIsNotNone(code(clean_url, "https://jane.d"))


class PhoneValidatorTests(SimpleTestCase):
    def test_FP2_keeps_the_written_form_and_never_adds_a_country_code(self):
        self.assertEqual(clean_phone("+91 9876543210"), "+91 9876543210")
        self.assertEqual(clean_phone("(020) 7946-0000"), "(020) 7946-0000")
        self.assertEqual(clean_phone("9876543210"), "9876543210")

    def test_FP2_digit_count_must_be_7_to_15(self):
        self.assertEqual(code(clean_phone, "123456"), "bad_phone")
        self.assertEqual(code(clean_phone, "+" + "1" * 16), "bad_phone")
        self.assertEqual(clean_phone("1234567"), "1234567")

    def test_FP3_date_ranges_decimals_and_letters_are_rejected(self):
        for raw in ("2019-2023", "2019 - 2023", "9.99", "555-CALL-NOW", "12345abc789", ""):
            with self.subTest(raw=raw):
                self.assertEqual(code(clean_phone, raw), "bad_phone")

    def test_length_is_capped_at_the_model_limit(self):
        self.assertEqual(code(clean_phone, "1" * 33), "bad_phone")


class TextAndListValidatorTests(SimpleTestCase):
    def test_V1_text_is_normalized_and_bounded(self):
        self.assertEqual(clean_text("  Jane\n  Doe​ "), "Jane Doe")
        self.assertEqual(code(clean_text, "x" * 300), "length")
        self.assertEqual(code(clean_text, "12345"), "no_letters")

    def test_V1_markup_and_non_text_are_rejected(self):
        self.assertEqual(code(clean_text, "<script>x</script>"), "markup")
        self.assertEqual(code(clean_text, None), "not_text")

    def test_V2_country_must_be_a_known_alpha3(self):
        self.assertEqual(clean_country("ind"), "IND")
        for raw in ("IN", "XXX", "India", "", None):
            with self.subTest(raw=raw):
                self.assertEqual(code(clean_country, raw), "bad_country")

    def test_V2_tags_are_lowercased_deduplicated_and_pattern_checked(self):
        self.assertEqual(clean_tags(["Python", "python", "C++", "node.js"]), ["python", "c++", "node.js"])
        self.assertEqual(code(clean_tags, ["has space"]), "bad_tag")
        self.assertEqual(code(clean_tags, [f"t{i}" for i in range(51)]), "too_many")
        self.assertEqual(code(clean_tags, "python"), "not_list")

    def test_FT1_titles_are_3_to_80_chars_not_sentences_and_deduplicated(self):
        self.assertEqual(clean_titles(["Backend Engineer", "backend engineer"]), ["Backend Engineer"])
        self.assertEqual(code(clean_titles, ["QA"]), "length")
        self.assertEqual(code(clean_titles, ["Built many things."]), "sentence")

    def test_Z1_only_importable_fields_have_a_validator(self):
        for field in ("email", "mailing_address", "visa_status_by_country", "salary_by_region",
                      "citizenship_countries", "working_timezone"):
            with self.subTest(field=field):
                self.assertEqual(code(clean_field, field, "x"), "not_importable")


class _Base(TestCase):
    def setUp(self):
        patcher = mock.patch("apps.matching.signals.schedule_rematch")
        self.addCleanup(patcher.stop)
        self.schedule = patcher.start()
        self.profile = User.objects.create_user(username="alice", password="pw").profile
        self.schedule.reset_mock()

    def reload(self):
        self.profile.refresh_from_db()
        return self.profile

    def set_provenance(self, key, source, locked=False):
        provenance = dict(self.profile.field_provenance or {})
        provenance[key] = {"source": source, "locked": locked, "updated_at": "2026-01-01T00:00:00+00:00", "detail": ""}
        Profile.objects.filter(pk=self.profile.pk).update(field_provenance=provenance)
        self.profile.refresh_from_db()


class ApplyImportedTests(_Base):
    def test_R1_empty_fields_are_written_and_stamped_imported_with_the_job_detail(self):
        result = apply_imported(
            self.profile.pk,
            {"full_name": "Jane Doe", "phone": "+91 9876543210", "linkedin_url": "linkedin.com/in/jane-doe"},
            detail="job-1",
        )
        profile = self.reload()
        self.assertEqual(sorted(result.applied), ["full_name", "linkedin_url", "phone"])
        self.assertEqual(profile.linkedin_url, "https://www.linkedin.com/in/jane-doe")
        for key in result.applied:
            entry = profile.field_provenance[key]
            self.assertEqual((entry["source"], entry["locked"], entry["detail"]), ("imported", False, "job-1"))

    def test_R2_user_set_learned_and_locked_values_are_kept(self):
        Profile.objects.filter(pk=self.profile.pk).update(
            phone="111-222-3333", full_name="Real Name", headline="Learned headline"
        )
        self.profile.refresh_from_db()
        self.set_provenance("full_name", "user", locked=True)
        self.set_provenance("headline", "learned")
        result = apply_imported(
            self.profile.pk, {"phone": "999 888 7777", "full_name": "Other Name", "headline": "New"}
        )
        profile = self.reload()
        self.assertEqual(result.applied, [])
        self.assertEqual(
            result.kept,
            {"phone": ImportState.USER, "full_name": ImportState.LOCKED, "headline": ImportState.LEARNED},
        )
        self.assertEqual((profile.phone, profile.full_name, profile.headline), ("111-222-3333", "Real Name", "Learned headline"))

    def test_R2_a_previously_imported_value_is_replaced_by_a_new_import(self):
        apply_imported(self.profile.pk, {"phone": "+91 9876543210"}, detail="job-1")
        result = apply_imported(self.profile.pk, {"phone": "+91 9123456780"}, detail="job-2")
        profile = self.reload()
        self.assertEqual(result.applied, ["phone"])
        self.assertEqual((profile.phone, profile.field_provenance["phone"]["detail"]), ("+91 9123456780", "job-2"))

    def test_R2_an_identical_imported_value_is_unchanged_and_not_saved(self):
        apply_imported(self.profile.pk, {"phone": "+91 9876543210"})
        self.schedule.reset_mock()
        before = self.reload().updated_at
        result = apply_imported(self.profile.pk, {"phone": "+91 9876543210"})
        self.assertEqual((result.applied, result.unchanged), ([], ["phone"]))
        self.assertEqual(self.reload().updated_at, before)

    def test_R3_a_value_the_user_edited_in_review_is_stamped_user(self):
        apply_imported(self.profile.pk, {"phone": "+91 9876543210", "full_name": "Jane Doe"}, edited={"phone"}, detail="job-1")
        profile = self.reload()
        self.assertEqual(profile.field_provenance["phone"]["source"], "user")
        self.assertEqual(profile.field_provenance["phone"]["detail"], "")
        self.assertEqual(profile.field_provenance["full_name"]["source"], "imported")

    def test_R4_several_fields_are_one_save_and_one_rematch(self):
        saves = []
        post_save.connect(
            lambda **kw: saves.append(1), sender=Profile, dispatch_uid="count-saves", weak=False
        )
        self.addCleanup(post_save.disconnect, sender=Profile, dispatch_uid="count-saves")
        apply_imported(
            self.profile.pk,
            {"phone": "+91 9876543210", "target_tags": ["python", "golang"], "target_titles": ["Backend Engineer"], "full_name": "Jane Doe"},
        )
        self.assertEqual(len(saves), 1)
        self.assertEqual(self.schedule.call_count, 1)

    def test_R4_a_non_matching_field_alone_triggers_no_rematch(self):
        apply_imported(self.profile.pk, {"phone": "+91 9876543210", "headline": "Engineer"})
        self.schedule.assert_not_called()

    def test_R5_a_field_the_user_changed_since_the_review_rendered_is_kept(self):
        """The page was rendered with an empty phone; the user then typed one."""
        Profile.objects.filter(pk=self.profile.pk).update(phone="555-0100-999")
        result = apply_imported(self.profile.pk, {"phone": "+91 9876543210", "full_name": "Jane Doe"})
        profile = self.reload()
        self.assertEqual(profile.phone, "555-0100-999")
        self.assertEqual(result.kept, {"phone": ImportState.USER})
        self.assertEqual(result.applied, ["full_name"])

    def test_V4_any_invalid_value_writes_nothing_at_all(self):
        with self.assertRaises(ImportApplyError) as caught:
            apply_imported(
                self.profile.pk,
                {"full_name": "Jane Doe", "portfolio_url": "javascript:alert(1)", "location_country": "ZZZ"},
            )
        self.assertEqual(caught.exception.errors, {"portfolio_url": "bad_scheme", "location_country": "bad_country"})
        profile = self.reload()
        self.assertEqual((profile.full_name, profile.portfolio_url, profile.field_provenance), ("", "", {}))

    def test_Z1_non_importable_fields_are_refused_outright(self):
        for key in ("visa_status_by_country", "salary_by_region", "citizenship_countries",
                    "working_timezone", "mailing_address", "resume_text", "email"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                apply_imported(self.profile.pk, {key: "x"})

    def test_import_state_reports_each_source(self):
        self.assertEqual(import_state(self.profile, "phone"), ImportState.EMPTY)
        Profile.objects.filter(pk=self.profile.pk).update(phone="123456789")
        self.profile.refresh_from_db()
        self.assertEqual(import_state(self.profile, "phone"), ImportState.USER)  # legacy value = user's
        self.set_provenance("phone", "imported")
        self.assertEqual(import_state(self.profile, "phone"), ImportState.IMPORTED)
        self.set_provenance("phone", "user", locked=True)
        self.assertEqual(import_state(self.profile, "phone"), ImportState.LOCKED)

    def test_a_user_edit_through_the_form_path_still_beats_a_later_import(self):
        before = profile_fields.snapshot(self.profile)
        self.profile.target_titles = ["Staff Engineer"]
        profile_fields.record_user_edits(self.profile, before)
        self.profile.save()
        result = apply_imported(self.profile.pk, {"target_titles": ["Backend Engineer"]})
        self.assertEqual(result.kept, {"target_titles": ImportState.USER})
        self.assertEqual(self.reload().target_titles, ["Staff Engineer"])
