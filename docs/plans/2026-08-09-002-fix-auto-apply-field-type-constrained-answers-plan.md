---
title: "fix: Constrain auto-apply answers to each form field's real input type"
status: completed
created: 2026-08-09
type: fix
---

# fix: Constrain auto-apply answers to each form field's real input type

## Problem Frame

Auto-apply's LLM answer-inference pipeline treats every Greenhouse question as free text, regardless of the field's actual input type. `Question` (`apps/auto_apply/llm/base.py:29-42`) and the prompt built from it (`apps/auto_apply/llm/langchain_client.py:61-85`) carry only a label string — never the field's `field_type` or, for option-bearing fields (`SINGLE_SELECT`, `MULTI_SELECT`, `CHECKBOX_GROUP`), its real `options` tuple (`apps/auto_apply/greenhouse_form/field_mapping.py:41-69`). The LLM is free to answer a dropdown question ("What college did you attend?") with the user's actual college name even when that string isn't one of the form's listed options.

The fill layer (`apps/auto_apply/greenhouse_form/client.py:_fill_answers`, lines 762-803) only partially copes with this at submit time:
- `COMBOBOX_SELECT` and `CHECKBOX_GROUP` already fail closed with a typed `GreenhouseFormSubmissionFailed` and a clear message when the answer doesn't match any real option (lines 820-839, 866-907) — a good existing pattern.
- `SINGLE_SELECT` has **no such handling** — a bad label value bubbles up as a raw Playwright `TimeoutError` from `select_option(label=...)` (line 781-783), caught only by `submit()`'s generic `except Exception` (lines 342-347).
- None of this happens until **send time**. A dropdown mismatch is invisible at draft time — the human review queue (`apps/web/views.py` `auto_apply_queue`/`edit_auto_apply_draft`) shows the LLM's free-text guess as if it were a normal, fillable answer, with no signal that it may not exist as a real option. The user only discovers the problem when the send fails minutes later with a generic `SUBMISSION_FAILED`.

This plan builds on the already-shipped human-review-queue fix (`docs/plans/2026-08-09-001-fix-auto-apply-unanswerable-questions-human-review-plan.md`, origin: that plan) — this is the same design lineage: rather than blindly submitting an answer that will fail (or, worse, silently degrading the applicant's real answer), an option-constrained field whose true value has no matching option should surface as a **needs-review placeholder** the human can resolve by picking a valid option, mirroring how an LLM-unanswerable question already surfaces.

## Requirements

- **R1.** `Question`/the LLM prompt carry the field's `field_type` and (for option-bearing types) its exact `options` list, so the LLM is instructed to answer only with a listed option — never invented text — for `SINGLE_SELECT`, `MULTI_SELECT`, and `CHECKBOX_GROUP` fields.
- **R2.** When the field is option-bearing and the user's real, best-available answer doesn't correspond to any listed option, the LLM (or a deterministic fallback layer) selects the best generic fallback already present in the option list (e.g. an "Other"/"Not listed"/"None of the above"-shaped option) instead of fabricating a non-existent value.
- **R3.** If no such fallback option exists in the field's option list and no listed option is a good match, the field is treated the same as an LLM-unanswerable custom question (R7 of the origin plan): a blank, `needs_review` placeholder in `answers_payload`, never a fabricated or blindly-submitted value, and never a whole-draft exclusion for this reason alone.
- **R4.** At draft time (not just submit time), an answer for an option-bearing field is validated against the field's real `options` set. A mismatch is caught and surfaced as `needs_review` in the review queue immediately — the same visible signal a human already gets for an LLM-unanswerable question — rather than discovered for the first time as a submission failure.
- **R5.** `SINGLE_SELECT` fill gets the same graceful, typed-exception handling `COMBOBOX_SELECT`/`CHECKBOX_GROUP`/`MULTI_SELECT` already have: a `select_option` call with an unmatched label must raise a typed `GreenhouseFormSubmissionFailed` with a clear message, not propagate a raw Playwright `TimeoutError`.
- **R6.** The human review queue UI (`templates/web/auto_apply_queue.html`) presents an option-constrained needs-review field as an actual `<select>`/choice control populated from `form_field.options` (plus, when applicable, the ability to leave the fallback as-is), not a freeform text box — so a human fixing the flagged answer can't re-introduce an invalid value.
- **R7.** Existing field types with no options (`TEXT`, `TEXTAREA`, `FILE`) are unaffected — this plan does not change how free-text or file-upload questions are answered or reviewed.
- **R8.** Existing regression coverage for field discovery and filling (per `docs/plans/2026-08-06-002-fix-auto-apply-field-discovery-and-historical-failures-plan.md` R5) continues to pass — label-driven text/select/checkbox/file fixtures keep discovering and filling identically for the happy path where the LLM's answer already matches a real option.

## Scope Boundaries

**In scope:** `SINGLE_SELECT`, `MULTI_SELECT`, `CHECKBOX_GROUP` answer-time option constraint and validation; `SINGLE_SELECT` fill-time graceful failure; review-queue UI for option-constrained fields.

**Out of scope:**
- `COMBOBOX_SELECT` fields (e.g. Location) are not option-constrained the same way — their `options` are often incomplete/unreliable (best-effort geocode-search results, per `_extract_options` in `client.py:735-758`), and they already have graceful submit-time failure handling. Left untouched.
- The duplicate-label collision gap noted in `docs/plans/2026-08-02-001-feat-auto-apply-greenhouse-slice-plan.md:72` (two identically-labeled questions collapsing into one answer) — a separate, already-known, already-deferred problem, not introduced or worsened by this fix.
- Any change to `FILE`-type handling — the origin plan's required-FILE-question exclusion behavior stays as-is.
- Any new LLM provider/prompt-engineering work beyond passing field constraints into the existing prompt (no model swap, no few-shot example additions beyond what's needed for the options instruction).

### Deferred to Follow-Up Work

- Fuzzy/semantic "closest option" matching beyond an explicit fallback-option heuristic (e.g. Levenshtein distance against option labels) — starting with an explicit-fallback-option-first approach (R2/R3) is simpler and less error-prone; smarter matching can be added later if the fallback-option approach proves insufficient in practice.
- Extending field-type option validation into the `edit_auto_apply_draft` POST handler as a hard server-side rejection (currently only the UI, R6, constrains input) — deferred since a determined API caller bypassing the UI is a pre-existing gap for this whole review-queue feature, not unique to option-bearing fields.

## Key Technical Decisions

### D1. Thread `field_type` + `options` into `Question`, not a parallel data structure

`Question` (`llm/base.py:29-42`) gains two new fields: `field_type: str = ""` and `options: tuple[str, ...] = ()`. `drafting.draft_for()` builds `Question(id=f.label, text=f.label, field_type=f.field_type, options=f.options)` instead of dropping the metadata (drafting.py:185). This is the minimal change that gets the constraint to the LLM — no new file, no parallel lookup structure to keep in sync with `FormField`.

### D2. Prompt instructs the LLM to answer option-bearing questions with an exact option string or a designated fallback

`LangChainAnswerInferenceClient._build_prompt()` (`langchain_client.py:61-85`) renders, for any question with non-empty `options`, the exact option list inline (e.g. `<question id="..." options="Option A|Option B|Other">...`) plus a system-prompt instruction: for a question with `options`, the answer must be exactly one of the listed strings (verbatim, case-sensitive match) or, if the true answer isn't listed, an explicit fallback option among those listed (a generic catch-all like "Other"/"Not listed"/"Prefer not to answer" — the LLM identifies which listed option is a fallback-shaped one, not a fixed keyword match, since employer-defined fallback labels vary). If no listed option is a reasonable answer at all (true value unclear AND no generic fallback present), the LLM should return an empty/uncertain answer exactly as it already does for other unanswerable questions today — the existing `insufficient_evidence`/empty-answer path in `resolve_answers()` becomes the natural R3 mechanism, no new LLM output field required.

### D3. Deterministic post-LLM validation is the actual enforcement point, not LLM instruction-following alone

LLM instruction-following on option constraints is a strong prior, not a guarantee. `answer_resolution.resolve_field_answers()` (or a thin wrapper around it) validates: for any resolved answer where the originating question had `options`, if `answer not in options`, treat the answer as if it were empty (route to the same "unanswerable" placeholder path as any other unanswerable question) rather than trusting the LLM's raw string through to `answers_payload`. This is the R4 mechanism — draft-time validation lives here, not scattered into `drafting.py`. `drafting.py`'s existing "falsy answer" branch (`drafting.py:191-224`) already produces the correct `needs_review` placeholder shape for this case with zero changes to that branch — the validation just needs to make an invalid-option answer arrive there as falsy.

### D4. `SINGLE_SELECT` fill gets the `_fill_combobox`/`_fill_checkbox_group` treatment, not a shared abstraction

Wrap `SINGLE_SELECT`'s `select_option(label=str(value))` call (`client.py:781-783`) in a try/except translating Playwright's raw exception into `GreenhouseFormSubmissionFailed(f"No matching option for {value!r} found in select field {label!r}")`, mirroring the existing message shape used by `_fill_combobox` (client.py:820-839). Given D3 already prevents an invalid option from reaching `answers_payload` in the normal flow, this is now primarily a defense-in-depth guard for stale/drifted schemas (the field's real options changed between draft and send — already partially caught by `schema_matches()`, but this is the last-resort fill-time backstop) — not introducing a new shared helper for a three-line change is the right amount of abstraction here.

### D5. Review-queue UI renders option-bearing needs-review fields as a `<select>`, populated from the stored `options`

`answers_payload` entries already carry `field_type` (drafting.py:173-180, 217-233); this plan adds `options` to that same dict for option-bearing fields (sourced from `form_field.options`, already available at draft time). `templates/web/auto_apply_queue.html`'s answer-editing form (currently a single `<input type="text">` for every field, line 50) branches: when `entry.options` is non-empty, render a `<select>` populated with those options (plus the current value pre-selected, if it happens to already be valid) instead of a text input. This directly satisfies R6 and reuses the same `label__<i>`/`value__<i>` POST shape `edit_auto_apply_draft` already parses — no view-layer parsing changes needed for the UI half of this plan.

## Files

- `apps/auto_apply/llm/base.py` — `Question` dataclass gains `field_type`, `options`
- `apps/auto_apply/llm/langchain_client.py` — prompt building + system prompt instruction for option-constrained questions
- `apps/auto_apply/services/drafting.py` — pass `field_type`/`options` into `Question` construction; add `options` to `answers_payload` entries for option-bearing fields
- `apps/auto_apply/services/answer_resolution.py` — post-LLM validation: invalid-option answer treated as unanswerable
- `apps/auto_apply/greenhouse_form/client.py` — `SINGLE_SELECT` fill gets typed-exception handling
- `apps/web/views.py` — no server-side parsing change expected (see D5), but confirm `edit_auto_apply_draft` tolerates an `options`-carrying answer entry unchanged
- `templates/web/auto_apply_queue.html` — render `<select>` for option-bearing needs-review fields
- `apps/auto_apply/tests/test_drafting_service.py`, `apps/auto_apply/tests/test_answer_resolution.py` (or wherever LLM-pipeline tests for `resolve_field_answers` live), `apps/auto_apply/tests/test_greenhouse_form_client.py`, `apps/web/tests/test_auto_apply_views.py` — test updates per unit below

---

## Implementation Units

### U1. Carry field constraints from `FormField` into `Question`

**Requirements:** R1 | **Dependencies:** none
**Files:** `apps/auto_apply/llm/base.py`, `apps/auto_apply/services/drafting.py`, `apps/auto_apply/tests/test_drafting_service.py`
**Approach:** Add `field_type: str = ""` and `options: tuple[str, ...] = ()` to `Question` (base.py:29-42), defaulted so every other `Question` construction site in the codebase keeps working unchanged. In `drafting.draft_for()`, build custom-field `Question`s as `Question(id=f.label, text=f.label, field_type=f.field_type, options=f.options)` (replacing drafting.py:185).
**Patterns to follow:** Existing `Question` dataclass shape (base.py).
**Test scenarios:**
- A `SINGLE_SELECT` custom field produces a `Question` whose `options` tuple matches the `FormField.options` exactly.
- A `TEXT` custom field produces a `Question` with `options=()` (unaffected, R7).
- Existing `draft_for()` tests continue to pass unmodified in shape (no regression to the happy-path answer flow).
**Verification:** `Question` round-trips `field_type`/`options` for every custom field discovered from a `SINGLE_SELECT`/`MULTI_SELECT`/`CHECKBOX_GROUP` fixture.

### U2. Instruct the LLM to answer within the option set

**Requirements:** R1, R2 | **Dependencies:** U1
**Files:** `apps/auto_apply/llm/langchain_client.py`, test file for the LangChain client (locate existing suite, e.g. `apps/auto_apply/tests/test_langchain_client.py` or equivalent)
**Approach:** Extend `_build_prompt()` (langchain_client.py:61-85) to render each question's `options` when non-empty (e.g. as an attribute or nested list in the per-question XML/prompt block already used). Extend `_SYSTEM_PROMPT` (lines 26-46) with an instruction: for a question presented with options, answer with one of the exact listed strings; if the true answer isn't listed, prefer a listed fallback-shaped option (e.g. "Other", "Not applicable", "Prefer not to say" — described generically, not as a fixed keyword the model pattern-matches on) over inventing a new string; if genuinely uncertain and no reasonable listed option applies, follow the existing low-confidence/insufficient-evidence behavior (empty answer).
**Patterns to follow:** Existing `_SYSTEM_PROMPT` structure and `_build_prompt()`'s per-question rendering.
**Test scenarios:**
- Building a prompt for a question with `options=("Yes", "No")` includes both option strings in the rendered prompt.
- Building a prompt for a question with `options=()` renders identically to before this change (no regression for free-text questions).
- (Mocked-LLM integration scenario) A fake LLM response returning an exact option string is accepted through unchanged by the existing response-parsing path.
**Verification:** Prompt snapshot/substring assertions confirm options appear only for option-bearing questions; existing non-option prompt tests remain green.

### U3. Validate LLM answers against the real option set before they reach the draft

**Requirements:** R3, R4 | **Dependencies:** U1, U2
**Files:** `apps/auto_apply/services/answer_resolution.py`, its test file
**Approach:** In `resolve_field_answers()` (answer_resolution.py:50-104), after `llm.base.resolve_answers()` returns, for every `ResolvedAnswer` whose originating `Question.options` is non-empty: if `resolved_answer.answer` is truthy and not an exact (case-sensitive) member of `question.options`, replace it with an empty answer (mirroring the shape `drafting.py` already treats as "unanswerable" — same `reason`/`category` fields preserved so the existing needs-review UX in drafting.py's falsy-answer branch fires unchanged). This is the deterministic backstop for D3 — LLM instruction-following (U2) is the first line, this is the enforced one.
**Execution note:** Write the invalid-option-answer test first (an LLM stub returning a string not in `options`) before wiring the fix, since this is the actual security/correctness boundary of the whole plan (D3) — a regression here silently reintroduces the original bug.
**Patterns to follow:** `ExplicitAnswer`-override handling already present in this function for the general shape of "adjust a resolved answer based on a side-channel signal before returning it."
**Test scenarios:**
- LLM returns an exact option string (`"Option A"` when `options=("Option A", "Option B")`) → answer passes through unchanged.
- LLM returns a string not in `options` (e.g. the user's real college name when `options=("State U", "Other")`) → resolved answer becomes empty/unanswerable-shaped, not the invented string.
- LLM returns an empty string for a question with `options` (already-unanswerable case) → unaffected, still empty.
- A question with `options=()` (free text) → validation is a no-op regardless of what the LLM returns (R7).
- Case-sensitivity: an LLM answer that differs from a real option only in case (e.g. `"yes"` vs `"Yes"`) is treated as a mismatch (exact match only) — documents the deliberate strictness choice rather than silently guessing intent.
**Verification:** A drafted `AutoApplyDraft` for a `SINGLE_SELECT` question with a mismatched LLM answer lands in `answers_payload` as a blank `needs_review` placeholder with `options` present, not the mismatched text.

### U4. Carry `options` onto `answers_payload` entries and render a constrained control in the review queue

**Requirements:** R4, R6 | **Dependencies:** U3
**Files:** `apps/auto_apply/services/drafting.py`, `templates/web/auto_apply_queue.html`, `apps/web/tests/test_auto_apply_views.py`
**Approach:** In `drafting.py`'s custom-fields loop (drafting.py:191-233), add `"options": form_field.options` to both the blank-placeholder and normal-answer payload branches (empty tuple for non-option fields, so the template can key off truthiness safely). In `auto_apply_queue.html`'s per-field editing block (around line 48-53), branch: when `entry.options` is non-empty, render a `<select name="value__{{ forloop.counter0 }}">` with an `<option>` per value (selecting the current `entry.value` if it happens to match one) instead of `<input type="text">`; keep the existing text input for fields with no options.
**Patterns to follow:** The existing `label in draft.blocking_fields` / `entry.needs_review` conditional structure already in the template (lines 42-46) for the branching style to mirror.
**Test scenarios:**
- Covers (origin plan) R6 parity: a `SINGLE_SELECT` needs-review placeholder renders a `<select>` with all real options present in the response HTML, not a text `<input>`.
- A `TEXT` needs-review placeholder still renders as a text `<input>` (no regression, R7).
- Submitting the edit form with a `<select>`-chosen value updates `draft.answers[label].value` via the existing `edit_auto_apply_draft` POST handling, unchanged.
- A `SINGLE_SELECT` answer that already matches an option (no review needed) still renders as plain confirmed text per the existing non-`drafted`-status branch (line 51-53) — options-rendering only applies to the editable/`drafted` path.
**Verification:** Full page render for a draft with a mismatched-dropdown placeholder shows a populated `<select>`; saving a valid selection clears the placeholder state on next view.

### U5. `SINGLE_SELECT` fill fails closed with a typed exception

**Requirements:** R5 | **Dependencies:** none (parallelizable with U1-U4)
**Files:** `apps/auto_apply/greenhouse_form/client.py`, `apps/auto_apply/tests/test_greenhouse_form_client.py`
**Approach:** Wrap the `SINGLE_SELECT` branch of `_fill_answers()` (client.py:781-783) in a try/except around `control.select_option(label=str(value))`, catching Playwright's timeout/error and re-raising `GreenhouseFormSubmissionFailed(f"No matching option for {value!r} found in select field {label!r}")` — same message shape as `_fill_combobox` (client.py:820-839).
**Patterns to follow:** `_fill_combobox`'s existing try/except-and-typed-raise shape.
**Test scenarios:**
- A `SINGLE_SELECT` field filled with a value matching a real option fills successfully (regression guard, R8).
- A `SINGLE_SELECT` field filled with a value matching no real option raises `GreenhouseFormSubmissionFailed` with a message naming the field label and attempted value, not a raw Playwright exception type.
- The raised exception still flows through `submit()`'s existing debug-artifact capture and `ReasonCode` mapping unchanged (no new `ReasonCode` needed — `SUBMISSION_FAILED` already covers this, per the "no dedicated exception type" finding from research; this unit only makes the *path* to that reason code graceful, not new).
**Verification:** Existing `MULTI_SELECT`/`CHECKBOX_GROUP`/`COMBOBOX_SELECT` mismatch tests continue passing; a new equivalent test exists for `SINGLE_SELECT`.

---

## Test Strategy

Django's built-in test runner (`docker exec jobborg-web-1 python manage.py test apps --settings=config.settings.test --keepdb`, per this repo's established convention — note the `apps` scope, not bare `manage.py test`, per a known pre-existing untracked-file collision at repo root). Unit-level coverage for U1-U3 (dataclass shape, prompt rendering, validation logic) needs no live LLM or browser — mock the LangChain client response the same way existing `langchain_client` tests already do. U4's template assertions are content-level (`response.content.decode()`), per the pattern already established in the origin plan's `test_queue_renders_answer_required_badge_and_hides_send_button`. U5 uses the existing Playwright-fixture-driven `GreenhouseFormClient` test harness (mocked page/browser, not a live Greenhouse board).

Cross-cutting regression: re-run the full `apps.auto_apply`/`apps.web` suites after all units land to confirm the label-driven text/select/checkbox/file fixture regression guard (R8, per `docs/plans/2026-08-06-002-...` R5) still holds.
