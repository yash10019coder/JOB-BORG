---
date: 2026-08-02
topic: auto-apply-greenhouse-slice
---

# Auto-Apply: First Slice (Greenhouse Only)

## Summary

Add a first, narrow auto-apply capability under issue #17, scoped to Greenhouse only: a user opts into auto-apply on a single job from their recommendations, the system drafts the application (standard fields from Profile, remaining questions from the user's explicit saved answers or LLM inference from their resume) via headless browser automation against Greenhouse's real application page, and the user reviews and sends it from a queue. No job is ever submitted without a human clicking send.

---

## Problem Frame

Issue #17 covers auto-apply, referral outreach, and cold-email as a single deferred bucket, explicitly held back from v1 because "matching quality needs to be proven trustworthy before the system is allowed to act on a user's behalf." Today, a matched job still requires the user to leave JobBorg, open the employer's Greenhouse page, and retype the same standard fields and similar custom-question answers by hand for every application — the exact manual grind the product exists to remove. Building the full outreach bucket (referral + cold-email + auto-apply, all ATSes) at once is too large a bet against unvalidated matching quality; this doc scopes the smallest slice of auto-apply — one ATS, human-reviewed sends — that's still genuinely usable rather than a toy.

---

## Actors

- A1. User (job seeker): opts a specific job into auto-apply, maintains a small set of explicit answers for sensitive/specific questions, reviews and sends drafted applications.
- A2. Auto-apply drafting worker (system): given a job the user opted into, fetches its Greenhouse question schema, fills fields from Profile/explicit answers/LLM inference, and produces a draft or an exclusion reason.
- A3. LLM answer inferer (system): given a Greenhouse question and the user's resume/profile, returns an inferred answer plus a confidence signal; used only where no explicit answer exists.
- A4. Greenhouse (external ATS): source of the job's rendered application page (question fields, resume upload, submit action) that the automation drives directly, since no application-submission API credential is available for these employers.

---

## Key Flows

- F1. User opts a job into auto-apply
  - **Trigger:** user clicks "Auto-apply" on a job in their recommendations
  - **Actors:** A1, A2, A3, A4
  - **Steps:** worker loads the job's real Greenhouse application page and inspects its rendered fields → fills standard fields (name, email, phone, resume, LinkedIn) from Profile → for each remaining question, uses the user's explicit saved answer if one exists, otherwise asks the LLM to infer an answer from the resume/profile with a confidence signal → if any required question has neither an explicit answer nor a confident inference, or the schema needs a field type this slice doesn't support, the job is excluded from auto-apply with a reason shown to the user → otherwise a draft is created in "drafted" status and added to the review queue
  - **Outcome:** job is either queued as a draft or explicitly marked not-auto-applyable with a reason
  - **Covered by:** R1-R9

- F2. User reviews and sends a drafted application
  - **Trigger:** user opens the auto-apply review queue
  - **Actors:** A1, A4
  - **Steps:** user sees the draft with all answers, low-confidence LLM answers visually flagged → user may edit any answer → user clicks Send → system drives the real application page (via headless browser automation) to submit the final answers → on success, the corresponding JobApplication moves to Applied
  - **Outcome:** application is submitted and reflected in the user's history, or the user edits/leaves it queued without sending
  - **Covered by:** R10-R14

- F3. Draft goes stale before it's sent
  - **Trigger:** the underlying Greenhouse job posting closes or is removed before the user sends the draft
  - **Actors:** A2, A4
  - **Steps:** system detects the job is no longer open → draft is marked stale in the queue → Send is disabled for it
  - **Outcome:** user can't submit against a closed requisition
  - **Covered by:** R15

---

## Requirements

**Opt-in & drafting**
- R1. Auto-apply is available only for jobs sourced from Greenhouse in this slice.
- R2. The user triggers auto-apply per job from their recommendations; there is no automatic or bulk drafting based on match score.
- R3. On trigger, the system loads the job's real Greenhouse application page via headless browser automation and inspects its rendered fields to determine the question schema — no application-submission API credential is required.
- R4. Standard fields (name, email, phone, resume, LinkedIn) are filled from the user's existing Profile data.
- R5. For each remaining question, the system uses the user's explicit saved answer if one exists; otherwise it asks the LLM to infer an answer from the user's resume/profile, returning both an answer and a confidence signal.
- R6. If a required question has neither an explicit answer nor a confident LLM inference, or the schema includes a field type this slice doesn't support, the job is excluded from auto-apply for that user, with the reason shown and the option to apply manually as today.
- R7. A successfully-answered draft is created in a "drafted" status distinct from JobApplication's existing Saved/Applied/Dismissed statuses; JobApplication.status does not change until the draft is sent.
- R8. LLM-inferred answers below a defined confidence threshold are marked so the review UI can distinguish them from explicit or high-confidence answers.
- R9. The user maintains a small set of reusable explicit answers (e.g. work authorization, sponsorship needs) for auto-apply, separate from the full Phase 5 `answers_bank` (issue #16, out of scope here).

**Review & send**
- R10. The review queue lists all of a user's drafted applications with answers visible and editable before send.
- R11. Low-confidence LLM-inferred answers are visually distinguished in the queue so the user knows to check them.
- R12. Sending a draft drives the real Greenhouse application page via headless browser automation to submit the final (possibly edited) answers.
- R13. A successful send transitions the corresponding JobApplication to Applied status.
- R14. A failed send (submission rejected, form-shape mismatch, or bot-detection challenge blocks automation) keeps the draft in the queue with the error shown, rather than dropping it silently or marking it Applied.

**Draft lifecycle**
- R15. If the underlying Greenhouse job posting closes or is removed before the draft is sent, the draft is marked stale and cannot be sent.

---

## Acceptance Examples

- AE1. **Covers R6.** Given a Greenhouse job whose form has a required custom question with no explicit answer and no confident LLM inference, when the user clicks Auto-apply, then the job is excluded from auto-apply, the reason is shown, and the user can still apply manually.
- AE2. **Covers R5, R8, R11.** Given a question with no explicit answer but confidently inferable from the resume (e.g. years of Python experience), when the draft is created, the answer appears unflagged; given a question the LLM answers with low confidence, when the draft is created, that answer is visually flagged for review.
- AE3. **Covers R12, R13, R14.** Given a reviewed draft the user sends, when the automated submission succeeds, JobApplication becomes Applied; when it fails (rejected, blocked, or mismatched form), the draft stays queued with the error shown and JobApplication is not marked Applied.
- AE4. **Covers R15.** Given a drafted application whose job posting has since closed, when the user opens the queue, the draft shows as stale and Send is disabled.

---

## Success Criteria

- A user can go from seeing a Greenhouse job in their recommendations to a submitted application without visiting the employer's site or retyping answers, for jobs this slice can confidently handle.
- No application is ever submitted with a fabricated or low-confidence answer the user hasn't had a chance to see — unsupported jobs are excluded rather than silently mis-filled.
- A downstream planner can implement this without inventing which ATS, what "auto-apply" actually submits (draft-then-send, not fully automatic), where answers come from, or what happens on coverage gaps and staleness.

---

## Scope Boundaries

- Other ATS/job boards (Lever, Ashby, Workday, future Indeed/LinkedIn) — later slices under issue #17, once this one is proven.
- Fully automatic submission with no human review step.
- Referral outreach and cold-email — separate legs of issue #17, untouched by this slice.
- The full Phase 5 CRM schema (issue #16: `contacts`, `contact_outreach_log`, full `answers_bank`, `email_events`) — this slice needs only a minimal place to store the user's explicit answers, not that system.
- Automatic/bulk drafting triggered by match score rather than explicit per-job user action.
- Cover letter generation as a distinct feature — if a job's form requires one, it's handled as another explicit-or-LLM-inferred answer per R5, not built separately.

---

## Key Decisions

- **Draft-then-send, not autofill-and-confirm-inline or fully-automatic:** keeps a human checkpoint on every submission while matching quality is unvalidated (per issue #17's own deferral rationale), and the review queue absorbs low-confidence LLM answers without needing separate fallback logic.
- **Greenhouse-only, headless browser automation over API submission:** Greenhouse's application-submission API requires a per-employer opt-in key that each employer must manually generate and hand over — confirmed during planning research to be unavailable for the boards this product already ingests read-only, which would have made API-first exclude nearly every job rather than the rare unsupported form. Browser automation against the real rendered application page works for any ingested job regardless of employer opt-in, at the cost of higher fragility (DOM drift, bot-detection walls) that the plan documents mitigations for. (Note: an earlier draft of this doc inverted this decision — this corrects it back to what was actually decided.)
- **Hybrid answer sourcing (explicit answers + LLM inference from resume) shipped in this same first slice, not deferred:** most real Greenhouse forms carry at least one custom question — without inference, too few jobs would qualify for auto-apply to make the feature worth using.
- **Per-job opt-in trigger, not score-threshold auto-drafting:** keeps the user in control of which jobs get drafted while match-score trustworthiness is unproven.

---

## Dependencies / Assumptions

- Submission is via headless browser automation (Playwright) against Greenhouse's real rendered application page, not its API — confirmed during planning that API submission needs a per-employer key this product doesn't have. Some employer boards run bot-detection (reCAPTCHA/Cloudflare) that can block or challenge headless automation; per explicit product decision, the CAPTCHA-solving *capability* (a pluggable solver interface, attempted before falling back to fail-closed) ships in this slice, but a specific working vendor behind that interface does not — planning correctly identified that picking/provisioning an actual paid CAPTCHA-solving vendor is implementation-time follow-up work, not a same-slice deliverable, so every real challenge fails closed until a vendor is wired in. Residential proxy infrastructure is separately deferred to general rollout.
- Needs a lightweight place to persist the user's explicit answers (R9) — a small addition, not the full Phase 5 schema; exact shape is a planning decision.
- LLM answer inference needs the user's resume in a form the LLM can read (parsed text); depends on however resume storage/parsing currently works — verify during planning.
- Confidence threshold/mechanism for "auto-fill silently vs. flag for review" is a planning/tuning decision, not resolved here.

---

## Outstanding Questions

### Resolve Before Planning

_None — the product-shape decisions above (mode, ATS, answer sourcing, submission mechanism, trigger) are settled._

### Deferred to Planning

- [Affects R3, R6][Resolved during planning] Confirmed Greenhouse's application-submission API requires a per-employer opt-in key unavailable for this product's ingested boards; submission uses headless browser automation instead. See plan for form-shape-drift and bot-detection handling.
- [Affects R9][Technical] Where do the user's explicit answers live — a new small model, an extension of Profile, or something else — without duplicating the future Phase 5 `answers_bank`?
- [Affects R5, R8][Technical] What confidence threshold/mechanism does the LLM answer-inference step use, and how is it computed?
- [Affects R15][Technical] Is staleness checked proactively (scheduled sweep) or lazily (at send-time), or both?
