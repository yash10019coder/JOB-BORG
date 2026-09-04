"""Tests for the Greenhouse browser-automation client.

Exercised against a real headless Chromium instance (via Playwright) with
route interception serving local HTML fixtures, so `getByRole`/`getByLabel`
locator behavior, `expect()` assertions, and the accessibility snapshot are
genuine rather than mocked -- while never touching a live Greenhouse page,
per U3's verification requirement. The whole class is skipped when Playwright
or its Chromium binary isn't available in the current environment (see the
module docstring note below for how to enable it).

To run these for real:
    pip install playwright && playwright install chromium
"""
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase

from apps.auto_apply.greenhouse_form.client import GreenhouseFormClient
from apps.auto_apply.greenhouse_form.exceptions import (
    GreenhouseFormChallenged,
    GreenhouseFormError,
    GreenhouseFormSchemaMismatch,
    GreenhouseFormSubmissionFailed,
    GreenhouseFormVerificationFailed,
)
from apps.auto_apply.greenhouse_form.field_mapping import (
    CHECKBOX_ACKNOWLEDGEMENT,
    CHECKBOX_GROUP,
    COMBOBOX_SELECT,
    FILE,
    MULTI_SELECT,
    SINGLE_SELECT,
    TEXT,
    TEXTAREA,
    FormField,
    FormSchema,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
JOB_URL = "https://job-boards.greenhouse.io/acme/jobs/12345"
DISALLOWED_URL = "https://evil.example.com/acme/jobs/12345"


def _fixture_html(name: str) -> str:
    return (FIXTURES / name).read_text()


try:
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_IMPORTABLE = True
except ImportError:
    _PLAYWRIGHT_IMPORTABLE = False


def _playwright_chromium_available() -> bool:
    if not _PLAYWRIGHT_IMPORTABLE:
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001 -- any launch failure means "unavailable"
        return False


_SKIP_REASON = (
    "Playwright is not importable, or `playwright install chromium` has not "
    "been run in this environment -- these tests drive a real headless "
    "Chromium instance against local HTML fixtures."
)
_BROWSER_AVAILABLE = _playwright_chromium_available()


class _TestContextHandle:
    """Test-only ContextHandle: closes only the per-test context, not the
    class-shared browser/Playwright driver those contexts were spawned
    from."""

    def __init__(self, context):
        self._context = context

    def new_page(self):
        return self._context.new_page()

    def close(self) -> None:
        self._context.close()


def _routed_context_factory(browser, url: str, html: str, calls: list | None = None):
    """Build a `context_factory` that hands the client a *real* isolated
    Playwright context, with `url` intercepted to serve `html` locally --
    the client's own `page.goto(url)` call is satisfied without any network
    access, exercising the real navigation/DOM/accessibility-tree code
    paths rather than a mock."""

    def factory():
        if calls is not None:
            calls.append(1)
        context = browser.new_context()
        context.route(url, lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        return _TestContextHandle(context)

    return factory


class _AlwaysTrueSolver:
    def solve(self, challenge, timeout):
        return True


class _AlwaysFalseSolver:
    def solve(self, challenge, timeout):
        return False


class _RaisingSolver:
    def solve(self, challenge, timeout):
        raise TimeoutError("solver timed out")


@unittest.skipUnless(_BROWSER_AVAILABLE, _SKIP_REASON)
class GreenhouseFormClientTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._playwright = sync_playwright().start()
        cls._browser = cls._playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._playwright.stop()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self._tmpdir = Path(tempfile.mkdtemp(prefix="gh-form-test-"))
        self.addCleanup(shutil.rmtree, self._tmpdir, ignore_errors=True)

    def _client(self, html: str, *, url: str = JOB_URL, calls: list | None = None, **kwargs):
        factory = _routed_context_factory(self._browser, url, html, calls=calls)
        return GreenhouseFormClient(context_factory=factory, **kwargs)

    def _resume_file(self) -> Path:
        path = self._tmpdir / "resume.pdf"
        path.write_bytes(b"%PDF-1.4 fake resume content")
        return path

    # -- inspect(): standard-fields-only fixture -------------------------

    def test_inspect_standard_form_returns_expected_fields(self):
        client = self._client(_fixture_html("greenhouse_standard_form.html"))
        schema = client.inspect(JOB_URL)

        by_label = schema.by_label()
        self.assertEqual(
            set(by_label),
            {"First Name", "Last Name", "Email", "Phone", "Resume/CV"},
        )
        self.assertEqual(by_label["First Name"].field_type, TEXT)
        self.assertTrue(by_label["First Name"].required)
        self.assertEqual(by_label["Phone"].field_type, TEXT)
        self.assertFalse(by_label["Phone"].required)
        self.assertEqual(by_label["Resume/CV"].field_type, FILE)
        self.assertTrue(by_label["Resume/CV"].required)

    # -- inspect(): custom-question fixture -------------------------------

    def test_inspect_custom_questions_form_identifies_field_types(self):
        client = self._client(_fixture_html("greenhouse_custom_questions_form.html"))
        schema = client.inspect(JOB_URL)

        by_label = schema.by_label()
        self.assertEqual(by_label["Why do you want to work here?"].field_type, TEXTAREA)
        self.assertTrue(by_label["Why do you want to work here?"].required)

        work_auth = by_label["Are you legally authorized to work in the US?"]
        self.assertEqual(work_auth.field_type, SINGLE_SELECT)
        self.assertTrue(work_auth.required)
        self.assertEqual(set(work_auth.options), {"Select...", "Yes", "No"})

        tech_stack = by_label["Which of the following technologies have you used professionally?"]
        self.assertEqual(tech_stack.field_type, MULTI_SELECT)
        self.assertFalse(tech_stack.required)
        self.assertEqual(set(tech_stack.options), {"Python", "JavaScript", "Go", "Rust"})

    # -- inspect(): react-select-style combobox + duplicate file labels ---

    def test_inspect_discovers_combobox_and_disambiguates_file_labels(self):
        # Reproduces what live verification against a real Greenhouse board
        # found: get_by_label(exact=True) fails to resolve a control that
        # carries both `for`/`id` *and* `aria-labelledby`, because it
        # matches against the label's raw textContent (asterisk included)
        # rather than the accessible name -- silently dropping the field
        # from the schema. Also reproduces Greenhouse's file-upload widget,
        # where both Resume/CV and Cover Letter's real <input type=file>
        # are labelled identically ("Attach"), and the human-meaningful
        # name lives on the surrounding group instead.
        client = self._client(_fixture_html("greenhouse_combobox_and_file_upload_form.html"))
        schema = client.inspect(JOB_URL)

        by_label = schema.by_label()
        self.assertEqual(
            set(by_label),
            {
                "First Name",
                "Email",
                "Are you authorized to work?",
                "Favorite Country",
                "Resume/CV",
                "Cover Letter",
                "Which languages do you know?",
            },
        )

        combobox_field = by_label["Are you authorized to work?"]
        self.assertEqual(combobox_field.field_type, COMBOBOX_SELECT)
        self.assertTrue(combobox_field.required)
        self.assertEqual(set(combobox_field.options), {"Yes", "No"})

        self.assertEqual(by_label["Resume/CV"].field_type, FILE)
        self.assertEqual(by_label["Resume/CV"].control_id, "resume")
        self.assertEqual(by_label["Cover Letter"].field_type, FILE)
        self.assertEqual(by_label["Cover Letter"].control_id, "cover_letter")

        # Reproduces the real Blacksky Greenhouse board: individual
        # checkboxes sharing a <fieldset><legend>, previously entirely
        # unsupported (raised GreenhouseFormSchemaMismatch as required
        # field type "checkbox").
        checkbox_field = by_label["Which languages do you know?"]
        self.assertEqual(checkbox_field.field_type, CHECKBOX_GROUP)
        self.assertTrue(checkbox_field.required)
        self.assertEqual(set(checkbox_field.options), {"Python", "Go", "Rust"})

    # -- inspect()/submit(): standalone checkbox (no enclosing fieldset) ----

    def test_inspect_discovers_standalone_checkbox_as_acknowledgement_field(self):
        # A standalone checkbox (no <fieldset><legend>) was previously
        # entirely invisible to discovery -- _checkbox_group_field()
        # returned None and the caller silently `continue`d past it. Now
        # discovered as its own CHECKBOX_ACKNOWLEDGEMENT field.
        client = self._client(_fixture_html("greenhouse_standalone_checkbox_form.html"))
        schema = client.inspect(JOB_URL)

        by_label = schema.by_label()
        self.assertIn("I agree to the Terms of Service", by_label)
        terms_field = by_label["I agree to the Terms of Service"]
        self.assertEqual(terms_field.field_type, CHECKBOX_ACKNOWLEDGEMENT)
        self.assertTrue(terms_field.required)
        self.assertEqual(terms_field.options, ("Yes", "No"))
        self.assertEqual(terms_field.control_id, "terms")

        # Edge case: a non-required standalone checkbox is discovered too,
        # correctly marked not required.
        newsletter_field = by_label["Subscribe me to the newsletter"]
        self.assertEqual(newsletter_field.field_type, CHECKBOX_ACKNOWLEDGEMENT)
        self.assertFalse(newsletter_field.required)

    def test_submit_checks_standalone_checkbox_answered_yes(self):
        client = self._client(_fixture_html("greenhouse_standalone_checkbox_form.html"))
        result = client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Email": "ada@example.com",
                "Resume/CV": str(self._resume_file()),
                "I agree to the Terms of Service": "Yes",
            },
        )
        self.assertTrue(result.success)

    def test_submit_leaves_standalone_checkbox_unchecked_when_answered_no(self):
        # A non-required standalone checkbox answered "No" must not be
        # checked, and must not block submission.
        client = self._client(_fixture_html("greenhouse_standalone_checkbox_form.html"))
        result = client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Email": "ada@example.com",
                "Resume/CV": str(self._resume_file()),
                "I agree to the Terms of Service": "Yes",
                "Subscribe me to the newsletter": "No",
            },
        )
        self.assertTrue(result.success)

    def test_submit_required_standalone_checkbox_unanswered_blocks_native_submit(self):
        # Fail-closed guard (R2): the fixture's own native `required`
        # blocks the real form submit when "terms" is left unchecked --
        # confirms an unanswered required standalone checkbox is a genuine
        # blocker end-to-end, not silently ignorable, now that it's a
        # real, discovered, is_supported field rather than invisible.
        client = self._client(
            _fixture_html("greenhouse_standalone_checkbox_form.html"), confirmation_timeout_ms=500
        )
        with self.assertRaises(GreenhouseFormSubmissionFailed):
            client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Email": "ada@example.com",
                    "Resume/CV": str(self._resume_file()),
                },
            )

    def test_inspect_waits_for_combobox_options_that_render_after_the_listbox_opens(self):
        # Reproduces a live-verified race: a Greenhouse "Degree"/"School"
        # combobox's listbox container becomes visible immediately on open,
        # but its real <option> entries render ~300ms later. The old
        # _extract_options() only waited for the (empty) container to
        # become visible, then read whatever options existed at that
        # instant -- racing ahead of the population and recording zero
        # options for a field that genuinely has real choices.
        client = self._client(_fixture_html("greenhouse_delayed_combobox_options_form.html"))
        schema = client.inspect(JOB_URL)

        degree_field = schema.by_label()["Degree"]
        self.assertEqual(degree_field.field_type, COMBOBOX_SELECT)
        self.assertEqual(
            set(degree_field.options),
            {"Associate's Degree", "Bachelor's Degree", "Master's Degree", "Other"},
        )

    def test_inspect_scrolls_combobox_to_load_options_beyond_the_first_page(self):
        # Reproduces a live-verified gap: a Greenhouse "School" combobox is
        # backed by a remote paginated API (2,466 entries at 100/page on
        # the live board) -- a bare open only ever renders the first page.
        # Scrolling the listbox's last option into view is what triggers
        # the widget to fetch and render the next page; without that,
        # _extract_options() only ever captured page 1, and the just-added
        # option-constraint validation would then wrongly reject any real
        # answer that happened to live on a later page.
        client = self._client(_fixture_html("greenhouse_paginated_combobox_options_form.html"))
        schema = client.inspect(JOB_URL)

        school_field = schema.by_label()["School"]
        self.assertEqual(school_field.field_type, COMBOBOX_SELECT)
        # All 3 fixture pages (12 total) should load -- the fixture has
        # far fewer pages than a real 2,466-entry field, well inside
        # _COMBOBOX_SCROLL_ITERATIONS's bound, so this asserts full
        # convergence, not just "more than the first page".
        self.assertEqual(len(school_field.options), 12)
        self.assertIn("Aalborg University", school_field.options)
        self.assertIn("Adams State University", school_field.options)

    # -- inspect(): education-API-backed combobox (School/Degree/Discipline) ---

    class _FakeEducationSession:
        """Test double for `requests.Session` -- no real HTTP call is ever
        made. Records every GET."""

        def __init__(self, responses_by_page=None, raises=None):
            self.responses_by_page = responses_by_page or {}
            self.raises = raises
            self.calls: list[dict] = []

        def get(self, url, params=None, timeout=None):
            self.calls.append({"url": url, "params": params, "timeout": timeout})
            if self.raises is not None:
                raise self.raises
            return self.responses_by_page[params["page"]]

    class _FakeEducationResponse:
        def __init__(self, status_code=200, json_body=None):
            self.status_code = status_code
            self._json_body = json_body or {}

        def json(self):
            return self._json_body

    def test_inspect_uses_full_education_api_list_when_available(self):
        # The fixture's DOM-scraped list is only 2 schools; the education
        # API (mocked here, never hitting a real network) reports 3 --
        # the API list should entirely replace the DOM-scraped one, not
        # merge with or lose out to it.
        cache.clear()
        session = self._FakeEducationSession(
            {
                1: self._FakeEducationResponse(
                    200,
                    {
                        "items": [
                            {"id": 1, "text": "API School A"},
                            {"id": 2, "text": "API School B"},
                            {"id": 3, "text": "API School C"},
                        ],
                        "meta": {"total_count": 3},
                    },
                )
            }
        )
        client = self._client(
            _fixture_html("greenhouse_education_api_backed_combobox_form.html"),
            education_api_session=session,
        )

        schema = client.inspect(JOB_URL)

        school_field = schema.by_label()["School"]
        self.assertEqual(
            set(school_field.options), {"API School A", "API School B", "API School C"}
        )
        self.assertNotIn("DOM Sample School A", school_field.options)
        # JOB_URL's board token is "acme" -- confirms the real request URL
        # shape, not just that *a* request happened.
        self.assertEqual(
            session.calls[0]["url"],
            "https://boards.greenhouse.io/v1/boards/acme/education/schools",
        )

    def test_inspect_falls_back_to_dom_options_when_education_api_fails(self):
        # A board without this endpoint enabled, a network error, or any
        # other API failure must never lose the field entirely -- fall
        # back to whatever the DOM scrape already found.
        cache.clear()
        session = self._FakeEducationSession(raises=ConnectionError("boom"))
        client = self._client(
            _fixture_html("greenhouse_education_api_backed_combobox_form.html"),
            education_api_session=session,
        )

        schema = client.inspect(JOB_URL)

        school_field = schema.by_label()["School"]
        self.assertEqual(set(school_field.options), {"DOM Sample School A", "DOM Sample School B"})

    def test_inspect_does_not_call_education_api_for_non_education_combobox(self):
        # A combobox whose control_id doesn't match Greenhouse's Education
        # block naming (e.g. "Are you authorized to work?", a plain
        # work_auth id) must never trigger an education API call.
        cache.clear()
        session = self._FakeEducationSession()
        client = self._client(
            _fixture_html("greenhouse_combobox_and_file_upload_form.html"),
            education_api_session=session,
        )

        client.inspect(JOB_URL)

        self.assertEqual(session.calls, [])

    # -- inspect()/submit(): aria-labelledby-only combobox (no <label>) ---

    def test_inspect_discovers_aria_labelledby_only_combobox_fields(self):
        # Reproduces real captured accessibility-tree evidence (Alpaca EEO
        # questions, Location (City)): a bare text node -- not a <label> --
        # followed by a role="combobox" input whose accessible name comes
        # purely from aria-labelledby. The label-based discovery pass never
        # finds these; this is the direct fix for "many fields remain
        # unfilled".
        client = self._client(_fixture_html("greenhouse_aria_labelledby_only_form.html"))
        schema = client.inspect(JOB_URL)

        by_label = schema.by_label()
        self.assertEqual(set(by_label), {"First Name", "Gender", "Location (City)"})

        gender_field = by_label["Gender"]
        self.assertEqual(gender_field.field_type, COMBOBOX_SELECT)
        self.assertTrue(gender_field.required)
        self.assertEqual(set(gender_field.options), {"Male", "Female", "Decline to self-identify"})

        location_field = by_label["Location (City)"]
        self.assertEqual(location_field.field_type, COMBOBOX_SELECT)
        self.assertFalse(location_field.required)

    def test_inspect_does_not_double_count_labelled_and_aria_labelledby_field(self):
        # Regression guard for the de-dup path: a field discoverable via a
        # real <label for=...> (which also happens to carry aria-labelledby,
        # like the existing combobox_and_file_upload fixture) must be
        # counted exactly once, not once per discovery pass.
        client = self._client(_fixture_html("greenhouse_combobox_and_file_upload_form.html"))
        schema = client.inspect(JOB_URL)

        matches = [f for f in schema.fields if f.label == "Are you authorized to work?"]
        self.assertEqual(len(matches), 1)

    def test_submit_fills_aria_labelledby_only_combobox_fields(self):
        client = self._client(_fixture_html("greenhouse_aria_labelledby_only_form.html"))
        schema = client.inspect(JOB_URL)

        submit_client = self._client(_fixture_html("greenhouse_aria_labelledby_only_form.html"))
        result = submit_client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Gender": "Female",
                "Location (City)": "New York, NY",
            },
            expected_schema=schema,
        )
        self.assertTrue(result.success)

    # -- inspect()/submit(): aria-label-only fields (no <label>, no aria-labelledby) ---

    def test_inspect_discovers_aria_label_only_fields(self):
        # Controls with aria-label directly on them (no <label> element and
        # no aria-labelledby) are a valid accessibility pattern that should
        # be discovered. Currently not handled by either the main label-based
        # discovery pass or the aria_labelledby_only pass.
        client = self._client(_fixture_html("greenhouse_aria_label_only_form.html"))
        schema = client.inspect(JOB_URL)

        by_label = schema.by_label()
        self.assertEqual(set(by_label), {"First Name", "Email Address", "Country of Residence"})

        email_field = by_label["Email Address"]
        self.assertEqual(email_field.field_type, TEXT)
        self.assertTrue(email_field.required)

        country_field = by_label["Country of Residence"]
        self.assertEqual(country_field.field_type, COMBOBOX_SELECT)
        self.assertFalse(country_field.required)

    def test_submit_fills_aria_label_only_fields(self):
        client = self._client(_fixture_html("greenhouse_aria_label_only_form.html"))
        schema = client.inspect(JOB_URL)

        submit_client = self._client(_fixture_html("greenhouse_aria_label_only_form.html"))
        result = submit_client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Email Address": "ada@example.com",
                "Country of Residence": "Canada",
            },
            expected_schema=schema,
        )
        self.assertTrue(result.success)

    def test_submit_combobox_prefers_option_starting_with_typed_value(self):
        # Reproduces what live verification against a real Greenhouse board
        # (Alpaca) found: typing "India" into a phone country-code combobox
        # matches both "India +91" and "British Indian Ocean Territory
        # +246" as a substring, and clicking the first DOM match (rather
        # than the one that *starts with* the typed value) silently
        # selected the wrong country.
        client = self._client(_fixture_html("greenhouse_combobox_and_file_upload_form.html"))
        schema = client.inspect(JOB_URL)

        resume_path = self._tmpdir / "resume.pdf"
        resume_path.write_bytes(b"%PDF-1.4 fake resume")

        submit_client = self._client(_fixture_html("greenhouse_combobox_and_file_upload_form.html"))
        result = submit_client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Email": "ada@example.com",
                "Are you authorized to work?": "Yes",
                "Favorite Country": "India",
                "Resume/CV": str(resume_path),
                "Which languages do you know?": ["Python"],
            },
            expected_schema=schema,
        )

        self.assertTrue(result.success)
        self.assertIn("Selected country: India", result.confirmation_text)

    def test_submit_fills_combobox_and_distinct_file_fields(self):
        client = self._client(_fixture_html("greenhouse_combobox_and_file_upload_form.html"))
        schema = client.inspect(JOB_URL)

        resume_path = self._tmpdir / "resume.pdf"
        resume_path.write_bytes(b"%PDF-1.4 fake resume")
        cover_path = self._tmpdir / "cover.pdf"
        cover_path.write_bytes(b"%PDF-1.4 fake cover letter")

        submit_client = self._client(_fixture_html("greenhouse_combobox_and_file_upload_form.html"))
        result = submit_client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Email": "ada@example.com",
                "Are you authorized to work?": "Yes",
                "Resume/CV": str(resume_path),
                "Cover Letter": str(cover_path),
                "Which languages do you know?": ["Python", "Rust"],
            },
            expected_schema=schema,
        )

        self.assertTrue(result.success)

    # -- required unsupported field type ----------------------------------

    def test_required_unsupported_field_type_raises_schema_mismatch(self):
        client = self._client(_fixture_html("greenhouse_unsupported_required_field_form.html"))
        with self.assertRaises(GreenhouseFormSchemaMismatch):
            client.inspect(JOB_URL)

    # -- submit(): schema drift between draft and send --------------------

    def test_submit_against_drifted_schema_raises_schema_mismatch(self):
        inspect_client = self._client(_fixture_html("greenhouse_custom_questions_form.html"))
        expected_schema = inspect_client.inspect(JOB_URL)

        submit_client = self._client(_fixture_html("greenhouse_custom_questions_form_drifted.html"))
        with self.assertRaises(GreenhouseFormSchemaMismatch):
            submit_client.submit(
                JOB_URL,
                {"First Name": "Ada"},
                expected_schema=expected_schema,
            )

    # -- challenge handling -------------------------------------------------

    def test_inspect_challenge_page_raises_challenged(self):
        client = self._client(_fixture_html("greenhouse_challenge_form.html"))
        with self.assertRaises(GreenhouseFormChallenged):
            client.inspect(JOB_URL)

    def test_inspect_invisible_recaptcha_badge_is_not_treated_as_challenged(self):
        # Reproduces what live verification against a real Greenhouse board
        # (Alpaca) found: reCAPTCHA v3/Enterprise's always-present
        # background-scoring badge renders an iframe with `title=
        # "reCAPTCHA"` -- matching the title-based half of
        # `_challenge_detected()`'s selector -- even though its `src` marks
        # it `size=invisible` (no interactive challenge, nothing blocking
        # submission). A prior version excluded `size=invisible` only from
        # the src-based selector clause, not the title-based one, so this
        # badge still tripped a false GreenhouseFormChallenged.
        client = self._client(_fixture_html("greenhouse_invisible_recaptcha_badge_form.html"))
        schema = client.inspect(JOB_URL)
        self.assertIn("First Name", schema.by_label())

    # -- unexpected-error wrapping ------------------------------------------

    def test_inspect_unexpected_error_is_wrapped_as_greenhouse_form_error(self):
        """A raw, unexpected error (a Playwright/browser failure, or any
        other bug) during inspect() must surface as a typed
        GreenhouseFormError rather than propagating raw -- otherwise it
        would skip past draft_for()'s GreenhouseFormError handlers straight
        to draft_auto_apply's catch-all, which persists no AutoApplyDraft
        row at all, silently dropping the job with no trace in the review
        queue."""
        client = self._client(_fixture_html("greenhouse_standard_form.html"))
        with patch.object(
            GreenhouseFormClient, "_discover_schema", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(GreenhouseFormError) as ctx:
                client.inspect(JOB_URL)
        self.assertIn("Could not load the application form", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, GreenhouseFormChallenged)
        self.assertNotIsInstance(ctx.exception, GreenhouseFormSchemaMismatch)

    def test_submit_challenge_with_no_solver_raises_challenged_without_filling(self):
        client = self._client(_fixture_html("greenhouse_challenge_form.html"))
        with patch.object(GreenhouseFormClient, "_fill_answers") as fill_mock:
            with self.assertRaises(GreenhouseFormChallenged):
                client.submit(JOB_URL, {"First Name": "Ada"})
        fill_mock.assert_not_called()

    def test_submit_challenge_with_solver_returning_true_proceeds(self):
        client = self._client(
            _fixture_html("greenhouse_standard_form.html"),
            captcha_solver=_AlwaysTrueSolver(),
        )
        with patch.object(GreenhouseFormClient, "_challenge_detected", side_effect=[True, False]):
            result = client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Last Name": "Lovelace",
                    "Email": "ada@example.com",
                    "Resume/CV": str(self._resume_file()),
                },
            )
        self.assertTrue(result.success)

    def test_submit_captcha_solver_returning_false_raises_challenged(self):
        client = self._client(
            _fixture_html("greenhouse_challenge_form.html"),
            captcha_solver=_AlwaysFalseSolver(),
        )
        with self.assertRaises(GreenhouseFormChallenged):
            client.submit(JOB_URL, {"First Name": "Ada"})

    def test_submit_captcha_solver_raising_is_treated_as_challenged(self):
        client = self._client(
            _fixture_html("greenhouse_challenge_form.html"),
            captcha_solver=_RaisingSolver(),
        )
        with self.assertRaises(GreenhouseFormChallenged):
            client.submit(JOB_URL, {"First Name": "Ada"})

    # -- happy-path submit() integration -----------------------------------

    def test_submit_happy_path_confirms_success(self):
        client = self._client(_fixture_html("greenhouse_custom_questions_form.html"))
        schema = client.inspect(JOB_URL)

        submit_client = self._client(_fixture_html("greenhouse_custom_questions_form.html"))
        result = submit_client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Last Name": "Lovelace",
                "Email": "ada@example.com",
                "Phone": "555-0100",
                "Resume/CV": str(self._resume_file()),
                "Why do you want to work here?": "Because I love hard problems.",
                "Are you legally authorized to work in the US?": "Yes",
                "Which of the following technologies have you used professionally?": [
                    "Python",
                    "Go",
                ],
            },
            expected_schema=schema,
        )

        self.assertTrue(result.success)
        self.assertIn("submitted successfully", result.confirmation_text.lower())

    def test_submit_single_select_mismatch_raises_typed_submission_failed(self):
        # U5: a SINGLE_SELECT answer with no matching option must fail
        # closed with a typed, clear-message exception -- not a raw
        # Playwright TimeoutError bubbling up from select_option().
        client = self._client(_fixture_html("greenhouse_custom_questions_form.html"))
        schema = client.inspect(JOB_URL)

        submit_client = self._client(
            _fixture_html("greenhouse_custom_questions_form.html"), confirmation_timeout_ms=500
        )
        with self.assertRaises(GreenhouseFormSubmissionFailed) as ctx:
            submit_client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Last Name": "Lovelace",
                    "Email": "ada@example.com",
                    "Phone": "555-0100",
                    "Resume/CV": str(self._resume_file()),
                    "Why do you want to work here?": "Because I love hard problems.",
                    "Are you legally authorized to work in the US?": "Maybe, unclear",
                    "Which of the following technologies have you used professionally?": [
                        "Python",
                        "Go",
                    ],
                },
                expected_schema=schema,
            )

        message = str(ctx.exception)
        self.assertIn("No matching option", message)
        self.assertIn("Are you legally authorized to work in the US?", message)

    def test_submit_confirms_success_via_text_pattern_without_status_role(self):
        # Reproduces what live verification against a real Greenhouse board
        # found: the confirmation view carries no role="status" (or any
        # other ARIA live-region role) at all, only human-readable text.
        client = self._client(_fixture_html("greenhouse_confirmation_text_only_form.html"))
        schema = client.inspect(JOB_URL)

        submit_client = self._client(_fixture_html("greenhouse_confirmation_text_only_form.html"))
        result = submit_client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Email": "ada@example.com",
                "Resume/CV": str(self._resume_file()),
            },
            expected_schema=schema,
        )

        self.assertTrue(result.success)
        self.assertIn("thanks for applying", result.confirmation_text.lower())

    # -- disabled Submit button diagnostic -----------------------------------

    def test_submit_disabled_button_raises_diagnostic_naming_blocking_field(self):
        # Confirmed live (8/10 historical `submission_failed` rows): a
        # client-side-disabled Submit makes the click a silent no-op, which
        # previously surfaced only as the opaque "No post-submit success
        # signal found" after the full poll budget elapsed. Leaving the
        # required combobox unanswered keeps the fixture's Submit disabled.
        client = self._client(
            _fixture_html("greenhouse_disabled_submit_form.html"), confirmation_timeout_ms=500
        )
        with self.assertRaises(GreenhouseFormSubmissionFailed) as ctx:
            client.submit(JOB_URL, {"First Name": "Ada"})

        message = str(ctx.exception)
        self.assertIn("disabled", message.lower())
        self.assertIn("Are you eligible to work in this location?", message)

    def test_submit_enabled_button_after_all_required_fields_filled_proceeds(self):
        # Regression guard: once every required field is answered, Submit
        # becomes enabled and the click proceeds exactly as before.
        client = self._client(_fixture_html("greenhouse_disabled_submit_form.html"))
        result = client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Are you eligible to work in this location?": "Yes",
            },
        )
        self.assertTrue(result.success)

    def test_submit_disabled_button_diagnostic_does_not_blame_a_correctly_filled_combobox(self):
        # Regression test for a code-review finding (P0/P2, correctness +
        # adversarial agreement): _fill_combobox() never leaves a native
        # value on the control (the widget tracks the choice elsewhere --
        # see its own docstring), so a naive input_value()=="" check would
        # misreport every correctly-answered combobox as still-blocking.
        # Here the combobox IS answered but First Name is left blank, so
        # the real blocker is First Name -- the combobox must not appear.
        client = self._client(
            _fixture_html("greenhouse_disabled_submit_form.html"), confirmation_timeout_ms=500
        )
        with self.assertRaises(GreenhouseFormSubmissionFailed) as ctx:
            client.submit(JOB_URL, {"Are you eligible to work in this location?": "Yes"})

        message = str(ctx.exception)
        self.assertIn("First Name", message)
        self.assertNotIn("Are you eligible to work in this location?", message)

    # -- submission failure + debug artifacts --------------------------------

    def test_submit_rejected_form_raises_submission_failed_with_debug_artifacts(self):
        debug_dir = self._tmpdir / "debug"
        client = self._client(
            _fixture_html("greenhouse_submission_rejected_form.html"),
            debug_artifact_dir=debug_dir,
            confirmation_timeout_ms=500,
        )
        with self.assertRaises(GreenhouseFormSubmissionFailed) as ctx:
            client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Email": "ada@example.com",
                    "Resume/CV": str(self._resume_file()),
                },
            )

        artifacts = ctx.exception.debug_artifacts
        self.assertIsNotNone(artifacts)
        self.assertTrue(Path(artifacts.screenshot_path).exists())
        self.assertTrue(Path(artifacts.accessibility_tree_path).exists())

    # -- verification interstitial detection (U5) ---------------------------

    def test_submit_verification_interstitial_raises_verification_failed(self):
        # Happy path (U5 scope): a code-entry control (autocomplete=
        # "one-time-code" + inputmode="numeric") AND confirming copy ("We
        # sent a verification code...") are both present -> a distinct,
        # typed outcome, not success and not GreenhouseFormSubmissionFailed.
        client = self._client(_fixture_html("greenhouse_email_verification_form.html"))
        with self.assertRaises(GreenhouseFormVerificationFailed) as ctx:
            client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Email": "ada@example.com",
                    "Resume/CV": str(self._resume_file()),
                },
            )
        self.assertNotIsInstance(ctx.exception, GreenhouseFormSubmissionFailed)

    def test_submit_verification_multibox_code_fills_each_box_and_confirms(self):
        # Reproduces real captured evidence (1785860486-8e3b8ab2-a11y.yaml):
        # the verification code control is 8 separate single-character
        # boxes, not one input holding the whole code. A provider returning
        # an 8-character code must have each character distributed to the
        # correct box, not typed entirely into the first one.
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome

        class _FakeProvider:
            def get_code(self, *, since, deadline_monotonic):
                return CodeLookupResult(outcome=VerificationOutcome.FOUND, code="87654321")

        client = self._client(_fixture_html("greenhouse_email_verification_multibox_form.html"))
        result = client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Email": "ada@example.com",
                "Resume/CV": str(self._resume_file()),
            },
            email_code_provider=_FakeProvider(),
            deadline_monotonic=time.monotonic() + 60,
        )
        self.assertTrue(result.success)

    def test_submit_verification_multibox_length_mismatch_raises_code_rejected(self):
        # Regression test for a code-review finding (P1, adversarial +
        # testing agreement): every multi-box character input also matches
        # `_VERIFICATION_CODE_INPUT_SELECTOR` (each carries
        # inputmode="numeric"), so a naive fallback would silently stuff a
        # too-short/too-long code into box #1 alone instead of raising.
        # The fixture has 8 boxes; a 6-character code must raise a typed
        # verification failure, not misfill.
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome

        class _FakeProvider:
            def get_code(self, *, since, deadline_monotonic):
                return CodeLookupResult(outcome=VerificationOutcome.FOUND, code="654321")

        client = self._client(_fixture_html("greenhouse_email_verification_multibox_form.html"))
        with self.assertRaises(GreenhouseFormVerificationFailed) as ctx:
            client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Email": "ada@example.com",
                    "Resume/CV": str(self._resume_file()),
                },
                email_code_provider=_FakeProvider(),
                deadline_monotonic=time.monotonic() + 60,
            )
        self.assertEqual(ctx.exception.outcome, VerificationOutcome.CODE_REJECTED)

    def test_submit_bare_multibox_interstitial_still_detected(self):
        # Regression test for the bug fixed here: the real interstitial
        # captured in media/auto_apply_debug/1786259666-c46a2165-a11y.yaml
        # rendered 8 single-character code boxes carrying NONE of
        # `_VERIFICATION_CODE_INPUT_SELECTOR`'s assumed attributes (no
        # autocomplete="one-time-code", no inputmode="numeric", no
        # name/id/placeholder containing "code") -- only `maxlength="1"`.
        # Before this fix, detection's code_input.count() was 0 and the
        # interstitial was silently missed, falling through to the generic
        # GreenhouseFormSubmissionFailed instead of routing into email
        # verification at all.
        client = self._client(_fixture_html("greenhouse_verification_multibox_bare_form.html"))
        with self.assertRaises(GreenhouseFormVerificationFailed) as ctx:
            client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Email": "ada@example.com",
                    "Resume/CV": str(self._resume_file()),
                },
            )
        self.assertNotIsInstance(ctx.exception, GreenhouseFormSubmissionFailed)

    def test_verification_interstitial_detected_true_for_bare_multibox_shape(self):
        client = GreenhouseFormClient(context_factory=_TestContextHandle)
        mock_page = MagicMock()

        def _locator(sel):
            if "maxlength" in sel:
                return MagicMock(count=lambda: 8)
            if sel == "body":
                return MagicMock(
                    inner_text=lambda: "A verification code was sent to you. Enter the code below."
                )
            return MagicMock(count=lambda: 0)

        mock_page.locator.side_effect = _locator

        self.assertTrue(client._verification_interstitial_detected(mock_page))

    def test_verification_interstitial_not_falsely_triggered_by_single_stray_box(self):
        # A lone maxlength="1" input elsewhere on the page (e.g. a
        # single-character field unrelated to verification) must not alone
        # satisfy the control-signal -- mirrors `_fill_verification_code()`'s
        # own `box_count >= 2` threshold for the same shape.
        client = GreenhouseFormClient(context_factory=_TestContextHandle)
        mock_page = MagicMock()

        def _locator(sel):
            if "maxlength" in sel:
                return MagicMock(count=lambda: 1)
            return MagicMock(count=lambda: 0)

        mock_page.locator.side_effect = _locator

        self.assertFalse(client._verification_interstitial_detected(mock_page))

    def test_verification_interstitial_not_falsely_triggered_without_confirming_copy(self):
        # The multi-box control signal alone is never enough -- confirming
        # copy is still required (existing two-signal design, unchanged).
        client = GreenhouseFormClient(context_factory=_TestContextHandle)
        mock_page = MagicMock()

        def _locator(sel):
            if "maxlength" in sel:
                return MagicMock(count=lambda: 8)
            if sel == "body":
                return MagicMock(inner_text=lambda: "Nothing relevant here.")
            return MagicMock(count=lambda: 0)

        mock_page.locator.side_effect = _locator

        self.assertFalse(client._verification_interstitial_detected(mock_page))

    # -- submit(): bounded post-fill re-discovery for revealed fields -------

    def test_submit_no_revealed_fields_behaves_identically(self):
        # R7 happy path: choosing "Referral" never reveals the "specify"
        # field -- the extra re-discovery pass finds nothing new and
        # submission proceeds exactly as before this change.
        client = self._client(_fixture_html("greenhouse_reveals_required_field_on_select_form.html"))
        result = client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Email": "ada@example.com",
                "Resume/CV": str(self._resume_file()),
                "How did you hear about us?": "Referral",
            },
        )
        self.assertTrue(result.success)

    def test_submit_revealed_required_field_raises_schema_mismatch(self):
        # R5/R6: choosing "Other" reveals a new required "Please specify"
        # field that was never in the drafted answers -- there is no
        # answer for a field that didn't exist at draft time, so this
        # fails closed before Submit is ever clicked, rather than silently
        # submitting incomplete or hanging on the generic timeout path.
        client = self._client(_fixture_html("greenhouse_reveals_required_field_on_select_form.html"))
        with self.assertRaises(GreenhouseFormSchemaMismatch) as ctx:
            client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Email": "ada@example.com",
                    "Resume/CV": str(self._resume_file()),
                    "How did you hear about us?": "Other",
                },
            )
        self.assertIn("Please specify", str(ctx.exception))

    def test_submit_normal_success_fixture_still_resolves_as_success(self):
        # Regression guard: an ordinary success fixture must remain
        # unaffected by the new verification-interstitial branch.
        client = self._client(_fixture_html("greenhouse_custom_questions_form.html"))
        schema = client.inspect(JOB_URL)

        submit_client = self._client(_fixture_html("greenhouse_custom_questions_form.html"))
        result = submit_client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Last Name": "Lovelace",
                "Email": "ada@example.com",
                "Phone": "555-0100",
                "Resume/CV": str(self._resume_file()),
                "Why do you want to work here?": "Because I love hard problems.",
                "Are you legally authorized to work in the US?": "Yes",
                "Which of the following technologies have you used professionally?": [
                    "Python",
                    "Go",
                ],
            },
            expected_schema=schema,
        )
        self.assertTrue(result.success)

    def test_submit_success_wins_tie_against_verification_lookalike_signals(self):
        # Tie-break: success copy overlapping a verification phrase ("check
        # your email for next steps") PLUS a stray numeric input elsewhere
        # on the page must still classify as success -- success is checked
        # first, every pass.
        client = self._client(
            _fixture_html("greenhouse_success_with_verification_lookalike_form.html")
        )
        result = client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Email": "ada@example.com",
                "Resume/CV": str(self._resume_file()),
            },
        )
        self.assertTrue(result.success)

    def test_submit_validation_error_page_unchanged_submission_failed(self):
        # A genuine validation-error page (no verification markup at all)
        # must keep raising the existing GreenhouseFormSubmissionFailed,
        # unaffected by the new classifier branch.
        client = self._client(
            _fixture_html("greenhouse_submission_rejected_form.html"),
            confirmation_timeout_ms=500,
        )
        with self.assertRaises(GreenhouseFormSubmissionFailed):
            client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Email": "ada@example.com",
                    "Resume/CV": str(self._resume_file()),
                },
            )

    def test_submit_numeric_input_without_verification_copy_is_submission_failed(self):
        # A numeric-shaped input present with NO verification copy
        # alongside it is only one of the two required signals -> falls
        # through to the existing GreenhouseFormSubmissionFailed, exactly
        # like the reCAPTCHA-v3 single-signal false positive this codebase
        # already fixed once.
        client = self._client(
            _fixture_html("greenhouse_stray_numeric_input_no_verification_copy_form.html"),
            confirmation_timeout_ms=500,
        )
        with self.assertRaises(GreenhouseFormSubmissionFailed):
            client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Email": "ada@example.com",
                    "Resume/CV": str(self._resume_file()),
                },
            )

    def test_submit_neither_signal_respects_single_shared_timeout_budget(self):
        # Worst-case timing: a page matching neither success nor
        # verification must still respect the EXISTING poll/timeout
        # budget -- the verification check must not add a second, separate
        # timeout on top of it.
        client = self._client(
            _fixture_html("greenhouse_submission_rejected_form.html"),
            confirmation_timeout_ms=500,
        )
        start = time.monotonic()
        with self.assertRaises(GreenhouseFormSubmissionFailed):
            client.submit(
                JOB_URL,
                {
                    "First Name": "Ada",
                    "Email": "ada@example.com",
                    "Resume/CV": str(self._resume_file()),
                },
            )
        elapsed = time.monotonic() - start
        # Generous upper bound: comfortably under 2x the configured
        # confirmation_timeout_ms (which would indicate a second, separate
        # wait being added for the verification check), with slack for
        # real browser/navigation overhead.
        self.assertLess(elapsed, 3.0)

    # -- hostname allowlist -------------------------------------------------

    def test_disallowed_hostname_rejected_before_navigation(self):
        calls: list = []
        client = self._client(
            _fixture_html("greenhouse_standard_form.html"), url=DISALLOWED_URL, calls=calls
        )
        with self.assertRaises(GreenhouseFormError):
            client.inspect(DISALLOWED_URL)
        self.assertEqual(calls, [])  # context_factory (and thus navigation) never invoked


class SchemaMatchesTests(SimpleTestCase):
    """Direct unit coverage of the option-set-aware drift comparison, since
    the integration tests above exercise it only indirectly."""

    def test_identical_schemas_match(self):
        from apps.auto_apply.greenhouse_form.field_mapping import schema_matches

        a = FormSchema(fields=(FormField("Email", TEXT, True),))
        b = FormSchema(fields=(FormField("Email", TEXT, True),))
        self.assertTrue(schema_matches(a, b))

    def test_option_set_drift_is_detected_regardless_of_order(self):
        from apps.auto_apply.greenhouse_form.field_mapping import schema_matches

        a = FormSchema(fields=(FormField("Auth", SINGLE_SELECT, True, ("Yes", "No")),))
        # Same set, different order -> still a match.
        b = FormSchema(fields=(FormField("Auth", SINGLE_SELECT, True, ("No", "Yes")),))
        self.assertTrue(schema_matches(a, b))
        # A genuinely added option -> drift.
        c = FormSchema(
            fields=(FormField("Auth", SINGLE_SELECT, True, ("Yes", "No", "Sponsorship")),)
        )
        self.assertFalse(schema_matches(a, c))

    def test_missing_field_is_drift(self):
        from apps.auto_apply.greenhouse_form.field_mapping import schema_matches

        a = FormSchema(fields=(FormField("Email", TEXT, True), FormField("Phone", TEXT, False)))
        b = FormSchema(fields=(FormField("Email", TEXT, True),))
        self.assertFalse(schema_matches(a, b))


class SchemaSerializationTests(SimpleTestCase):
    """`schema_to_dict`/`schema_from_dict` round-trip -- this is what lets
    `AutoApplyDraft.form_schema_snapshot` be passed back to `submit()` as
    `expected_schema` at send time (a plain JSONField dict/list/str shape,
    no custom encoder)."""

    def test_round_trip_preserves_all_field_data(self):
        from apps.auto_apply.greenhouse_form.field_mapping import (
            schema_from_dict,
            schema_to_dict,
        )

        original = FormSchema(
            fields=(
                FormField("Email", TEXT, True),
                FormField("Auth", SINGLE_SELECT, True, ("Yes", "No")),
                FormField("Stack", MULTI_SELECT, False, ("Python", "Go")),
            )
        )

        restored = schema_from_dict(schema_to_dict(original))

        self.assertEqual(restored, original)

    def test_none_and_empty_input_return_none(self):
        from apps.auto_apply.greenhouse_form.field_mapping import schema_from_dict

        self.assertIsNone(schema_from_dict(None))
        self.assertIsNone(schema_from_dict({}))


class ValidatedFilePathTests(SimpleTestCase):
    """Direct unit coverage of the FILE-field defense-in-depth check --
    the last line of defense before a value reaches Playwright's
    `set_input_files()`, in case a bug or future code path ever lets a
    bad value get this far (the primary defense is that
    `edit_auto_apply_draft` refuses to let a user edit a FILE-type
    answer at all -- see apps/web/views.py)."""

    def test_existing_file_path_is_returned_resolved(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            resolved = GreenhouseFormClient._validated_file_path(tmp.name, "Resume/CV")
        self.assertEqual(resolved, Path(tmp.name).resolve())

    def test_nonexistent_path_raises_greenhouse_form_error(self):
        with self.assertRaises(GreenhouseFormError):
            GreenhouseFormClient._validated_file_path(
                "/nonexistent/path/resume.pdf", "Resume/CV"
            )

    def test_directory_path_raises_greenhouse_form_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(GreenhouseFormError):
                GreenhouseFormClient._validated_file_path(tmpdir, "Resume/CV")


class EmailVerificationProviderIntegrationTests(SimpleTestCase):
    def test_no_provider_raises_no_inbox_credentials(self):
        from apps.auto_apply.email_verification.base import VerificationOutcome

        client = GreenhouseFormClient(context_factory=_TestContextHandle)
        mock_page = MagicMock()
        mock_page.locator.side_effect = lambda sel: (
            MagicMock(count=lambda: 1)
            if "code" in sel
            else MagicMock(count=lambda: 0, inner_text=lambda: "enter your verification code")
        )
        status_reg = MagicMock()
        status_reg.count.return_value = 0
        mock_page.get_by_role.return_value = status_reg

        with self.assertRaises(GreenhouseFormVerificationFailed) as cm:
            client._confirm_success(mock_page, provider=None)

        self.assertEqual(cm.exception.outcome, VerificationOutcome.NO_INBOX_CREDENTIALS)

    def test_provider_returns_found_code_types_and_confirms(self):
        from apps.auto_apply.email_verification.base import VerificationOutcome

        client = GreenhouseFormClient(context_factory=_TestContextHandle)
        mock_page = MagicMock()

        code_input = MagicMock()
        submit_btn = MagicMock()
        status_reg = MagicMock()
        status_reg.count.return_value = 0

        call_count = {"val": 0}

        def mock_locator(sel):
            if "code" in sel:
                return MagicMock(count=lambda: 1 if call_count["val"] == 0 else 0, first=code_input)
            if "button" in sel or "input[type='submit']" in sel:
                return MagicMock(first=submit_btn)
            if sel == "body":
                if call_count["val"] == 0:
                    return MagicMock(inner_text=lambda: "enter your verification code")
                return MagicMock(inner_text=lambda: "Thank you for applying")
            return MagicMock(count=lambda: 0)

        mock_page.locator.side_effect = mock_locator
        mock_page.get_by_role.return_value = status_reg

        def on_click():
            call_count["val"] = 1

        submit_btn.click.side_effect = on_click

        mock_provider = MagicMock()
        from apps.auto_apply.email_verification.base import CodeLookupResult
        mock_provider.get_code.return_value = CodeLookupResult(
            outcome=VerificationOutcome.FOUND, code="654321"
        )

        result = client._confirm_success(
            mock_page, provider=mock_provider, deadline_monotonic=time.monotonic() + 300
        )
        self.assertTrue(result.success)
        code_input.fill.assert_called_with("654321")

    def test_post_code_failure_suppresses_debug_artifacts(self):
        from apps.auto_apply.email_verification.base import VerificationOutcome

        with tempfile.TemporaryDirectory() as tmpdir:
            client = GreenhouseFormClient(
                context_factory=_TestContextHandle, debug_artifact_dir=tmpdir
            )
            mock_page = MagicMock()

            code_input = MagicMock()
            submit_btn = MagicMock()
            submit_btn.click.side_effect = Exception("Page crash during submit")
            status_reg = MagicMock()
            status_reg.count.return_value = 0

            mock_page.locator.side_effect = lambda sel: (
                MagicMock(count=lambda: 1, first=code_input)
                if "code" in sel
                else (
                    MagicMock(first=submit_btn)
                    if "button" in sel
                    else MagicMock(count=lambda: 0, inner_text=lambda: "enter your verification code")
                )
            )
            mock_page.get_by_role.return_value = status_reg

            mock_provider = MagicMock()
            from apps.auto_apply.email_verification.base import CodeLookupResult
            mock_provider.get_code.return_value = CodeLookupResult(
                outcome=VerificationOutcome.FOUND, code="654321"
            )

            with self.assertRaises(GreenhouseFormVerificationFailed) as cm:
                client._confirm_success(
                    mock_page, provider=mock_provider, deadline_monotonic=time.monotonic() + 300
                )


            self.assertEqual(cm.exception.outcome, VerificationOutcome.CODE_REJECTED)
            self.assertIsNone(cm.exception.debug_artifacts)
            self.assertEqual(len(list(Path(tmpdir).glob("*"))), 0)

    def test_post_code_success_check_exception_also_suppresses_debug_artifacts(self):
        """Regression test for a code-review finding (P0, adversarial):
        the post-code success check (after code fill+click succeed) sat
        outside the try/except that suppresses debug artifacts, so an
        exception from THAT call (e.g. a Playwright "execution context
        destroyed" navigation race) could escape and capture a screenshot
        with the just-typed OTP still on screen -- exactly what R9 forbids."""
        from apps.auto_apply.email_verification.base import VerificationOutcome

        with tempfile.TemporaryDirectory() as tmpdir:
            client = GreenhouseFormClient(
                context_factory=_TestContextHandle, debug_artifact_dir=tmpdir
            )
            mock_page = MagicMock()

            code_input = MagicMock()
            submit_btn = MagicMock()
            mock_page.locator.side_effect = lambda sel: (
                MagicMock(count=lambda: 1, first=code_input)
                if "code" in sel
                else MagicMock(first=submit_btn)
            )

            mock_provider = MagicMock()
            from apps.auto_apply.email_verification.base import CodeLookupResult
            mock_provider.get_code.return_value = CodeLookupResult(
                outcome=VerificationOutcome.FOUND, code="654321"
            )

            with patch.object(
                client,
                "_check_success_signal",
                side_effect=[None, Exception("execution context was destroyed")],
            ), patch.object(
                client, "_verification_interstitial_detected", return_value=True
            ):
                with self.assertRaises(GreenhouseFormVerificationFailed) as cm:
                    client._confirm_success(
                        mock_page,
                        provider=mock_provider,
                        deadline_monotonic=time.monotonic() + 300,
                    )

            self.assertEqual(cm.exception.outcome, VerificationOutcome.CODE_REJECTED)
            self.assertIsNone(cm.exception.debug_artifacts)
            self.assertEqual(len(list(Path(tmpdir).glob("*"))), 0)


