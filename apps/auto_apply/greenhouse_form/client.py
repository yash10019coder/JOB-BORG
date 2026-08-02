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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from .exceptions import (
    DebugArtifacts,
    GreenhouseFormChallenged,
    GreenhouseFormError,
    GreenhouseFormSchemaMismatch,
    GreenhouseFormSubmissionFailed,
)
from .field_mapping import (
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


# -- Pluggable CAPTCHA-solver interface --------------------------------------
#
# The real CaptchaSolver implementation ships separately (see U5); this is
# only the minimal shape this client needs to hand a detected challenge off
# to *something*, expressed as a Protocol so any structurally-compatible
# solver (including U5's) can be injected without this module importing it.


@dataclass
class ChallengeContext:
    """Minimal, duck-typed description of a detected bot-detection challenge."""

    url: str
    challenge_type: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CaptchaSolver(Protocol):
    """Protocol a pluggable CAPTCHA-solving provider must satisfy.

    Implementations that raise or exceed ``timeout`` are, by contract,
    treated identically to returning ``False`` -- solving failed, and this
    client fails closed (``GreenhouseFormChallenged``) either way.
    """

    def solve(self, challenge: ChallengeContext, timeout: float) -> bool: ...


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
            page.goto(job_url, wait_until="domcontentloaded")
            if self._challenge_detected(page):
                raise GreenhouseFormChallenged(
                    f"Bot-detection challenge present on {job_url}; inspect() "
                    "never attempts to solve it, only submit() does."
                )
            return self._discover_schema(page, job_url)

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
            page.goto(job_url, wait_until="domcontentloaded")

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
        recaptcha_iframe = page.locator(
            'iframe[src*="recaptcha" i], iframe[title*="recaptcha" i]'
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
        label_texts = [self._clean_label(t) for t in page.locator("form label").all_text_contents()]
        for label_text in label_texts:
            if not label_text:
                continue
            control = page.get_by_label(label_text, exact=True)
            if control.count() == 0:
                continue
            control = control.first
            field_type = self._classify_field_type(control)
            required = self._is_required(control)
            options = self._extract_options(control, field_type)
            form_field = FormField(
                label=label_text, field_type=field_type, required=required, options=options
            )
            if required and not form_field.is_supported:
                raise GreenhouseFormSchemaMismatch(
                    f"Required field {label_text!r} on {job_url} has unsupported "
                    f"type {field_type!r}."
                )
            fields.append(form_field)
        return FormSchema(fields=tuple(fields))

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
    def _extract_options(control, field_type: str) -> tuple[str, ...]:
        if field_type not in (SINGLE_SELECT, MULTI_SELECT):
            return ()
        raw_options = control.locator("option").all_text_contents()
        return tuple(opt.strip() for opt in raw_options if opt.strip())

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

            control = page.get_by_label(label, exact=True).first

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
                control.set_input_files(str(value))
            else:
                raise GreenhouseFormSchemaMismatch(
                    f"No fill strategy for field {label!r} of type {form_field.field_type!r}."
                )

    def _click_submit(self, page) -> None:
        submit_button = page.get_by_role("button", name="Submit", exact=False)
        if submit_button.count() == 0:
            submit_button = page.locator('button[type="submit"], input[type="submit"]')
        submit_button.first.click()

    def _confirm_success(self, page) -> SubmissionResult | None:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        confirmation = page.get_by_role("status")
        try:
            confirmation.first.wait_for(timeout=self.confirmation_timeout_ms)
        except PlaywrightTimeoutError:
            return None
        text = confirmation.first.inner_text()
        return SubmissionResult(success=True, confirmation_text=text)

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
