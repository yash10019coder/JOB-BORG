---
title: Multi-Provider LLM Answer Inference via LangChain
type: feat
status: completed
date: 2026-08-08
---

# Multi-Provider LLM Answer Inference via LangChain

## Summary

`apps/auto_apply/llm/` today has two hand-rolled, vendor-specific clients (`AnthropicAnswerInferenceClient` using the native `anthropic` SDK, `NvidiaAnswerInferenceClient` using the `openai` SDK pointed at NVIDIA NIM) selected through a small `CLIENT_REGISTRY: dict[str, tuple[module_path, class_name]]` in `apps/auto_apply/llm/base.py`. Both clients duplicate the same system prompt, the same `_QuestionAnswerBatchSchema` Pydantic shape, and the same XML-delimited prompt-building logic, differing only in how they call their vendor SDK and how they get structured output back.

This plan replaces both hand-rolled clients with **one** LangChain-backed client (`LangChainAnswerInferenceClient`) driven by a small provider-configuration table, using LangChain's `init_chat_model()` + `with_structured_output()` as the uniform integration surface across vendors. Two brand-new providers (direct OpenAI, Google Gemini) are added on top of the existing two (Anthropic, NVIDIA NIM) to demonstrate that adding a provider is now a config entry, not a new hand-rolled file.

**A past review in this codebase already flagged the existing `CLIENT_REGISTRY` pattern as a P2 premature-abstraction concern** (see origin note below) — a registry of *classes* for what was, at the time, a single real implementation. This plan is explicit about not repeating that mistake in a new form: it collapses the abstraction to one client class plus a *data-only* provider config dict, rather than layering LangChain's own provider abstraction on top of the existing class registry. `AnswerInferenceClient` (the `Protocol`), `resolve_answers`, and `evidence_appears_in` in `base.py` — the actual security/business-logic boundary — are unchanged.

(see origin: `docs/plans/2026-08-04-001-feat-auto-apply-greenhouse-email-verification-plan.md`, Decision D4, which independently avoided repeating this same over-abstraction and cites it by name)

## Requirements

- R1. `apps/auto_apply/llm` supports at least four LLM providers for answer inference: Anthropic (existing default), NVIDIA NIM (existing), OpenAI (new), Google Gemini (new) — all through one code path, not one hand-rolled client class per vendor.
- R2. Adding a fifth provider in the future is achievable by adding one entry to a provider-configuration table (model string, API key setting, optional `base_url`, structured-output method) — no new client class, no new file.
- R3. The `AnswerInferenceClient` Protocol (`infer(questions, resume_text, profile) -> list[QuestionAnswer]`) is preserved exactly as-is; `apps/auto_apply/services/drafting.py`'s call site (`llm_base.get_client()`) requires no changes.
- R4. Every provider's `infer()` call remains a single batched call per draft (the existing docstring-enforced invariant) — LangChain's per-provider wrapper must not introduce per-question round trips.
- R5. Structured, schema-validated output (the `_QuestionAnswerBatchSchema` shape: `question_id`, `answer`, `evidence`, `self_reported_confidence`, `insufficient_evidence`) is preserved for every provider, including NVIDIA NIM's small instruct model, which has no native structured-output mode.
- R6. Every real (non-test) provider client construction has an explicit, bounded request timeout sourced from `settings.AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS` — regression guard for a previously-fixed silent-hang bug (confirmed live: an unbounded NIM call blocked 5+ minutes with nothing to kill it). This must hold for all four providers, not just the two that had it before.
- R7. `resolve_answers`'s existing `except Exception` failure handling around `llm_client.infer(...)` continues to catch every provider's failure mode (schema-validation failure, auth failure, rate limit, timeout, malformed response) without any provider-specific exception handling added to `base.py`.
- R8. Existing behavior for Anthropic and NVIDIA NIM is preserved from the caller's perspective — same settings names still resolve to a working client, same category hard-exclusion and groundedness-gate behavior in `resolve_answers` is untouched.

## Scope Boundaries

- Not in scope: changing `resolve_answers`, `evidence_appears_in`, `categories.py`'s classifier, or the `Question`/`QuestionAnswer`/`ResolvedAnswer` dataclasses in `base.py` — this plan only replaces *how* `infer()` talks to a vendor, not the orchestration/security logic around it.
- Not in scope: per-user or per-request provider override. Provider selection remains settings-only (`AUTO_APPLY_LLM_PROVIDER`), matching the existing single production call site in `drafting.py`, which never threads a provider choice through from caller context.
- Not in scope: adding `langchain-nvidia-ai-endpoints`. NVIDIA NIM continues to be reached through `langchain-openai`'s `ChatOpenAI` pointed at NIM's OpenAI-compatible `base_url`, mirroring what the current hand-rolled client already does with the raw `openai` SDK — lower dependency footprint, officially supported.
- Not in scope: LangChain's broader framework surface (chains, agents, memory, tools/agentic loops, retrieval). Only `init_chat_model()` and `BaseChatModel.with_structured_output()` are used.
- Not in scope: streaming responses. `infer()` returns a complete batch, matching current behavior for all providers.

### Deferred to Follow-Up Work

- Per-provider retry/backoff tuning beyond LangChain's/each SDK's own defaults — no evidence today's providers need it; revisit if failure rates justify it.
- A formal `docs/solutions/` entry documenting the "registry of classes vs. registry of config" resolution for this area, so the reasoning in this plan's Summary is discoverable outside plan-doc archaeology (the current P2 finding is only recorded inline in this and the referenced origin plan, per repo research — no standalone solutions-track entry exists for it).

## Key Technical Decisions

### D1. Collapse the provider abstraction to one client class + a data-only config table

Delete `CLIENT_REGISTRY: dict[str, tuple[module_path, class_name]]` and its `import_module`/`getattr` dispatch in `base.py`. Replace with `_PROVIDER_CONFIGS: dict[str, ProviderConfig]`, where `ProviderConfig` is a small frozen dataclass (`init_model: str` — the `init_chat_model()` provider prefix, e.g. `"anthropic"`/`"openai"`/`"google_genai"`; `default_model: str`; `api_key_setting: str`; `base_url: str | None`; `structured_output_method: str`). `get_client(provider=None, **kwargs)` keeps its exact current signature and settings-driven default, but its body becomes: look up `_PROVIDER_CONFIGS[provider]`, raise the same `ValueError` shape on an unknown key, and construct the single `LangChainAnswerInferenceClient(provider_config, **kwargs)`.

This is the deliberate reconciliation of the two competing signals from research: `apps/jobs/ingestion/dispatch.py`'s registry-by-key pattern is repo-endorsed precedent (AGENTS.md calls it out approvingly) for genuinely-plural implementations, but the *specific* thing flagged as premature was a registry of **classes** built for what was then one real vendor. Four real, concurrently-needed providers is no longer "premature," and a dict of plain config values (not modules/classes) is a materially thinner abstraction than what was flagged — there is exactly one client class in the codebase after this change, not four.

### D2. One generic client, driven by `init_chat_model()` + `with_structured_output()`

`LangChainAnswerInferenceClient.__init__(self, provider_config: ProviderConfig, api_key=None, model=None, client=None)` builds (or accepts an injected) `BaseChatModel` via `init_chat_model(f"{provider_config.init_model}:{model or provider_config.default_model}", api_key=..., timeout=settings.AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS, base_url=provider_config.base_url)`, then calls `.with_structured_output(_QuestionAnswerBatchSchema, method=provider_config.structured_output_method)` once at construction time and reuses the bound runnable across `infer()` calls. `infer()` invokes it once with the same XML-delimited prompt shape the current clients already build (moved here, deduplicated) and maps the returned Pydantic instance's `.answers` into `QuestionAnswer` objects exactly as the current clients do.

`client` stays the test-injection seam (same convention as today): when supplied, it is used directly as the already-configured structured-output runnable, and `init_chat_model` is never called — this is what keeps tests network-free.

### D3. Structured-output method is per-provider config, not a single hardcoded value

Research confirms `with_structured_output()`'s default `method="function_calling"` is not guaranteed to work for NIM's small instruct model, which is exactly why the existing hand-rolled NVIDIA client resorts to prompt-instructed raw-JSON parsing today. `ProviderConfig.structured_output_method` lets each provider pick the right strategy independently (`"function_calling"` for Anthropic/OpenAI/Google, `"json_mode"` for NVIDIA NIM if `function_calling` proves unreliable against the specific NIM-hosted model — confirmed empirically during implementation, not assumed). This keeps the one-class design intact while giving NIM the same "prompt-based JSON, but still schema-validated by LangChain" fallback its current hand-rolled client effectively re-implements by hand.

### D4. Preserve the bounded-timeout invariant explicitly, not incidentally

The prior NVIDIA silent-hang bug (client constructed with no timeout, blocked 5+ minutes, no Celery `time_limit` to kill it) is a regression risk specifically *because* this refactor changes how every provider's underlying SDK client gets constructed. `LangChainAnswerInferenceClient`'s real-client construction path always passes `timeout=settings.AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS` into `init_chat_model(...)` for all four providers — not just the two that had this fix before. A construction-level test (parametrized across all four providers, mirroring the existing `AnthropicClientConstructionTests`/`NvidiaClientConstructionTests` pattern) is the direct regression guard.

### D5. Exception handling is unchanged in `base.py`

Research confirms LangChain does not wrap provider transport/auth/rate-limit errors in a uniform exception type — those still leak through as the underlying vendor SDK's own exception classes, and LangChain adds exactly one new type on top (`OutputParserException`, a `ValueError` subclass) for schema-validation failures. `resolve_answers`'s existing blanket `except Exception` around `llm_client.infer(...)` (`base.py`, `ResolutionReason.LLM_CALL_FAILED`) already catches all of this correctly with zero changes required.

## Files

- Modify: `requirements/base.txt` — remove direct `anthropic==0.120.2` / `openai==2.52.1` pins (no longer directly imported once the hand-rolled clients are deleted); add `langchain-core`, `langchain`, `langchain-anthropic`, `langchain-openai`, `langchain-google-genai`, each exact-pinned with a comment block naming the consuming file, matching this file's existing convention.
- Modify: `config/settings/base.py` — add `OPENAI_API_KEY`, `GOOGLE_API_KEY` env-backed settings alongside the existing `ANTHROPIC_API_KEY`/`NVIDIA_API_KEY`; update the stale "Anthropic Claude is the only registered implementation" comment.
- Create: `apps/auto_apply/llm/langchain_client.py` — `ProviderConfig` dataclass, `_PROVIDER_CONFIGS`, `LangChainAnswerInferenceClient`, the deduplicated `_SYSTEM_PROMPT`/`_build_prompt`/`_QuestionAnswerBatchSchema`.
- Modify: `apps/auto_apply/llm/base.py` — replace `CLIENT_REGISTRY`/`get_client()`'s `import_module` dispatch with the `_PROVIDER_CONFIGS` lookup from `langchain_client.py`; no changes to `Question`, `QuestionAnswer`, `ResolvedAnswer`, `resolve_answers`, `evidence_appears_in`, `_profile_text`.
- Delete: `apps/auto_apply/llm/anthropic_client.py`, `apps/auto_apply/llm/nvidia_client.py` — superseded by `langchain_client.py`.
- Modify: `apps/auto_apply/llm/__init__.py` — update the module docstring (currently says "`anthropic_client.py` is the only registered provider in this slice", already stale before this plan).
- Modify: `apps/auto_apply/tests/test_llm_answer_inference.py` — update `GetClientRegistryTests` and remove `AnthropicClientConstructionTests`/the Anthropic-SDK-faked tests (superseded by the new generic-client tests); `resolve_answers`/`evidence_appears_in` tests (using `FakeAnswerInferenceClient`) are untouched.
- Delete: `apps/auto_apply/tests/test_nvidia_client.py` — superseded by `test_langchain_client.py`.
- Create: `apps/auto_apply/tests/test_langchain_client.py` — the new client's test suite.

## Implementation Units

### U1. Add LangChain dependencies and provider settings

**Requirements:** R1, R2 | **Dependencies:** none
**Files:** `requirements/base.txt`, `config/settings/base.py`
**Approach:** Add `langchain-core`, `langchain` (for `init_chat_model`), `langchain-anthropic`, `langchain-openai` (covers both direct OpenAI and NVIDIA NIM via `base_url`), `langchain-google-genai`, each exact-pinned (`==`) to the current stable release, each with a `#` comment naming `apps/auto_apply/llm/langchain_client.py` as the consumer — mirroring the existing `anthropic==`/`openai==` comment style being removed. Remove the two lines being replaced. Add `OPENAI_API_KEY = env("OPENAI_API_KEY", default="")` and `GOOGLE_API_KEY = env("GOOGLE_API_KEY", default="")` next to the existing `ANTHROPIC_API_KEY`/`NVIDIA_API_KEY` block; update the stale single-provider comment above that settings block.
**Patterns to follow:** the existing `anthropic==`/`openai==` comment-block convention in `requirements/base.txt`; the existing `env("...", default="")` pattern for the two current API key settings.
**Test scenarios:** Test expectation: none -- pure dependency/settings addition, no behavior yet (exercised indirectly by U2/U3's tests once the settings are consumed).
**Verification:** `pip install -r requirements/base.txt` (or the project's equivalent install step) succeeds; `python manage.py check` passes with the new settings present.

### U2. Build the generic LangChain-backed client

**Requirements:** R1, R3, R4, R5, R6, R7 | **Dependencies:** U1
**Files:** create `apps/auto_apply/llm/langchain_client.py`; create `apps/auto_apply/tests/test_langchain_client.py`
**Approach:** Define `ProviderConfig` (frozen dataclass: `init_model`, `default_model`, `api_key_setting`, `base_url: str | None = None`, `structured_output_method: str = "function_calling"`). Define `_PROVIDER_CONFIGS` with four entries: `"anthropic"` (`init_model="anthropic"`, current `DEFAULT_MODEL` from the deleted `anthropic_client.py`, `api_key_setting="ANTHROPIC_API_KEY"`), `"openai"` (new, `init_model="openai"`, a current GPT model, `api_key_setting="OPENAI_API_KEY"`), `"google"` (new, `init_model="google_genai"`, a current Gemini model, `api_key_setting="GOOGLE_API_KEY"`), `"nvidia"` (`init_model="openai"`, current NIM `DEFAULT_MODEL`, `api_key_setting="NVIDIA_API_KEY"`, `base_url="https://integrate.api.nvidia.com/v1"`, `structured_output_method` set per D3's empirical finding). Move `_SYSTEM_PROMPT`, `_build_prompt`, and the `_QuestionAnswerSchema`/`_QuestionAnswerBatchSchema` Pydantic models here, deduplicated from the two files being deleted (NVIDIA's prior JSON-mode-specific prompt suffix is dropped if `method="function_calling"` proves reliable for it; kept as a `structured_output_method`-conditional prompt addition otherwise). `LangChainAnswerInferenceClient.__init__(self, provider_config, api_key=None, model=None, client=None)`: if `client` is given, store it directly as the bound structured-output runnable; otherwise call `init_chat_model(f"{provider_config.init_model}:{model or provider_config.default_model}", api_key=api_key or getattr(settings, provider_config.api_key_setting), timeout=settings.AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS, base_url=provider_config.base_url)` then `.with_structured_output(_QuestionAnswerBatchSchema, method=provider_config.structured_output_method)`, storing the result. `infer()`: empty-list short-circuit (matches both current clients), otherwise one `.invoke(self._build_prompt(questions, resume_text, profile))` call on the bound runnable, mapping `result.answers` into `QuestionAnswer` objects.
**Execution note:** Test-first for the schema-mapping and empty-list-short-circuit behavior — these are pure, fast, and the direct regression surface for R4/R5.
**Patterns to follow:** the `client=None` test-injection constructor convention from the deleted `anthropic_client.py`/`nvidia_client.py`; the XML-delimited `_build_prompt` shape from those same files (prompt-injection mitigation comment carried forward verbatim — it documents that `evidence_appears_in` in `base.py`, untouched by this plan, remains the primary defense).
**Test scenarios:**
- Happy path: `infer()` with 3 questions returns 3 `QuestionAnswer` objects correctly mapped from a fake structured-output response, using `langchain_core.language_models.fake_chat_models.GenericFakeChatModel` (or an equivalent hand-rolled fake honoring the same `with_structured_output`-return-a-Pydantic-instance contract) injected via `client=`.
- Empty questions list: `infer([], ...)` returns `[]` without invoking the underlying runnable at all (regression guard mirroring both deleted clients' existing behavior).
- Single-call batching: `infer()` with N>1 questions invokes the underlying runnable exactly once, not once per question (direct test for R4, e.g. asserting a call-count-tracking fake was invoked once).
- Schema-validation failure: fake configured to raise on `.invoke(...)` (simulating LangChain's `OutputParserException`) propagates unchanged out of `infer()` — no swallowing, matches R7's expectation that `resolve_answers`'s blanket catch handles it.
- Real-client construction timeout, parametrized across all four provider keys in `_PROVIDER_CONFIGS`: patch `init_chat_model` and assert it was called with an explicit `timeout` kwarg equal to `settings.AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS` (direct regression test for D4/R6, covering all four providers in one parametrized test rather than four near-duplicate test methods).
- NVIDIA-specific: patch `init_chat_model` and assert `base_url="https://integrate.api.nvidia.com/v1"` is passed when constructing the `"nvidia"` provider config, and that no other provider passes a `base_url`.
- `client=` injection bypasses `init_chat_model` entirely: patch `init_chat_model` and assert it is never called when `client=<fake>` is supplied to the constructor.
**Verification:** `apps/auto_apply/tests/test_langchain_client.py` green; no test in the file makes a real network call (confirm no `settings.*_API_KEY` needs to be a real credential for the suite to pass).

### U3. Wire the config-driven client into `get_client()`

**Requirements:** R1, R2, R3, R8 | **Dependencies:** U2
**Files:** modify `apps/auto_apply/llm/base.py`; modify `apps/auto_apply/tests/test_llm_answer_inference.py`
**Approach:** Delete `CLIENT_REGISTRY` and its `import_module`/`getattr` body in `get_client()`. Import `_PROVIDER_CONFIGS` and `LangChainAnswerInferenceClient` from `langchain_client.py` (module-level import is safe here — the circular-import concern that originally motivated the `(module_path, class_name)` tuple indirection doesn't apply, since `langchain_client.py` will import `Question`/`QuestionAnswer` from `base.py` the same way the deleted clients did, and `base.py` no longer needs to dynamically import a class, only look up a dataclass in a dict). `get_client(provider=None, **kwargs)` keeps its exact signature: resolve `provider` from `settings.AUTO_APPLY_LLM_PROVIDER` when not given, `KeyError` on `_PROVIDER_CONFIGS[provider]` re-raised as the same `ValueError` message shape as today (`f"No AnswerInferenceClient registered for provider={provider!r}. Registered: {sorted(_PROVIDER_CONFIGS)}"`), then `return LangChainAnswerInferenceClient(_PROVIDER_CONFIGS[provider], **kwargs)`.
**Patterns to follow:** the exact current `get_client()` signature and error-message shape (R3's compatibility requirement — `drafting.py`'s `llm_base.get_client()` call site must need zero changes).
**Test scenarios:**
- `test_default_provider_resolves_to_anthropic_client`: unchanged assertion intent, updated to assert `isinstance(client, LangChainAnswerInferenceClient)` with `provider_config.init_model == "anthropic"` (or equivalent), since there is no longer an `AnthropicAnswerInferenceClient` class to `isinstance`-check against.
- `test_unregistered_provider_raises`: unchanged — `ValueError` on an unknown provider key.
- New: `test_all_four_providers_resolve` — parametrized over `"anthropic"`, `"openai"`, `"google"`, `"nvidia"`, asserting each resolves via `get_client(provider=..., client=object())` without raising (Covers R1/R2 directly).
- New: `test_get_client_signature_unchanged_for_drafting_call_site` — calling `get_client()` with zero arguments (matching `drafting.py`'s exact call shape) does not raise when `AUTO_APPLY_LLM_PROVIDER` is left at its default.
**Verification:** full `apps/auto_apply` test suite green; `apps/auto_apply/services/drafting.py` requires no diff (grep confirms `llm_base.get_client()` call site is untouched).

### U4. Remove superseded hand-rolled clients and their tests

**Requirements:** R1 | **Dependencies:** U3
**Files:** delete `apps/auto_apply/llm/anthropic_client.py`, `apps/auto_apply/llm/nvidia_client.py`, `apps/auto_apply/tests/test_nvidia_client.py`; modify `apps/auto_apply/tests/test_llm_answer_inference.py` (remove `AnthropicClientConstructionTests` and the Anthropic-SDK-faked `AnthropicAnswerInferenceClientTests`-style tests, now superseded by U2's `test_langchain_client.py`); modify `apps/auto_apply/llm/__init__.py`
**Approach:** Straightforward deletion once U2/U3 land and their replacement coverage is confirmed green — this unit exists separately so the deletion is an explicit, reviewable diff rather than silently folded into U2/U3. Update `apps/auto_apply/llm/__init__.py`'s module docstring to describe the current `langchain_client.py` shape instead of the stale "`anthropic_client.py` is the only registered provider" text.
**Test scenarios:** Test expectation: none -- pure deletion; the retained/replacement test suites (U2, U3) are the coverage for what these files used to test.
**Verification:** `grep -r "anthropic_client\|nvidia_client" apps/` returns no hits outside this plan document and git history; full `apps/auto_apply` suite still green after deletion.

## Test Strategy

Django's built-in test runner (`python manage.py test`), matching this repo's convention — no pytest. `SimpleTestCase` throughout (no DB needed, matching the existing `test_llm_answer_inference.py`/`test_nvidia_client.py` posture). Real vendor/LangChain network calls are never made in tests: the `client=` constructor seam (unchanged convention from the deleted clients) accepts a pre-built fake structured-output runnable, and `langchain_core.language_models.fake_chat_models.GenericFakeChatModel` (a stable, `langchain-core`-native utility confirmed to support `with_structured_output()`) is the preferred fake — falling back to a minimal hand-rolled fake object exposing `.invoke()` if `GenericFakeChatModel`'s structured-output behavior proves awkward to program deterministically during implementation. `init_chat_model` itself is patched (via `unittest.mock.patch` at the `langchain_client` module's import site, matching this repo's established mocking convention) for the construction/timeout/base_url tests, since those specifically need to assert on the *arguments* passed to real-client construction rather than on inference behavior.

## Risks

- **NVIDIA NIM's small instruct model may not support `method="function_calling"` reliably** (research: no guaranteed automatic fallback in LangChain from tool-calling to prompt-based JSON mode). Mitigated by D3's per-provider `structured_output_method` config field, but the *correct* value for NIM is an execution-time empirical finding, not something this plan can certify in advance — flagged explicitly rather than assumed.
- **Added dependency surface**: five new packages (`langchain-core`, `langchain`, `langchain-anthropic`, `langchain-openai`, `langchain-google-genai`) replacing two (`anthropic`, `openai`), each pulling further transitive vendor-SDK dependencies. This is a real, stated trade against this codebase's demonstrated preference for minimal dependencies in sensitive (PII-handling) code paths — accepted here because the user explicitly requested LangChain by name and because the four-provider requirement makes LangChain's unified `with_structured_output()` genuinely load-bearing, not decorative.
- **LangChain 1.x is the current LTS line but this is this codebase's first-ever LangChain adoption** — no institutional experience to draw on if an integration-package-specific quirk surfaces (e.g., the `langchain-google-genai` structured-output gap with some Gemini models noted in research). Mitigated by the parametrized construction tests and the empirical-not-assumed posture on `structured_output_method`.
- **Google/OpenAI API keys are new secrets to provision operationally** (`OPENAI_API_KEY`, `GOOGLE_API_KEY`) — an ops/deployment concern outside this plan's code scope, noted so it isn't missed at rollout.

## Verification

- Full `apps/auto_apply` test suite green, including all new/updated test files, with zero real network calls (every provider construction path exercised via mocked `init_chat_model` or an injected fake runnable).
- `grep -r "anthropic_client\|nvidia_client" apps/` confirms clean removal.
- Manual/live smoke check (not part of the automated suite, since it requires real credentials): one real `infer()` call against each of the four providers with a small fixed question set, confirming schema-valid structured output comes back and `evidence_appears_in` still gates correctly end-to-end through `resolve_answers`.
