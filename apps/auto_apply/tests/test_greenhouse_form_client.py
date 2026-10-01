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
from django.test import SimpleTestCase, override_settings

from apps.auto_apply.greenhouse_form.client import _SETTLE_TIMEOUT_MS, GreenhouseFormClient
from apps.auto_apply.greenhouse_form.exceptions import (
    GreenhouseFormChallenged,
    GreenhouseFormError,
    GreenhouseFormSchemaMismatch,
    GreenhouseFormSubmissionFailed,
    GreenhouseFormSubmissionUnconfirmed,
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

    def test_preexisting_confirmation_and_arbitrary_status_do_not_confirm(self):
        for message in ("Thank you for applying", "Uploading resume", "Please fix the errors"):
            with self.subTest(message=message):
                html = f'<body><p role="status">{message}</p><form onsubmit="event.preventDefault()"><button>Submit</button></form></body>'
                client = self._client(html, confirmation_timeout_ms=10)
                from apps.auto_apply.greenhouse_form.exceptions import GreenhouseFormSubmissionUnconfirmed
                with self.assertRaises(GreenhouseFormSubmissionUnconfirmed):
                    client.submit(JOB_URL, {})

    def test_changed_generic_status_does_not_confirm(self):
        html = """<body><p role="status">Ready</p><form onsubmit="event.preventDefault();document.querySelector('p').textContent='Uploading resume'"><button>Submit</button></form></body>"""
        client = self._client(html, confirmation_timeout_ms=10)
        from apps.auto_apply.greenhouse_form.exceptions import GreenhouseFormSubmissionUnconfirmed
        with self.assertRaises(GreenhouseFormSubmissionUnconfirmed):
            client.submit(JOB_URL, {})

    def test_redirect_to_disallowed_origin_rejected_before_inspecting_or_filling(self):
        def factory():
            context = self._browser.new_context()
            redirect_html = f'<script>setTimeout(() => location.replace("{DISALLOWED_URL}"), 50)</script>'
            context.route(JOB_URL, lambda route: route.fulfill(content_type="text/html", body=redirect_html))
            context.route(DISALLOWED_URL, lambda route: route.fulfill(body='<form><button>Submit</button></form>'))
            return _TestContextHandle(context)
        client = GreenhouseFormClient(context_factory=factory)
        for operation in (lambda: client.inspect(JOB_URL), lambda: client.submit(JOB_URL, {})):
            with patch.object(client, "_discover_schema") as discover, patch.object(client, "_fill_answers") as fill:
                with self.assertRaises(GreenhouseFormError):
                    operation()
                discover.assert_not_called()
                fill.assert_not_called()

    def test_origin_revalidated_immediately_before_submit(self):
        client = self._client('<body><form><button>Submit</button></form></body>')
        with patch.object(client, "_fill_answers", side_effect=lambda page, *_: page.goto("about:blank")), patch.object(client, "_click_submit") as click:
            with self.assertRaises(GreenhouseFormError):
                client.submit(JOB_URL, {})
            click.assert_not_called()

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
        # Neither <input type=file> itself carries a required/aria-required
        # attribute in this fixture (see its own comment) -- required-ness
        # for a FILE field must come from the group's own label asterisk
        # instead (`_group_label_and_required_for`), not the input.
        self.assertTrue(by_label["Resume/CV"].required)
        self.assertFalse(by_label["Cover Letter"].required)

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
        # The scroll loop stopped growing before hitting the iteration cap
        # -- a strong signal this really is the whole list.
        self.assertTrue(school_field.options_complete)

    def test_inspect_marks_options_incomplete_when_still_growing_at_scroll_cap(self):
        # The mirror case: more pages than _COMBOBOX_SCROLL_ITERATIONS can
        # exhaust -- the option count is still growing on the last scroll
        # attempt, so discovery must not claim completeness (the live
        # combobox search could still find a later-page value at submit
        # time -- see answer_resolution.Question.options_enforced).
        client = self._client(_fixture_html("greenhouse_combobox_still_growing_at_scroll_cap_form.html"))
        schema = client.inspect(JOB_URL)

        school_field = schema.by_label()["School"]
        self.assertLess(len(school_field.options), 32)  # never reached all 8 pages
        self.assertFalse(school_field.options_complete)

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
        self.assertTrue(school_field.options_complete)
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
        # This fixture's DOM sample is a static, non-paginated 2-item list
        # (see its own comment) -- the scroll loop finds no growth on the
        # very first attempt, which is itself a legitimate completeness
        # signal, independent of the (failed) education API.
        self.assertTrue(school_field.options_complete)

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

    def test_submit_education_api_combobox_scrolls_to_find_option_typing_cannot_filter(self):
        # Regression test for a real production failure: School/Degree/
        # Discipline (education-API-backed, see education_api.py) is a
        # widget shape that only paginates via a real scroll interaction --
        # typing into it never filters the rendered listbox, unlike the
        # react-select-style Country/Location fields `_fill_combobox()` was
        # originally verified against. A genuinely valid answer
        # ("Information Systems", only rendered after a scroll loads its
        # page) must not raise "No matching option found".
        html = _fixture_html("greenhouse_discipline_scroll_not_type_filter_form.html")
        client = self._client(html)
        schema = client.inspect(JOB_URL)

        submit_client = self._client(html)
        result = submit_client.submit(
            JOB_URL,
            {
                "First Name": "Ada",
                "Discipline": "Information Systems",
            },
            expected_schema=schema,
        )

        self.assertTrue(result.success)

    def test_preexisting_status_text_is_not_treated_as_a_confirmation(self):
        # A status region already on the page before Submit (e.g. an autosave
        # notice) must not count as success; with nothing new after the click
        # the submission is "unconfirmed", not silently marked applied.
        html = _fixture_html("greenhouse_preexisting_status_form.html")
        schema = self._client(html).inspect(JOB_URL)

        with self.assertRaises(GreenhouseFormSubmissionUnconfirmed):
            self._client(html, confirmation_timeout_ms=800).submit(
                JOB_URL, {"First Name": "Ada"}, expected_schema=schema
            )

    def test_no_confirmation_after_click_raises_unconfirmed_subclass_of_failed(self):
        html = _fixture_html("greenhouse_preexisting_status_form.html")
        schema = self._client(html).inspect(JOB_URL)

        with self.assertRaises(GreenhouseFormSubmissionFailed) as ctx:
            self._client(html, confirmation_timeout_ms=800).submit(
                JOB_URL, {"First Name": "Ada"}, expected_schema=schema
            )
        self.assertIsInstance(ctx.exception, GreenhouseFormSubmissionUnconfirmed)

    def test_confirmation_exception_after_click_is_unconfirmed(self):
        client = self._client(_fixture_html("greenhouse_preexisting_status_form.html"))
        with patch.object(client, "_click_submit", wraps=client._click_submit) as click, patch.object(
            client, "_confirm_success", side_effect=RuntimeError("confirmation failed")
        ):
            with self.assertRaises(GreenhouseFormSubmissionUnconfirmed):
                client.submit(JOB_URL, {"First Name": "Ada"})
        click.assert_called_once()

    def test_click_exception_after_dispatch_is_unconfirmed(self):
        from playwright.sync_api import Locator

        original_click = Locator.click

        def click_then_raise(locator, *args, **kwargs):
            original_click(locator, *args, **kwargs)
            raise RuntimeError("navigation failed after click")

        client = self._client(_fixture_html("greenhouse_preexisting_status_form.html"))
        with patch.object(Locator, "click", autospec=True, side_effect=click_then_raise), patch.object(
            client, "_confirm_success"
        ) as confirm:
            with self.assertRaises(GreenhouseFormSubmissionUnconfirmed):
                client.submit(JOB_URL, {"First Name": "Ada"})
        confirm.assert_not_called()

    def test_actionability_failure_before_dispatch_is_failed(self):
        from playwright.sync_api import Locator

        original_click = Locator.click
        html = _fixture_html("greenhouse_preexisting_status_form.html").replace(
            '<button type="submit">', '<button type="submit" style="visibility:hidden">'
        )
        client = self._client(html)
        with patch.object(
            Locator, "click", autospec=True,
            side_effect=lambda locator: original_click(locator, timeout=200),
        ):
            with self.assertRaises(GreenhouseFormSubmissionFailed) as caught:
                client.submit(JOB_URL, {"First Name": "Ada"})
        self.assertNotIsInstance(caught.exception, GreenhouseFormSubmissionUnconfirmed)

    def test_unchanged_confirmation_phrase_in_status_is_not_new_evidence(self):
        html = _fixture_html("greenhouse_preexisting_status_form.html").replace(
            "Your progress is saved automatically.", "Thanks for applying!"
        )
        with self.assertRaises(GreenhouseFormSubmissionUnconfirmed):
            self._client(html, confirmation_timeout_ms=300).submit(JOB_URL, {"First Name": "Ada"})

    def test_existing_body_phrase_can_confirm_in_a_new_status_region(self):
        html = _fixture_html("greenhouse_preexisting_status_form.html").replace(
            '<div role="status">Your progress is saved automatically.</div>',
            '<p>thanks for applying</p>',
        ).replace(
            "e.preventDefault();",
            "e.preventDefault(); document.querySelector('p').setAttribute('role', 'status');",
        )
        result = self._client(html).submit(JOB_URL, {"First Name": "Ada"})
        self.assertTrue(result.success)
        self.assertEqual(result.confirmation_text, "thanks for applying")

    def test_updated_status_with_existing_phrase_confirms(self):
        html = _fixture_html("greenhouse_preexisting_status_form.html").replace(
            "Your progress is saved automatically.", "Thanks for applying!"
        ).replace(
            "e.preventDefault();",
            "e.preventDefault(); document.querySelector('[role=status]').textContent = "
            "'Thanks for applying! Application received.';",
        )
        self.assertTrue(self._client(html).submit(JOB_URL, {"First Name": "Ada"}).success)

    def test_existing_phrase_on_new_document_at_same_url_confirms(self):
        html = _fixture_html("greenhouse_preexisting_status_form.html").replace(
            '<div role="status">Your progress is saved automatically.</div>',
            '<p>thanks for applying</p>',
        ).replace("e.preventDefault();", "e.preventDefault(); location.reload();")

        def factory():
            context = self._browser.new_context()
            responses = iter((html, "<html><body>thanks for applying</body></html>"))
            context.route(JOB_URL, lambda route: route.fulfill(
                status=200, content_type="text/html", body=next(responses)
            ))
            return _TestContextHandle(context)

        result = GreenhouseFormClient(context_factory=factory).submit(JOB_URL, {"First Name": "Ada"})
        self.assertTrue(result.success)

    def test_submit_skips_blank_optional_file_and_preserves_required_upload(self):
        html = _fixture_html("greenhouse_combobox_and_file_upload_form.html")
        client = self._client(html)
        schema = client.inspect(JOB_URL)
        self.assertFalse(schema.by_label()["Cover Letter"].required)
        result = self._client(html).submit(
            JOB_URL,
            {
                "First Name": "Ada", "Email": "ada@example.com",
                "Are you authorized to work?": "Yes",
                "Resume/CV": str(self._resume_file()), "Cover Letter": "",
                "Which languages do you know?": ["Python"],
            },
            expected_schema=schema,
        )
        self.assertTrue(result.success)

    def test_stale_required_attribute_on_optional_file_input_does_not_block_submission(self):
        # Real production failure: the <input type=file> for an optional
        # Cover Letter field carries a stale/incorrect aria-required="true",
        # while the group's own visible label has no asterisk. Discovery
        # must trust the group label over the input's own attribute (see
        # `_group_label_and_required_for`), so a blank Cover Letter answer
        # is skipped instead of crashing on `_validated_file_path("", ...)`.
        html = _fixture_html("greenhouse_file_upload_stale_required_attribute_form.html")
        client = self._client(html)
        schema = client.inspect(JOB_URL)
        self.assertFalse(schema.by_label()["Cover Letter"].required)

        result = self._client(html).submit(
            JOB_URL,
            {"First Name": "Ada", "Cover Letter": ""},
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

    def test_submit_textarea_with_crlf_answer_does_not_raise_submission_failed(self):
        # Browsers normalize CRLF to bare LF when a textarea's value is set,
        # so an LLM-drafted multi-paragraph answer containing "\r\n" (as
        # happened on a real production draft) must not be compared
        # verbatim against the post-fill DOM value.
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
                "Why do you want to work here?": "First paragraph.\r\n\r\nSecond paragraph.",
                "Are you legally authorized to work in the US?": "Yes",
                "Which of the following technologies have you used professionally?": [
                    "Python",
                    "Go",
                ],
            },
            expected_schema=schema,
        )

        self.assertTrue(result.success)

    def test_submit_text_input_with_embedded_newline_does_not_raise_submission_failed(self):
        # Single-line `<input type="text">` controls strip newlines from
        # their value entirely (not convert them to "\n" like a textarea
        # does) -- an answer with a stray newline must be normalized the
        # same way before the post-fill DOM-value comparison.
        client = self._client(_fixture_html("greenhouse_custom_questions_form.html"))
        schema = client.inspect(JOB_URL)

        submit_client = self._client(_fixture_html("greenhouse_custom_questions_form.html"))
        result = submit_client.submit(
            JOB_URL,
            {
                "First Name": "Ada\nGrace",
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

    def test_submit_verification_confirmed_but_no_success_signal_raises_unconfirmed(self):
        # Regression test for a real production failure (AutoApplyDraft
        # #266): the code is entered and the Confirm click goes through
        # (no "Invalid code" copy ever appears), but no success signal is
        # ever rendered either -- genuinely ambiguous, not a case we know
        # failed. This must raise GreenhouseFormSubmissionUnconfirmed (so
        # the duplicate-application retry guard in apps/web/views.py
        # applies), NOT a plain GreenhouseFormVerificationFailed with
        # outcome=CODE_REJECTED, which that guard doesn't check for and
        # would let a Retry submit a second real application.
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome
        from apps.auto_apply.greenhouse_form.exceptions import GreenhouseFormSubmissionUnconfirmed

        class _FakeProvider:
            def get_code(self, *, since, deadline_monotonic):
                return CodeLookupResult(outcome=VerificationOutcome.FOUND, code="654321")

        client = self._client(
            _fixture_html("greenhouse_verification_confirmed_but_no_success_signal_form.html"),
            confirmation_timeout_ms=800,
        )
        with self.assertRaises(GreenhouseFormSubmissionUnconfirmed) as ctx:
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
        self.assertNotIsInstance(ctx.exception, GreenhouseFormVerificationFailed)

    def test_submit_verification_button_same_text_as_stale_button_still_clicks_right_one(self):
        # Regression test for a real production failure (Canonical): the
        # interstitial's own submit button is labeled "Submit application"
        # -- the EXACT same text as the stale original application-form
        # button still present in the DOM. Preferring "Verify"-labeled text
        # (the fix for test_submit_verification_overlay_clicks_verify_not_stale_submit_button)
        # does nothing here since neither button says "Verify"; only
        # scoping to the code input's own enclosing <form> disambiguates.
        # Confirmed live: without this fix, the click silently lands on the
        # stale button and the interstitial sits asking for the same code
        # forever with no error -- exactly the "entered but success not
        # confirmed" (GreenhouseFormSubmissionUnconfirmed) symptom seen in
        # production, repeatedly, on this employer's real board.
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome

        class _FakeProvider:
            def get_code(self, *, since, deadline_monotonic):
                return CodeLookupResult(outcome=VerificationOutcome.FOUND, code="654321")

        client = self._client(_fixture_html("greenhouse_verification_same_text_as_stale_submit_form.html"))
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

    def test_submit_verification_overlay_clicks_verify_not_stale_submit_button(self):
        # Real production failure: when the verification interstitial
        # renders as an overlay on top of the original form (rather than
        # replacing it, as greenhouse_email_verification_multibox_form.html
        # does), the original "Submit Application" button (type=submit,
        # text "Submit") stays in the DOM alongside the interstitial's own
        # "Verify" button. The old button selector took whichever the
        # browser found first in DOM order -- clicking the stale button
        # does nothing, so the code is never actually submitted and the
        # page is stuck on the interstitial forever ("Verification code
        # entered but application success not confirmed"). The fix prefers
        # a button whose visible text says "Verify".
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome

        class _FakeProvider:
            def get_code(self, *, since, deadline_monotonic):
                return CodeLookupResult(outcome=VerificationOutcome.FOUND, code="654321")

        client = self._client(_fixture_html("greenhouse_email_verification_overlay_form.html"))
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

    def test_post_fill_discovery_is_passive_and_checks_control_identity(self):
        html = '''<form>
            <label for="school--0">School</label><input id="school--0" role="combobox" required>
            <span id="degree-label">Degree</span><input id="degree--0" role="combobox" aria-labelledby="degree-label" required>
            </form>'''
        client = self._client(html)
        known = FormSchema((
            FormField("School", COMBOBOX_SELECT, True, control_id="school--0"),
            FormField("Degree", COMBOBOX_SELECT, True, control_id="degree--0"),
        ))

        def check(page):
            page.goto(JOB_URL)
            with patch.object(GreenhouseFormClient, "_extract_options") as extract, patch.object(client, "_maybe_full_education_options") as education:
                client._raise_on_newly_revealed_required_fields(page, JOB_URL, known)
                extract.assert_not_called()
                education.assert_not_called()
            page.locator("form").evaluate("el => el.insertAdjacentHTML('beforeend', '<label for=school--1>School</label><input id=school--1 required>')")
            with self.assertRaisesRegex(GreenhouseFormSchemaMismatch, "School"):
                client._raise_on_newly_revealed_required_fields(page, JOB_URL, known)

        client._with_fresh_page(check)


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

    def test_neither_signal_uses_confirmation_timeout_not_the_task_deadline(self):
        # The task passes its whole (multi-minute) budget as deadline_monotonic;
        # that must only bound the email-code lookup, not how long a page that
        # shows neither a success nor a verification signal is polled.
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
                deadline_monotonic=time.monotonic() + 600,
            )
        self.assertLess(time.monotonic() - start, 4.0)

    def test_verification_code_is_not_submitted_once_the_deadline_passes_during_fill(self):
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome

        class _FakeProvider:
            def get_code(self, *, since, deadline_monotonic):
                return CodeLookupResult(outcome=VerificationOutcome.FOUND, code="87654321")

        client = self._client(_fixture_html("greenhouse_email_verification_multibox_form.html"))
        # First check (after the code lookup) passes; the second (after the
        # boxes are filled, before clicking) reports the budget as spent.
        with patch.object(GreenhouseFormClient, "_deadline_expired", side_effect=[False, True]):
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
        self.assertEqual(ctx.exception.outcome, VerificationOutcome.CODE_TIMEOUT)

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
                deadline_monotonic=time.monotonic() + 300,
            )
        elapsed = time.monotonic() - start
        # Generous upper bound: comfortably under 2x the configured
        # confirmation_timeout_ms (which would indicate a second, separate
        # wait being added for the verification check), with slack for
        # real browser/navigation overhead.
        self.assertLess(elapsed, 3.0)

    def test_existing_zip_code_and_confirm_email_label_do_not_trigger_verification(self):
        client = self._client(
            _fixture_html("greenhouse_zip_code_confirm_email_form.html"),
            confirmation_timeout_ms=100,
        )
        with self.assertRaises(GreenhouseFormSubmissionFailed):
            client.submit(JOB_URL, {"Zip Code": "12345", "Confirm your email": "ada@example.com"})

    def test_hidden_verification_control_is_ignored(self):
        context = self._browser.new_context()
        try:
            page = context.new_page()
            page.set_content('<p>Confirm your email</p><input name="code" style="display:none">')
            self.assertFalse(GreenhouseFormClient._verification_interstitial_detected(page))
        finally:
            context.close()

    # -- hostname allowlist -------------------------------------------------

    def test_disallowed_hostname_rejected_before_navigation(self):
        calls: list = []
        client = self._client(
            _fixture_html("greenhouse_standard_form.html"), url=DISALLOWED_URL, calls=calls
        )
        with self.assertRaises(GreenhouseFormError):
            client.inspect(DISALLOWED_URL)
        self.assertEqual(calls, [])  # context_factory (and thus navigation) never invoked


class RediscoveryIdentityTests(SimpleTestCase):
    def test_ids_take_precedence_with_label_fallback_when_either_id_is_missing(self):
        client = GreenhouseFormClient()
        for old_id, new_id, old_label, new_label, is_new in (
            ("first", "second", "School", "School", True),
            ("first", "first", "School", "Renamed", False),
            ("", "first", "School", "School", False),
            ("first", "", "School", "School", False),
            ("", "", "School", "Degree", True),
        ):
            with self.subTest(old_id=old_id, new_id=new_id, new_label=new_label):
                known = FormSchema((FormField(old_label, TEXT, True, control_id=old_id),))
                fresh = FormSchema((FormField(new_label, TEXT, True, control_id=new_id),))
                with patch.object(client, "_discover_schema", return_value=fresh):
                    if is_new:
                        with self.assertRaises(GreenhouseFormSchemaMismatch):
                            client._raise_on_newly_revealed_required_fields(None, JOB_URL, known)
                    else:
                        client._raise_on_newly_revealed_required_fields(None, JOB_URL, known)



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
                FormField("School", COMBOBOX_SELECT, True, ("School A",), "school--0", True),
            )
        )

        restored = schema_from_dict(schema_to_dict(original))

        self.assertEqual(restored, original)

    def test_none_and_empty_input_return_none(self):
        from apps.auto_apply.greenhouse_form.field_mapping import schema_from_dict

        self.assertIsNone(schema_from_dict(None))
        self.assertIsNone(schema_from_dict({}))


class EmptyFileAnswerTests(SimpleTestCase):
    def test_optional_empty_file_skips_validation_and_upload(self):
        client = GreenhouseFormClient()
        schema = FormSchema(fields=(FormField("Upload", FILE, False),))
        control = MagicMock()
        with patch.object(client, "_locate_control", return_value=control), patch.object(
            client, "_validated_file_path", side_effect=GreenhouseFormSubmissionFailed("empty")
        ) as validate:
            client._fill_answers(MagicMock(), schema, {"Upload": ""})
            validate.assert_not_called()
            control.set_input_files.assert_not_called()

    def test_optional_blank_choice_type_fields_are_skipped_not_filled(self):
        # Production failure: an LLM inference failure (or an unfilled
        # optional needs_review placeholder) can leave a choice-type
        # question's answer blank -- attempting to fill it always crashes
        # ("No matching option for '' found..."). Verified for every
        # option-bearing field type, not just FILE.
        client = GreenhouseFormClient()
        schema = FormSchema(fields=(
            FormField("Location (City)", COMBOBOX_SELECT, False, ("NYC", "SF")),
            FormField("Willing to relocate?", SINGLE_SELECT, False, ("Yes", "No")),
            FormField("Languages", MULTI_SELECT, False, ("Python", "Go")),
            FormField("Skills", CHECKBOX_GROUP, False, ("Python", "Go")),
        ))
        with patch.object(client, "_locate_control") as locate:
            client._fill_answers(MagicMock(), schema, {
                "Location (City)": "",
                "Willing to relocate?": "",
                "Languages": [],
                "Skills": [],
            })
            locate.assert_not_called()

    def test_required_blank_choice_type_fields_still_attempt_a_fill(self):
        # A required blank choice-type answer must still reach the fill
        # logic (not be silently skipped) -- same fail-closed posture as a
        # required blank FILE field, which `_validated_file_path` enforces
        # by raising. Here it's enough to confirm the field wasn't skipped
        # before ever locating its control.
        client = GreenhouseFormClient()
        schema = FormSchema(fields=(FormField("Willing to relocate?", SINGLE_SELECT, True, ("Yes", "No")),))
        with patch.object(client, "_locate_control", side_effect=AssertionError("reached fill")) as locate:
            with self.assertRaisesMessage(AssertionError, "reached fill"):
                client._fill_answers(MagicMock(), schema, {"Willing to relocate?": ""})
            locate.assert_called_once()

    def test_required_empty_file_still_validates_and_raises(self):
        client = GreenhouseFormClient()
        schema = FormSchema(fields=(FormField("Upload", FILE, True),))
        control = MagicMock()
        with patch.object(client, "_locate_control", return_value=control), patch.object(
            client, "_validated_file_path", side_effect=GreenhouseFormSubmissionFailed("empty")
        ) as validate:
            with self.assertRaises(GreenhouseFormSubmissionFailed):
                client._fill_answers(MagicMock(), schema, {"Upload": ""})
            validate.assert_called_once_with("", "Upload")
            control.set_input_files.assert_not_called()

    def test_file_upload_waits_for_network_to_settle_before_proceeding(self):
        # Production failure: Greenhouse's upload widget shows the filename
        # immediately (the synchronous DOM `change` event set_input_files()
        # triggers) but uploads the file to its own backend asynchronously.
        # Clicking Submit before that completed left a real board showing
        # "Resume/CV is required." despite the filename chip already being
        # visible. A bounded, best-effort network-idle wait right after
        # set_input_files() -- same pattern as _goto_and_settle() -- gives
        # that upload a chance to finish first.
        client = GreenhouseFormClient()
        schema = FormSchema(fields=(FormField("Resume/CV", FILE, True),))
        control = MagicMock()
        page = MagicMock()
        with patch.object(client, "_locate_control", return_value=control), patch.object(
            client, "_validated_file_path", return_value="/tmp/resume.pdf"
        ):
            client._fill_answers(page, schema, {"Resume/CV": "resumes/resume.pdf"})
            control.set_input_files.assert_called_once_with("/tmp/resume.pdf")
            page.wait_for_load_state.assert_called_once_with(
                "networkidle", timeout=_SETTLE_TIMEOUT_MS
            )

    def test_file_upload_settle_timeout_does_not_propagate(self):
        # The wait is best-effort (mirrors _goto_and_settle()): a page that
        # never truly goes network-idle (an analytics beacon, a long poll)
        # must not turn into a hard submission failure.
        client = GreenhouseFormClient()
        schema = FormSchema(fields=(FormField("Resume/CV", FILE, True),))
        control = MagicMock()
        page = MagicMock()
        page.wait_for_load_state.side_effect = TimeoutError("never idle")
        with patch.object(client, "_locate_control", return_value=control), patch.object(
            client, "_validated_file_path", return_value="/tmp/resume.pdf"
        ):
            client._fill_answers(page, schema, {"Resume/CV": "resumes/resume.pdf"})  # must not raise

    def test_optional_unsupported_field_type_is_skipped_not_filled(self):
        # FormField's own docstring promises this: an unrecognized native
        # input type (e.g. "number", "date") is only fatal when required --
        # verified against a real production failure where a non-required
        # "End date year" <input type=number> crashed with "No fill
        # strategy ... of type 'number'" despite having a valid answer.
        client = GreenhouseFormClient()
        schema = FormSchema(fields=(FormField("End date year", "number", False),))
        with patch.object(client, "_locate_control") as locate:
            client._fill_answers(MagicMock(), schema, {"End date year": "2024"})
            locate.assert_not_called()

    def test_required_unsupported_field_type_still_attempts_a_fill(self):
        # Discovery already raises GreenhouseFormSchemaMismatch for a
        # *required* unsupported field (see _discover_schema), so this
        # shouldn't normally be reachable -- but as defense-in-depth,
        # _fill_answers must not silently skip a required one the way it
        # does an optional one.
        client = GreenhouseFormClient()
        schema = FormSchema(fields=(FormField("End date year", "number", True),))
        with patch.object(client, "_locate_control", side_effect=AssertionError("reached fill")) as locate:
            with self.assertRaisesMessage(AssertionError, "reached fill"):
                client._fill_answers(MagicMock(), schema, {"End date year": "2024"})
            locate.assert_called_once()


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


_EMPTY_BEFORE_SNAPSHOT = {"body": "", "statuses": ()}


class EmailVerificationProviderIntegrationTests(SimpleTestCase):
    def test_no_provider_raises_no_inbox_credentials(self):
        from apps.auto_apply.email_verification.base import VerificationOutcome

        client = GreenhouseFormClient(context_factory=_TestContextHandle)
        mock_page = MagicMock()
        mock_page.url = JOB_URL
        mock_page.locator.side_effect = lambda sel: (
            MagicMock(count=lambda: 1)
            if "code" in sel
            else MagicMock(count=lambda: 0, inner_text=lambda: "enter your verification code")
        )
        status_reg = MagicMock()
        status_reg.count.return_value = 0
        mock_page.get_by_role.return_value = status_reg

        with self.assertRaises(GreenhouseFormVerificationFailed) as cm:
            client._confirm_success(mock_page, _EMPTY_BEFORE_SNAPSHOT, provider=None)

        self.assertEqual(cm.exception.outcome, VerificationOutcome.NO_INBOX_CREDENTIALS)

    def test_provider_returns_found_code_types_and_confirms(self):
        from apps.auto_apply.email_verification.base import VerificationOutcome

        client = GreenhouseFormClient(context_factory=_TestContextHandle)
        mock_page = MagicMock()
        mock_page.url = JOB_URL

        code_input = MagicMock()
        submit_btn = MagicMock()
        status_reg = MagicMock()
        status_reg.count.return_value = 0

        call_count = {"val": 0}

        def mock_locator(sel):
            if "code" in sel:
                return MagicMock(count=lambda: 1 if call_count["val"] == 0 else 0, first=code_input)
            if "Verify" in sel:
                return MagicMock(first=MagicMock(count=lambda: 0))
            if "button" in sel or "input[type='submit']" in sel:
                return MagicMock(first=submit_btn)
            if sel == "body":
                if call_count["val"] == 0:
                    return MagicMock(inner_text=lambda: "enter your verification code")
                return MagicMock(inner_text=lambda: "Thank you for applying")
            return MagicMock(count=lambda: 0)

        mock_page.locator.side_effect = mock_locator
        mock_page.get_by_role.return_value = status_reg

        def on_click(**kwargs):
            call_count["val"] = 1

        submit_btn.click.side_effect = on_click

        mock_provider = MagicMock()
        from apps.auto_apply.email_verification.base import CodeLookupResult
        mock_provider.get_code.return_value = CodeLookupResult(
            outcome=VerificationOutcome.FOUND, code="654321"
        )

        result = client._confirm_success(
            mock_page,
            _EMPTY_BEFORE_SNAPSHOT,
            provider=mock_provider,
            deadline_monotonic=time.monotonic() + 300,
        )
        self.assertTrue(result.success)
        code_input.fill.assert_called_with("654321", timeout=client.navigation_timeout_ms)

    def test_post_code_failure_suppresses_debug_artifacts(self):
        from apps.auto_apply.email_verification.base import VerificationOutcome

        with tempfile.TemporaryDirectory() as tmpdir:
            client = GreenhouseFormClient(
                context_factory=_TestContextHandle, debug_artifact_dir=tmpdir
            )
            mock_page = MagicMock()
            mock_page.url = JOB_URL

            code_input = MagicMock()
            submit_btn = MagicMock()
            submit_btn.click.side_effect = Exception("Page crash during submit")
            status_reg = MagicMock()
            status_reg.count.return_value = 0

            mock_page.locator.side_effect = lambda sel: (
                MagicMock(count=lambda: 1, first=code_input)
                if "code" in sel
                else (
                    MagicMock(first=MagicMock(count=lambda: 0))
                    if "Verify" in sel
                    else (
                        MagicMock(first=submit_btn)
                        if "button" in sel
                        else MagicMock(count=lambda: 0, inner_text=lambda: "enter your verification code")
                    )
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
                    mock_page,
                    _EMPTY_BEFORE_SNAPSHOT,
                    provider=mock_provider,
                    deadline_monotonic=time.monotonic() + 300,
                )

            self.assertEqual(cm.exception.outcome, VerificationOutcome.CODE_REJECTED)
            self.assertIsNone(cm.exception.debug_artifacts)
            self.assertEqual(len(list(Path(tmpdir).glob("*"))), 0)

    def test_interstitial_detection_captures_artifacts_before_any_code_is_typed(self):
        # Deliberately the one safe capture point on the whole verification
        # path: the interstitial is confirmed present but no code has been
        # looked up or typed yet, so a screenshot here can only ever show
        # the bare interstitial -- exactly what's needed to identify a real
        # employer board's actual verify/confirm button text, with no OTP
        # ever at risk of appearing in it. A fully successful run (code
        # found, entered, confirmed on the first post-code check) never
        # touches any of the failure paths' own capture calls, isolating
        # this one call to the interstitial-detection point itself.
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome
        from apps.auto_apply.greenhouse_form.field_mapping import SubmissionResult

        client = GreenhouseFormClient(context_factory=_TestContextHandle)
        mock_page = MagicMock()
        mock_page.locator.return_value.first.count.return_value = 0  # no "Verify"-text button -- use fallback
        mock_provider = MagicMock()
        mock_provider.get_code.return_value = CodeLookupResult(
            outcome=VerificationOutcome.FOUND, code="654321"
        )
        success = SubmissionResult(success=True)

        with patch.object(
            client, "_verification_interstitial_detected", return_value=True
        ), patch.object(client, "_fill_verification_code"), patch.object(
            client, "_check_success_signal", side_effect=[None, success]
        ), patch.object(client, "_capture_debug_artifacts") as capture:
            result = client._confirm_success(
                mock_page, _EMPTY_BEFORE_SNAPSHOT,
                provider=mock_provider, deadline_monotonic=time.monotonic() + 300,
            )
        self.assertIs(result, success)
        capture.assert_called_once_with(mock_page)

    def test_post_code_destroyed_context_recovers_to_confirmed_success(self):
        """A "destroyed execution context" exception from the post-code
        success check almost always means the page just navigated -- likely
        TO the real success page, right as this check read from the
        outgoing document. Verified against a real production failure: this
        previously propagated straight out as a hard "Failed while
        submitting verification code", discarding what was probably a
        genuine success. It must instead be treated as "keep polling" --
        the very next check, against the now-settled page, can still
        confirm success."""
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome
        from apps.auto_apply.greenhouse_form.field_mapping import SubmissionResult

        client = GreenhouseFormClient(context_factory=_TestContextHandle, confirmation_timeout_ms=1000)
        mock_page = MagicMock()
        mock_page.locator.return_value.first.count.return_value = 0  # no "Verify"-text button -- use fallback
        mock_provider = MagicMock()
        mock_provider.get_code.return_value = CodeLookupResult(
            outcome=VerificationOutcome.FOUND, code="654321"
        )
        success = SubmissionResult(success=True)

        with patch.object(
            client,
            "_check_success_signal",
            side_effect=[None, Exception("Execution context was destroyed, most likely because of a navigation"), success],
        ), patch.object(
            client, "_verification_interstitial_detected", return_value=True
        ), patch.object(client, "_fill_verification_code"):
            result = client._confirm_success(
                mock_page, _EMPTY_BEFORE_SNAPSHOT,
                provider=mock_provider, deadline_monotonic=time.monotonic() + 300,
            )
        self.assertIs(result, success)

    def test_post_code_unrelated_success_check_exception_still_suppresses_debug_artifacts(self):
        """Regression test for a code-review finding (P0, adversarial):
        the post-code success check (after code fill+click succeed) sat
        outside the try/except that suppresses debug artifacts, so an
        exception from THAT call could escape and capture a screenshot with
        the just-typed OTP still on screen -- exactly what R9 forbids. A
        "destroyed context" exception specifically is now recovered from
        (see test_post_code_destroyed_context_recovers_to_confirmed_success)
        since it usually signals real progress, not failure -- this test
        covers every *other* kind of exception, which must still fail
        immediately without ever capturing artifacts."""
        from apps.auto_apply.email_verification.base import VerificationOutcome

        with tempfile.TemporaryDirectory() as tmpdir:
            client = GreenhouseFormClient(
                context_factory=_TestContextHandle, debug_artifact_dir=tmpdir
            )
            mock_page = MagicMock()
            mock_page.url = JOB_URL

            code_input = MagicMock()
            submit_btn = MagicMock()
            mock_page.locator.side_effect = lambda sel: (
                MagicMock(count=lambda: 1, first=code_input)
                if "code" in sel
                else (
                    MagicMock(first=MagicMock(count=lambda: 0))
                    if "Verify" in sel
                    else MagicMock(first=submit_btn)
                )
            )

            mock_provider = MagicMock()
            from apps.auto_apply.email_verification.base import CodeLookupResult
            mock_provider.get_code.return_value = CodeLookupResult(
                outcome=VerificationOutcome.FOUND, code="654321"
            )

            with patch.object(
                client,
                "_check_success_signal",
                side_effect=[None, Exception("some unrelated Playwright error")],
            ), patch.object(
                client, "_verification_interstitial_detected", return_value=True
            ):
                with self.assertRaises(GreenhouseFormVerificationFailed) as cm:
                    client._confirm_success(
                        mock_page,
                        _EMPTY_BEFORE_SNAPSHOT,
                        provider=mock_provider,
                        deadline_monotonic=time.monotonic() + 300,
                    )

            self.assertEqual(cm.exception.outcome, VerificationOutcome.CODE_REJECTED)
            self.assertIsNone(cm.exception.debug_artifacts)
            self.assertEqual(len(list(Path(tmpdir).glob("*"))), 0)


class VerificationDeadlineTests(SimpleTestCase):
    @patch("apps.auto_apply.greenhouse_form.client.time.monotonic")
    def test_expiry_during_fill_prevents_submit(self, clock):
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome
        clock.return_value = 100.0
        client = GreenhouseFormClient()
        page = MagicMock()
        provider = MagicMock()
        def lookup(**kwargs):
            clock.return_value = 129.5
            return CodeLookupResult(outcome=VerificationOutcome.FOUND, code="123456")
        provider.get_code.side_effect = lookup
        field = MagicMock()
        button = MagicMock()
        page.locator.side_effect = lambda sel: MagicMock(first=button) if "button" in sel else MagicMock(count=lambda: 1, first=field)
        def fill(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 500.0)
            clock.return_value = 130.0
        field.fill.side_effect = fill
        with patch.object(client, "_check_success_signal", return_value=None), patch.object(
            client, "_verification_interstitial_detected", return_value=True
        ), self.assertRaises(GreenhouseFormVerificationFailed) as caught:
            client._confirm_success(page, _EMPTY_BEFORE_SNAPSHOT, provider=provider, deadline_monotonic=130.0)
        self.assertEqual(caught.exception.outcome, VerificationOutcome.CODE_TIMEOUT)
        self.assertIsNone(caught.exception.debug_artifacts)
        button.click.assert_not_called()

    @patch("apps.auto_apply.greenhouse_form.client.time.monotonic")
    def test_multibox_fill_recalculates_timeout_and_stops_at_deadline(self, clock):
        from apps.auto_apply.email_verification.base import VerificationOutcome
        clock.return_value = 100.0
        client = GreenhouseFormClient()
        page = MagicMock()
        single = MagicMock()
        single.count.return_value = 3
        boxes = MagicMock()
        boxes.count.return_value = 3
        fields = [MagicMock() for _ in range(3)]
        boxes.nth.side_effect = fields.__getitem__
        page.locator.side_effect = [single, boxes]
        def fill(*args, **kwargs):
            clock.return_value += 0.25
        for field in fields:
            field.press_sequentially.side_effect = fill
        with self.assertRaises(GreenhouseFormVerificationFailed) as caught:
            client._fill_verification_code(page, "123", deadline_monotonic=100.5)
        self.assertEqual(caught.exception.outcome, VerificationOutcome.CODE_TIMEOUT)
        fields[0].press_sequentially.assert_called_once_with("1", timeout=500.0)
        fields[1].press_sequentially.assert_called_once_with("2", timeout=250.0)
        fields[2].press_sequentially.assert_not_called()


class EmailVerificationProviderDeadlineTests(SimpleTestCase):
    @override_settings(AUTO_APPLY_VERIFICATION_POLL_TIMEOUT_SECONDS=45)
    def test_provider_deadline_respects_configured_timeout_and_shared_budget(self):
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome

        for shared_deadline, expected in ((500, 145), (130, 130)):
            with self.subTest(shared_deadline=shared_deadline):
                client = GreenhouseFormClient(context_factory=_TestContextHandle)
                provider = MagicMock()
                provider.get_code.return_value = CodeLookupResult(outcome=VerificationOutcome.CODE_TIMEOUT)
                with patch("apps.auto_apply.greenhouse_form.client.time.monotonic", return_value=100), patch.object(
                    client, "_check_success_signal", return_value=None
                ), patch.object(client, "_verification_interstitial_detected", return_value=True):
                    with self.assertRaises(GreenhouseFormVerificationFailed):
                        client._confirm_success(
                            MagicMock(), _EMPTY_BEFORE_SNAPSHOT, provider=provider, deadline_monotonic=shared_deadline
                        )
                self.assertEqual(provider.get_code.call_args.kwargs["deadline_monotonic"], expected)

    def test_post_code_polls_for_delayed_success(self):
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome
        from apps.auto_apply.greenhouse_form.field_mapping import SubmissionResult

        client = GreenhouseFormClient(context_factory=_TestContextHandle, confirmation_timeout_ms=1000)
        page = MagicMock()
        page.locator.return_value.first.count.return_value = 0  # no "Verify"-text button -- use fallback selector
        provider = MagicMock()
        provider.get_code.return_value = CodeLookupResult(outcome=VerificationOutcome.FOUND, code="654321")
        success = SubmissionResult(success=True)
        with patch("apps.auto_apply.greenhouse_form.client.time.monotonic", return_value=100), patch.object(
            client, "_verification_interstitial_detected", return_value=True
        ), patch.object(client, "_fill_verification_code"), patch.object(
            client, "_check_success_signal", side_effect=[None, None, None, success]
        ):
            self.assertIs(
                client._confirm_success(page, _EMPTY_BEFORE_SNAPSHOT, provider=provider, deadline_monotonic=200),
                success,
            )
        self.assertEqual(page.wait_for_timeout.call_count, 2)

    def test_post_code_polling_stops_at_shared_deadline_without_extra_artifacts(self):
        from apps.auto_apply.email_verification.base import CodeLookupResult, VerificationOutcome

        client = GreenhouseFormClient(context_factory=_TestContextHandle, confirmation_timeout_ms=1000)
        provider = MagicMock()
        provider.get_code.return_value = CodeLookupResult(outcome=VerificationOutcome.FOUND, code="654321")
        page = MagicMock()
        page.locator.return_value.first.count.return_value = 0  # no "Verify"-text button -- use fallback selector
        clock = [100.0]
        provider.get_code.side_effect = lambda **kwargs: (clock.__setitem__(0, 129.9) or provider.get_code.return_value)
        page.wait_for_timeout.side_effect = lambda milliseconds: clock.__setitem__(0, clock[0] + milliseconds / 1000)
        with patch("apps.auto_apply.greenhouse_form.client.time.monotonic", side_effect=lambda: clock[0]), patch.object(
            client, "_verification_interstitial_detected", return_value=True
        ), patch.object(client, "_fill_verification_code"), patch.object(
            client, "_check_success_signal", return_value=None
        ), patch.object(client, "_capture_debug_artifacts") as capture:
            # A code was entered and submitted but no success signal ever
            # appeared -- ambiguous, not a case known to be safely
            # retryable, so this must raise GreenhouseFormSubmissionUnconfirmed
            # (see that exception's docstring and the real production
            # failure it was fixed for, AutoApplyDraft #266), not a plain
            # GreenhouseFormVerificationFailed(outcome=CODE_REJECTED) --
            # the latter bypasses trigger_auto_apply's SUBMISSION_UNCONFIRMED
            # duplicate-application retry guard entirely.
            with self.assertRaises(GreenhouseFormSubmissionUnconfirmed) as raised:
                client._confirm_success(page, _EMPTY_BEFORE_SNAPSHOT, provider=provider, deadline_monotonic=130)
        self.assertNotIsInstance(raised.exception, GreenhouseFormVerificationFailed)
        self.assertAlmostEqual(clock[0], 130)
        # Captured once, safely, at interstitial detection -- before any
        # code was looked up or typed -- but never again on the post-code
        # path, which still must never risk writing a live OTP to disk.
        capture.assert_called_once()

    def test_large_task_deadline_does_not_extend_classification_timeout(self):
        client = GreenhouseFormClient(context_factory=_TestContextHandle, confirmation_timeout_ms=500)
        page = MagicMock()
        clock = [100.0]
        page.wait_for_timeout.side_effect = lambda milliseconds: clock.__setitem__(0, clock[0] + milliseconds / 1000)
        with patch("apps.auto_apply.greenhouse_form.client.time.monotonic", side_effect=lambda: clock[0]), patch.object(
            client, "_check_success_signal", return_value=None
        ), patch.object(client, "_verification_interstitial_detected", return_value=False):
            self.assertIsNone(client._confirm_success(page, _EMPTY_BEFORE_SNAPSHOT, deadline_monotonic=1000))
        self.assertEqual(clock[0], 100.5)

    def test_provider_configuration_error_propagates_out_of_submit(self):
        from django.core.exceptions import ImproperlyConfigured

        client = GreenhouseFormClient(context_factory=MagicMock())
        client._context_factory.return_value.new_page.return_value.url = JOB_URL
        provider = MagicMock()
        provider.get_code.side_effect = ImproperlyConfigured("Missing keys")
        with patch.object(client, "_goto_and_settle"), patch.object(
            client, "_challenge_detected", return_value=False
        ), patch.object(client, "_discover_schema"), patch.object(
            client, "_fill_answers"
        ), patch.object(client, "_click_submit"), patch.object(
            client, "_check_success_signal", return_value=None
        ), patch.object(client, "_verification_interstitial_detected", return_value=True):
            with self.assertRaises(ImproperlyConfigured):
                client.submit(JOB_URL, {}, email_code_provider=provider, deadline_monotonic=time.monotonic() + 300)


class ContextInitializationCleanupTests(SimpleTestCase):
    def test_initialization_failures_release_resources_and_preserve_error(self):
        from unittest.mock import Mock
        from apps.auto_apply.greenhouse_form.client import _default_context_factory
        for failure_stage in ("launch", "context"):
            with self.subTest(stage=failure_stage):
                driver = Mock()
                browser = driver.chromium.launch.return_value
                original = RuntimeError("initialization failed")
                if failure_stage == "launch":
                    driver.chromium.launch.side_effect = original
                else:
                    browser.new_context.side_effect = original
                    browser.close.side_effect = RuntimeError("cleanup failed")
                driver.stop.side_effect = RuntimeError("stop failed")
                with patch("playwright.sync_api.sync_playwright") as start:
                    start.return_value.start.return_value = driver
                    with self.assertRaises(RuntimeError) as raised:
                        _default_context_factory()
                self.assertIs(raised.exception, original)
                driver.stop.assert_called_once()
                if failure_stage == "context":
                    browser.close.assert_called_once()
