"""Greenhouse application-page browser-automation client.

Drives the real rendered Greenhouse application page with Playwright rather
than calling an API -- Greenhouse's public submission API requires a
per-employer opt-in key this product's ingested boards don't have (see
``docs/plans/2026-08-02-001-feat-auto-apply-greenhouse-slice-plan.md``, U3).

DB-free and dependency-injected for testability, mirroring
``apps/jobs/ingestion/greenhouse_client.py``'s shape: a class-based client
with injectable collaborators (there it's an HTTP ``session``, here it's a
Playwright context factory) and a typed exception hierarchy so callers never
see raw Playwright exceptions.

Two operations:

- ``inspect(job_url) -> FormSchema``: loads the page, enumerates rendered
  fields by role/label, returns field metadata (label, type, required-ness).
- ``submit(job_url, answers, expected_schema) -> SubmissionResult``: fills
  the same page using role/label-based locators, verifies the fill via
  ``expect()`` assertions, clicks Submit, and confirms success via a
  post-submit page signal.

Every ``inspect()``/``submit()`` call runs against a fresh, isolated
Playwright browser context (no shared cookies/storage across calls),
constructed via an injectable ``context_factory`` and torn down after use.
"""
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

from apps.auto_apply.captcha.base import CaptchaSolver, ChallengeContext

from .exceptions import (
    DebugArtifacts,
    GreenhouseFormChallenged,
    GreenhouseFormError,
    GreenhouseFormSchemaMismatch,
    GreenhouseFormSubmissionFailed,
)
from .field_mapping import (
    CHECKBOX_GROUP,
    COMBOBOX_SELECT,
    FILE,
    MULTI_SELECT,
    SINGLE_SELECT,
    TEXT,
    TEXTAREA,
    FormField,
    FormSchema,
    SubmissionResult,
    schema_matches,
)

# Hostnames a job_url is allowed to point at before this client will
# navigate to it -- defense-in-depth given the browser executes arbitrary
# JS from whatever page it loads (unlike the JSON-only ingestion client).
DEFAULT_ALLOWED_HOSTNAMES = frozenset(
    {
        "job-boards.greenhouse.io",
        "boards.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }
)

# Native HTML input[type] values this client treats as free text.
_TEXT_INPUT_TYPES = frozenset({"text", "email", "tel", "url", None, ""})

_DEFAULT_NAVIGATION_TIMEOUT_MS = 30_000
_DEFAULT_CONFIRMATION_TIMEOUT_MS = 10_000
_DEFAULT_CAPTCHA_TIMEOUT_S = 30.0
_CONFIRMATION_POLL_INTERVAL_MS = 250
_COMBOBOX_OPTION_TIMEOUT_MS = 5_000
_SETTLE_TIMEOUT_MS = 5_000

# Phrasing Greenhouse (and similar ATS confirmation views) use to announce a
# successful submission. Verified against a live Greenhouse board
# (job-boards.greenhouse.io) that an explicit ARIA role="status" element --
# this client's original, sole success signal -- never appears on the real
# page; its only live-region elements are role="log" utility spans used for
# screen-reader announcements, not a confirmation banner. These phrases are
# the fallback: specific, multi-word, and unambiguously positive, so an
# error banner (e.g. "Something went wrong, please try again") can never
# accidentally satisfy this check the way a bare "submitted"/"error"
# presence-check could.
_CONFIRMATION_TEXT_PATTERNS = (
    "thank you for applying",
    "thanks for applying",
    "application submitted",
    "application has been submitted",
    "successfully submitted",
    "we've received your application",
    "we have received your application",
    "your application was submitted",
)


# -- Pluggable CAPTCHA-solver interface --------------------------------------
#
# ChallengeContext/CaptchaSolver are the shared, vendor-agnostic contract
# defined in apps.auto_apply.captcha.base (U5) -- imported rather than
# redeclared here, since both are plain dataclass/Protocol definitions with
# no Playwright dependency, so importing them carries no coupling cost.


# -- Context-factory injection seam ------------------------------------------


class ContextHandle(Protocol):
    """What ``client.py`` needs from an isolated browser context.

    Production uses a real Playwright ``BrowserContext`` wrapped by
    ``_PlaywrightContextHandle``; tests inject a fake (or a real Playwright
    context configured with route interception against local fixtures).
    """

    def new_page(self) -> Any: ...

    def close(self) -> None: ...


class _PlaywrightContextHandle:
    """Owns a Playwright instance + browser + context for one call's lifetime."""

    def __init__(self, playwright, browser, context):
        self._playwright = playwright
        self._browser = browser
        self._context = context

    def new_page(self):
        return self._context.new_page()

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            try:
                self._browser.close()
            finally:
                self._playwright.stop()


def _default_context_factory() -> "_PlaywrightContextHandle":
    from playwright.sync_api import sync_playwright  # local import: optional dep

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(accept_downloads=False)
    return _PlaywrightContextHandle(pw, browser, context)


class GreenhouseFormClient:
    def __init__(
        self,
        *,
        context_factory: Callable[[], ContextHandle] | None = None,
        captcha_solver: CaptchaSolver | None = None,
        allowed_hostnames: frozenset[str] = DEFAULT_ALLOWED_HOSTNAMES,
        debug_artifact_dir: str | Path | None = None,
        navigation_timeout_ms: int = _DEFAULT_NAVIGATION_TIMEOUT_MS,
        confirmation_timeout_ms: int = _DEFAULT_CONFIRMATION_TIMEOUT_MS,
        captcha_timeout_s: float = _DEFAULT_CAPTCHA_TIMEOUT_S,
    ):
        self._context_factory = context_factory or _default_context_factory
        self._default_captcha_solver = captcha_solver
        self.allowed_hostnames = frozenset(allowed_hostnames)
        self.debug_artifact_dir = Path(debug_artifact_dir) if debug_artifact_dir else None
        self.navigation_timeout_ms = navigation_timeout_ms
        self.confirmation_timeout_ms = confirmation_timeout_ms
        self.captcha_timeout_s = captcha_timeout_s

    # -- public API -----------------------------------------------------

    def inspect(self, job_url: str) -> FormSchema:
        """Load ``job_url`` and return its rendered field schema.

        Raises:
            GreenhouseFormError: ``job_url`` isn't on the hostname allowlist.
            GreenhouseFormChallenged: a bot-detection challenge is present.
            GreenhouseFormSchemaMismatch: a *required* field's type isn't in
                ``field_mapping.SUPPORTED_FIELD_TYPES``.
        """
        self._validate_url(job_url)

        def _run(page):
            try:
                self._goto_and_settle(page, job_url)
                if self._challenge_detected(page):
                    raise GreenhouseFormChallenged(
                        f"Bot-detection challenge present on {job_url}; inspect() "
                        "never attempts to solve it, only submit() does."
                    )
                return self._discover_schema(page, job_url)
            except (GreenhouseFormChallenged, GreenhouseFormSchemaMismatch):
                raise
            except Exception as exc:  # noqa: BLE001 -- convert to typed error
                # A raw Playwright/browser error (navigation timeout, DNS
                # failure, page crash, etc.) here would otherwise propagate
                # past draft_for()'s GreenhouseFormError handlers straight
                # to draft_auto_apply's catch-all, which persists no
                # AutoApplyDraft row at all -- silently dropping the job
                # with no trace in the review queue. Wrapping as a typed
                # error lets drafting.py's existing handler persist an
                # EXCLUDED row instead.
                raise GreenhouseFormError(
                    f"Could not load the application form at {job_url}: {exc}"
                ) from exc

        return self._with_fresh_page(_run)

    def submit(
        self,
        job_url: str,
        answers: dict[str, Any],
        *,
        expected_schema: FormSchema | None = None,
        captcha_solver: CaptchaSolver | None = None,
    ) -> SubmissionResult:
        """Fill and submit the application page at ``job_url``.

        Args:
            answers: mapping of field label -> value to fill. For
                ``single_select``/``multi_select`` fields, value is an
                option label (or list of option labels for multi-select).
            expected_schema: the schema this submission was drafted
                against, if any. When given, the schema re-inspected on the
                live page must match it (including option sets) or
                ``GreenhouseFormSchemaMismatch`` is raised before any fill
                is attempted.
            captcha_solver: overrides the instance's default solver for
                this call only.

        Raises:
            GreenhouseFormError: ``job_url`` isn't on the hostname allowlist.
            GreenhouseFormChallenged: a challenge is present and no solver
                is configured, or the configured solver fails/times out.
            GreenhouseFormSchemaMismatch: the live schema doesn't match
                ``expected_schema``, or a required field is unsupported.
            GreenhouseFormSubmissionFailed: the form was submitted but no
                success signal was found.
        """
        self._validate_url(job_url)
        solver = captcha_solver if captcha_solver is not None else self._default_captcha_solver

        def _run(page):
            self._goto_and_settle(page, job_url)

            if self._challenge_detected(page):
                self._attempt_captcha_solve(page, job_url, solver)
                # Re-check: a "solved" challenge that didn't actually clear
                # is still a challenge -- never proceed on faith.
                if self._challenge_detected(page):
                    raise GreenhouseFormChallenged(
                        f"Challenge on {job_url} was not cleared after a solve attempt."
                    )

            schema_now = self._discover_schema(page, job_url)
            if expected_schema is not None and not schema_matches(expected_schema, schema_now):
                self._raise_with_debug_artifacts(
                    GreenhouseFormSchemaMismatch,
                    f"Rendered schema for {job_url} has drifted from the schema "
                    "this submission was drafted against.",
                    page,
                )

            try:
                self._fill_answers(page, schema_now, answers)
                self._click_submit(page)
                result = self._confirm_success(page)
            except (GreenhouseFormChallenged, GreenhouseFormSchemaMismatch):
                raise
            except Exception as exc:  # noqa: BLE001 -- convert to typed error w/ artifacts
                self._raise_with_debug_artifacts(
                    GreenhouseFormSubmissionFailed,
                    f"Submitting the form at {job_url} failed: {exc}",
                    page,
                )

            if result is None:
                self._raise_with_debug_artifacts(
                    GreenhouseFormSubmissionFailed,
                    f"No post-submit success signal found at {job_url}.",
                    page,
                )
            return result

        return self._with_fresh_page(_run)

    # -- navigation ---------------------------------------------------------

    @staticmethod
    def _goto_and_settle(page, job_url: str) -> None:
        """Navigate to `job_url` and give the page's JS framework a bounded
        window to finish hydrating before any caller starts interacting
        with it.

        `wait_until="domcontentloaded"` alone resolves once the raw HTML is
        parsed, but before frameworks like React finish hydrating. Verified
        live against a real Greenhouse board (Alpaca, a Remix/React app)
        that acting immediately -- clicking into a combobox field -- can
        land while React is still hydrating a Suspense boundary around it,
        throwing React's own internal error #426 ("Suspense boundary
        received an update before it finished hydrating") and permanently
        corrupting that widget's JS state for the rest of the page's life
        (every subsequent interaction, any text, renders zero options from
        then on).

        `wait_until="networkidle"` on the initial `goto()` fixes this but
        is unsafe as the *primary* wait condition: verified live, on the
        same real page, that it can hang for the full navigation timeout
        when some request never quiesces (an analytics beacon, a long-poll,
        etc.) -- turning a working page into a hard failure. So `goto()`
        itself still only waits for `domcontentloaded` (fast, always
        resolves), and the network-idle wait afterward is capped at its own
        short, independent timeout and treated as best-effort: if the page
        never truly goes idle, proceeding anyway is still strictly better
        than the pre-fix behavior of not waiting at all.
        """
        page.goto(job_url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=_SETTLE_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 -- best-effort settle, not a hard requirement
            pass

    # -- URL validation ---------------------------------------------------

    def _validate_url(self, job_url: str) -> None:
        from urllib.parse import urlparse

        hostname = urlparse(job_url).hostname or ""
        if hostname not in self.allowed_hostnames:
            raise GreenhouseFormError(
                f"Refusing to navigate to {job_url!r}: hostname {hostname!r} is "
                f"not in the allowed Greenhouse hostname set {sorted(self.allowed_hostnames)}."
            )

    # -- context lifecycle --------------------------------------------------

    def _with_fresh_page(self, fn: Callable[[Any], Any]) -> Any:
        handle = self._context_factory()
        try:
            page = handle.new_page()
            page.set_default_timeout(self.navigation_timeout_ms)
            return fn(page)
        finally:
            handle.close()

    # -- challenge detection --------------------------------------------------

    @staticmethod
    def _challenge_detected(page) -> bool:
        # Excludes `size=invisible` reCAPTCHA iframes: verified live against
        # a real Greenhouse board (Alpaca) that its always-present
        # background-scoring v3/Enterprise badge -- reCAPTCHA's own term
        # for "no interactive challenge shown to the user" -- renders an
        # iframe matching a plain `src*="recaptcha"` selector. Treating
        # that as a blocking challenge would report every such board as
        # challenged even though nothing is actually blocking submission;
        # only an iframe *without* that marker (a real interactive
        # checkbox/image challenge) should count.
        recaptcha_iframe = page.locator(
            'iframe[src*="recaptcha" i]:not([src*="size=invisible"]), '
            'iframe[title*="recaptcha" i]:not([src*="size=invisible"])'
        )
        recaptcha_markup = page.locator(".g-recaptcha, #recaptcha, [data-sitekey]")
        return recaptcha_iframe.count() > 0 or recaptcha_markup.count() > 0

    def _attempt_captcha_solve(self, page, job_url: str, solver: CaptchaSolver | None) -> None:
        if solver is None:
            raise GreenhouseFormChallenged(
                f"Bot-detection challenge present on {job_url} and no CaptchaSolver "
                "is configured; failing closed rather than attempting to bypass it."
            )
        challenge = ChallengeContext(url=job_url, challenge_type="recaptcha")
        try:
            solved = solver.solve(challenge, self.captcha_timeout_s)
        except Exception as exc:  # noqa: BLE001 -- solver failure == unsolved, by contract
            raise GreenhouseFormChallenged(
                f"CaptchaSolver.solve() raised for {job_url}: {exc}"
            ) from exc
        if not solved:
            raise GreenhouseFormChallenged(
                f"CaptchaSolver.solve() reported failure for {job_url}."
            )

    # -- schema discovery --------------------------------------------------

    def _discover_schema(self, page, job_url: str) -> FormSchema:
        fields: list[FormField] = []
        seen_control_ids: set[str] = set()
        labels = page.locator("form label")
        for i in range(labels.count()):
            label_el = labels.nth(i)
            label_text = self._clean_label(label_el.text_content() or "")
            if not label_text:
                continue
            control = self._resolve_control_for_label(page, label_el)
            if control is None or control.count() == 0:
                continue
            control = control.first
            if (control.get_attribute("type") or "").lower() == "checkbox":
                # Greenhouse's standard multi-select-question markup: a
                # <fieldset><legend> wrapping several individual checkboxes,
                # each with its own <label for> reading the *option* text
                # (e.g. "Python"), not the question. Handled as one group
                # per <fieldset>, not one field per checkbox.
                group_field = self._checkbox_group_field(page, control, seen_control_ids)
                if group_field is not None:
                    if group_field.required and not group_field.is_supported:
                        raise GreenhouseFormSchemaMismatch(
                            f"Required field {group_field.label!r} on {job_url} has "
                            f"unsupported type {group_field.field_type!r}."
                        )
                    fields.append(group_field)
                continue
            control_id = control.get_attribute("id") or ""
            if control_id:
                if control_id in seen_control_ids:
                    # A second <label> pointing at a control already captured
                    # (e.g. a stray duplicate `for`) -- not a new field.
                    continue
                seen_control_ids.add(control_id)
            field_type = self._classify_field_type(control)
            if field_type == FILE:
                # Greenhouse's file-upload widget labels the real <input
                # type=file> with generic, indistinguishable text (both
                # Resume/CV and Cover Letter render a visually-hidden
                # <label>Attach</label>) -- the human-meaningful name lives
                # on the surrounding group instead. Prefer it when present.
                label_text = self._group_label_for(control) or label_text
            required = self._is_required(control)
            options = self._extract_options(page, control, field_type)
            form_field = FormField(
                label=label_text,
                field_type=field_type,
                required=required,
                options=options,
                control_id=control_id,
            )
            if required and not form_field.is_supported:
                raise GreenhouseFormSchemaMismatch(
                    f"Required field {label_text!r} on {job_url} has unsupported "
                    f"type {field_type!r}."
                )
            fields.append(form_field)
        return FormSchema(fields=tuple(fields))

    @staticmethod
    def _resolve_control_for_label(page, label_el):
        """Resolve a `<label>` element's associated control.

        Deliberately *not* `page.get_by_label(text, exact=True)`: verified
        live against a real Greenhouse board that for a control carrying
        both a native `for`/`id` association *and* `aria-labelledby` (its
        react-select-style Country/Location/custom-select widgets), Playwright
        matches `get_by_label(exact=True)` against the label's raw
        textContent -- including its `aria-hidden="true"` required-asterisk
        span -- rather than the ARIA-computed accessible name (which
        correctly excludes it). Since `_clean_label()` always strips that
        asterisk, the exact-text lookup silently matched zero elements for
        every such field, and `_discover_schema()` dropped them from the
        schema entirely. Resolving via the label's own `for`/id (or a
        control nested directly inside it) sidesteps that text-matching
        quirk altogether.
        """
        for_id = label_el.get_attribute("for")
        if for_id:
            control = page.locator(f'[id="{for_id}"]')
            return control if control.count() > 0 else None
        nested = label_el.locator("input, select, textarea")
        return nested if nested.count() > 0 else None

    @staticmethod
    def _group_label_for(control):
        """For a FILE control wrapped in a `role="group"` with its own
        `aria-labelledby` (Greenhouse's upload widget), return that group's
        label text -- the descriptive "Resume/CV"/"Cover Letter" name,
        rather than the generic "Attach" text on the control's own
        (visually-hidden) `<label>`. Returns `None` if no such group/label
        is found, leaving the caller's original label text untouched.
        """
        group = control.locator("xpath=ancestor::*[@role='group'][@aria-labelledby][1]")
        if group.count() == 0:
            return None
        labelledby_id = group.first.get_attribute("aria-labelledby")
        if not labelledby_id:
            return None
        label_el = control.page.locator(f'[id="{labelledby_id}"]')
        if label_el.count() == 0:
            return None
        text = GreenhouseFormClient._clean_label(label_el.first.text_content() or "")
        return text or None

    @staticmethod
    def _checkbox_group_field(page, control, seen_control_ids: set[str]) -> "FormField | None":
        """Build one `FormField(field_type=CHECKBOX_GROUP)` for the whole
        `<fieldset>` a checkbox belongs to, rather than treating each
        checkbox as its own field. Returns `None` for a checkbox with no
        enclosing `<fieldset>` (unrecognized markup) or one already
        captured via an earlier checkbox in the same group.
        """
        fieldset = control.locator("xpath=ancestor::fieldset[1]")
        if fieldset.count() == 0:
            return None
        fieldset = fieldset.first
        fieldset_id = fieldset.get_attribute("id")
        if not fieldset_id:
            fieldset_id = f"gh-checkbox-group-{uuid.uuid4().hex[:8]}"
            fieldset.evaluate("(el, id) => { el.id = id; }", fieldset_id)
        if fieldset_id in seen_control_ids:
            return None
        seen_control_ids.add(fieldset_id)

        legend = fieldset.locator("legend")
        label_text = (
            GreenhouseFormClient._clean_label(legend.first.text_content() or "")
            if legend.count() > 0
            else ""
        )
        if not label_text:
            return None

        checkboxes = fieldset.locator('input[type="checkbox"]')
        options: list[str] = []
        required = False
        for i in range(checkboxes.count()):
            checkbox = checkboxes.nth(i)
            checkbox_id = checkbox.get_attribute("id") or ""
            opt_label = ""
            if checkbox_id:
                opt_label_el = page.locator(f'label[for="{checkbox_id}"]')
                if opt_label_el.count() > 0:
                    opt_label = GreenhouseFormClient._clean_label(
                        opt_label_el.first.text_content() or ""
                    )
            if opt_label:
                options.append(opt_label)
            if GreenhouseFormClient._is_required(checkbox):
                required = True

        return FormField(
            label=label_text,
            field_type=CHECKBOX_GROUP,
            required=required,
            options=tuple(options),
            control_id=fieldset_id,
        )

    @staticmethod
    def _clean_label(text: str) -> str:
        return text.strip().rstrip("*").strip()

    @staticmethod
    def _classify_field_type(control) -> str:
        tag_name = control.evaluate("el => el.tagName").upper()
        if tag_name == "TEXTAREA":
            return TEXTAREA
        if tag_name == "SELECT":
            is_multiple = control.evaluate("el => el.multiple")
            return MULTI_SELECT if is_multiple else SINGLE_SELECT
        if tag_name == "INPUT":
            input_type = control.get_attribute("type")
            if input_type == "file":
                return FILE
            if (control.get_attribute("role") or "").lower() == "combobox":
                return COMBOBOX_SELECT
            if input_type in _TEXT_INPUT_TYPES:
                return TEXT
            return input_type or "unknown"
        return "unknown"

    @staticmethod
    def _is_required(control) -> bool:
        aria_required = control.get_attribute("aria-required")
        if aria_required and aria_required.lower() == "true":
            return True
        return control.get_attribute("required") is not None

    @staticmethod
    def _extract_options(page, control, field_type: str) -> tuple[str, ...]:
        if field_type in (SINGLE_SELECT, MULTI_SELECT):
            raw_options = control.locator("option").all_text_contents()
            return tuple(opt.strip() for opt in raw_options if opt.strip())
        if field_type == COMBOBOX_SELECT:
            # Best-effort only: a click-driven listbox (react-select and
            # similar) may show a default option set on open (small fixed
            # lists like Yes/No), a huge static list (Country), or nothing
            # at all until the user types (Location's geocoding search).
            # An empty result here isn't an error -- fill-time
            # `_fill_combobox()` re-derives options by typing the actual
            # answer, which works regardless of what (if anything) shows on
            # a bare open.
            try:
                control.click()
                listbox = page.get_by_role("listbox")
                listbox.first.wait_for(state="visible", timeout=1_000)
                raw_options = listbox.first.get_by_role("option").all_text_contents()
            except Exception:  # noqa: BLE001 -- no listbox on bare open is normal, not fatal
                raw_options = []
            finally:
                page.keyboard.press("Escape")
            return tuple(opt.strip() for opt in raw_options if opt.strip())
        return ()

    # -- filling --------------------------------------------------------

    def _fill_answers(self, page, schema: FormSchema, answers: dict[str, Any]) -> None:
        from playwright.sync_api import expect

        schema_by_label = schema.by_label()
        for label, value in answers.items():
            form_field = schema_by_label.get(label)
            if form_field is None:
                # An answer for a field the live page no longer renders is
                # exactly the drift schema_matches() should have already
                # caught when expected_schema was supplied; when it wasn't,
                # skip rather than guess at a locator for a field we never
                # confirmed exists.
                continue

            control = self._locate_control(page, form_field, label)

            if form_field.field_type in (TEXT, TEXTAREA):
                control.fill(str(value))
                expect(control).to_have_value(str(value))
            elif form_field.field_type == SINGLE_SELECT:
                control.select_option(label=str(value))
                expect(control).to_have_value(control.evaluate("el => el.value"))
            elif form_field.field_type == MULTI_SELECT:
                values = value if isinstance(value, (list, tuple)) else [value]
                control.select_option(label=[str(v) for v in values])
                selected = control.evaluate(
                    "el => Array.from(el.selectedOptions).map(o => o.value)"
                )
                if len(selected) != len(values):
                    raise GreenhouseFormSubmissionFailed(
                        f"Not all selections registered for multi-select field {label!r}."
                    )
            elif form_field.field_type == FILE:
                control.set_input_files(str(self._validated_file_path(value, label)))
            elif form_field.field_type == COMBOBOX_SELECT:
                self._fill_combobox(page, control, str(value), label)
            elif form_field.field_type == CHECKBOX_GROUP:
                self._fill_checkbox_group(page, control, value, label)
            else:
                raise GreenhouseFormSchemaMismatch(
                    f"No fill strategy for field {label!r} of type {form_field.field_type!r}."
                )

    @staticmethod
    def _locate_control(page, form_field: FormField, label: str):
        """Relocate the control a schema field was discovered from.

        Prefers the `control_id` captured at discovery time (see
        `_resolve_control_for_label()` for why label-text lookup alone is
        unreliable); falls back to `get_by_label()` only for schemas
        persisted before `control_id` existed.
        """
        if form_field.control_id:
            control = page.locator(f'[id="{form_field.control_id}"]')
            if control.count() > 0:
                return control.first
        return page.get_by_label(label, exact=True).first

    def _fill_combobox(self, page, control, value: str, label: str) -> None:
        """Fill a JS-driven combobox widget (react-select and similar):
        click to open it, type the answer to filter its listbox, then click
        the resulting matching option. Verified live against a real
        Greenhouse board (Country/Location/custom-select questions) that
        typing the target value reliably filters the listbox to a matching
        option, and that clicking it -- not `.fill()` alone -- is what
        actually registers the selection (the control's own `.value` stays
        empty afterward; the widget tracks the choice elsewhere).
        """
        control.click()
        control.fill(value)
        options = page.get_by_role("option", name=value, exact=False)
        try:
            options.first.wait_for(state="visible", timeout=_COMBOBOX_OPTION_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 -- convert to typed submission failure
            raise GreenhouseFormSubmissionFailed(
                f"No matching option for {value!r} found in combobox field {label!r}."
            ) from exc
        self._best_option_match(options, value).click()

    @staticmethod
    def _best_option_match(options, value: str):
        """Among every option whose text substring-matches `value`, prefer
        one whose text *starts with* it.

        Verified live against a real Greenhouse board (Alpaca): typing
        "India" into the phone country-code combobox matches both "India
        +91" and "British Indian Ocean Territory +246" -- both contain
        "india" as a substring -- and picking `options.first` (DOM order)
        landed on the wrong one. Only "India +91" starts with the typed
        value, so that heuristic disambiguates correctly; falls back to the
        first match when no option starts with it (e.g. mid-word answers).
        """
        count = options.count()
        if count <= 1:
            return options.first
        needle = value.strip().lower()
        for i in range(count):
            candidate = options.nth(i)
            text = (candidate.text_content() or "").strip().lower()
            if text.startswith(needle):
                return candidate
        return options.first

    @staticmethod
    def _fill_checkbox_group(page, fieldset, value: Any, label: str) -> None:
        """Check the boxes within `fieldset` whose own option label matches
        an entry in `value` (a single answer or list of answers), mirroring
        `_checkbox_group_field()`'s discovery-time option extraction.

        Also strips `required`/`aria-required` from every checkbox we're
        *not* checking. Verified live against a real Greenhouse board
        (Blacksky) that it marks every individual checkbox in a "select all
        that apply" group `required` -- unlike radio buttons, HTML gives no
        way to express "at least one of this group" on checkboxes, so taken
        at face value the browser's native constraint validation would
        block Submit entirely unless literally every option is checked,
        defeating the question's own "select all that apply" semantics.
        We enforce the real intended constraint ourselves (every *requested*
        option got checked, via the count comparison below) before ever
        touching the DOM's native validation.
        """
        values = {str(v) for v in (value if isinstance(value, (list, tuple)) else [value])}
        checkboxes = fieldset.locator('input[type="checkbox"]')
        checked_count = 0
        for i in range(checkboxes.count()):
            checkbox = checkboxes.nth(i)
            checkbox_id = checkbox.get_attribute("id") or ""
            opt_label = ""
            if checkbox_id:
                opt_label_el = page.locator(f'label[for="{checkbox_id}"]')
                if opt_label_el.count() > 0:
                    opt_label = GreenhouseFormClient._clean_label(
                        opt_label_el.first.text_content() or ""
                    )
            if opt_label in values:
                checkbox.check()
                checked_count += 1
            else:
                checkbox.evaluate(
                    "el => { el.removeAttribute('required'); "
                    "el.removeAttribute('aria-required'); }"
                )
        if checked_count != len(values):
            raise GreenhouseFormSubmissionFailed(
                f"Not all selections registered for checkbox-group field {label!r}."
            )

    @staticmethod
    def _validated_file_path(value: Any, label: str) -> Path:
        """Resolve a FILE-field answer to a path Playwright may upload.

        Answers for FILE fields are meant to originate only from
        ``drafting.py`` (a Profile's own ``resume.path``), never from user
        input -- ``edit_auto_apply_draft`` already refuses to let a user
        overwrite one (see apps/web/views.py). This is the last line of
        defense: without it, any bug or future code path that let a
        non-existent or non-file value (e.g. a directory, or a typo'd path)
        reach here would fail deep inside Playwright with an opaque error
        instead of a typed, diagnosable one.
        """
        candidate = Path(str(value)).resolve()
        if not candidate.is_file():
            raise GreenhouseFormError(
                f"File {value!r} for field {label!r} does not exist."
            )
        return candidate

    def _click_submit(self, page) -> None:
        submit_button = page.get_by_role("button", name="Submit", exact=False)
        if submit_button.count() == 0:
            submit_button = page.locator('button[type="submit"], input[type="submit"]')
        submit_button.first.click()

    def _confirm_success(self, page) -> SubmissionResult | None:
        """Poll for a post-submit success signal until ``confirmation_timeout_ms``
        elapses, checking every signal in ``_check_success_signal`` each pass
        rather than waiting out a full timeout on one selector before trying
        the next -- keeps the worst-case (genuine failure) wait bounded by a
        single timeout budget instead of one per signal.
        """
        deadline = time.monotonic() + (self.confirmation_timeout_ms / 1000)
        while True:
            result = self._check_success_signal(page)
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                return None
            page.wait_for_timeout(_CONFIRMATION_POLL_INTERVAL_MS)

    def _check_success_signal(self, page) -> SubmissionResult | None:
        # Signal 1: an explicit ARIA status live region, if the board
        # renders one -- cheap and unambiguous when present.
        status = page.get_by_role("status")
        if status.count() > 0:
            text = status.first.inner_text().strip()
            if text:
                return SubmissionResult(success=True, confirmation_text=text)

        # Signal 2: known confirmation phrasing anywhere in the rendered
        # page text. Only specific, positive multi-word phrases are
        # matched -- never element presence/absence alone -- so this can't
        # be tricked by an error banner that happens to render after
        # submit (see _CONFIRMATION_TEXT_PATTERNS).
        body_text = page.locator("body").inner_text().lower()
        for phrase in _CONFIRMATION_TEXT_PATTERNS:
            if phrase in body_text:
                return SubmissionResult(success=True, confirmation_text=phrase)

        return None

    # -- failure debugging --------------------------------------------------

    def _raise_with_debug_artifacts(self, exc_cls, message: str, page) -> None:
        raise exc_cls(message, debug_artifacts=self._capture_debug_artifacts(page))

    def _capture_debug_artifacts(self, page) -> DebugArtifacts | None:
        if self.debug_artifact_dir is None:
            return None
        try:
            self.debug_artifact_dir.mkdir(parents=True, exist_ok=True)
            run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}"
            screenshot_path = self.debug_artifact_dir / f"{run_id}.png"
            tree_path = self.debug_artifact_dir / f"{run_id}-a11y.yaml"
            page.screenshot(path=str(screenshot_path), full_page=True)
            # Playwright removed the legacy `page.accessibility` API; the
            # ARIA-snapshot locator method is the current equivalent (a
            # YAML-formatted serialization of the accessibility tree).
            tree_path.write_text(page.locator("body").aria_snapshot())
            return DebugArtifacts(
                screenshot_path=str(screenshot_path), accessibility_tree_path=str(tree_path)
            )
        except Exception:  # noqa: BLE001 -- debug capture must never mask the real failure
            return None
