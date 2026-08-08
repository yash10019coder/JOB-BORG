"""Tests for the pluggable LLM answer-inference service (U4).

``resolve_answers`` (apps.auto_apply.llm.base) is the deterministic
orchestration logic exercised here -- category hard-exclusion, single-call
batching, groundedness-before-confidence gating, and the LLM-failure
fallback. All tests here use ``FakeAnswerInferenceClient``, never a real LLM
call. The concrete LangChain-backed provider
(apps.auto_apply.llm.langchain_client.LangChainAnswerInferenceClient) has its
own test suite in ``test_langchain_client.py``.
"""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.auto_apply.llm.base import (
    Question,
    QuestionAnswer,
    ResolutionReason,
    evidence_appears_in,
    get_client,
    resolve_answers,
)
from apps.auto_apply.llm.categories import QuestionCategory
from apps.auto_apply.llm.langchain_client import LangChainAnswerInferenceClient


class FakeAnswerInferenceClient:
    """Test double satisfying ``AnswerInferenceClient``.

    Records every batch passed to ``infer()`` (so tests can assert call
    count / batching behavior) and returns pre-programmed answers keyed by
    question id. Optionally raises to exercise the error path.
    """

    def __init__(self, answers_by_id=None, raises: Exception | None = None):
        self.answers_by_id = answers_by_id or {}
        self.raises = raises
        self.calls: list[tuple[list[Question], str, object]] = []

    def infer(self, questions, resume_text, profile):
        self.calls.append((questions, resume_text, profile))
        if self.raises is not None:
            raise self.raises
        return [self.answers_by_id[q.id] for q in questions if q.id in self.answers_by_id]


RESUME_TEXT = (
    "Jane Doe. Senior Software Engineer with 6 years of Python experience. "
    "Built and shipped a Django-based billing platform at Acme Corp."
)
PROFILE = SimpleNamespace(full_name="Jane Doe", headline="Senior Software Engineer")


class ResolveAnswersHappyPathTests(SimpleTestCase):
    @override_settings(AUTO_APPLY_CONFIDENCE_THRESHOLD=0.75)
    def test_confidently_answerable_question_resolves_no_review(self):
        question = Question(id="q1", text="How many years of Python experience do you have?")
        llm_client = FakeAnswerInferenceClient(
            answers_by_id={
                "q1": QuestionAnswer(
                    question_id="q1",
                    answer="6 years",
                    evidence=["6 years of Python experience"],
                    self_reported_confidence=0.95,
                    insufficient_evidence=False,
                )
            }
        )

        [resolved] = resolve_answers([question], RESUME_TEXT, PROFILE, llm_client)

        self.assertEqual(resolved.answer, "6 years")
        self.assertFalse(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.OK)
        self.assertEqual(resolved.category, QuestionCategory.GENERIC)


class ResolveAnswersBatchingTests(SimpleTestCase):
    def test_multiple_questions_result_in_a_single_llm_call(self):
        questions = [
            Question(id="q1", text="How many years of Python experience do you have?"),
            Question(id="q2", text="What company did you most recently work at?"),
            Question(id="q3", text="What is your current job title?"),
        ]
        llm_client = FakeAnswerInferenceClient(
            answers_by_id={
                "q1": QuestionAnswer("q1", "6 years", ["6 years of Python experience"], 0.9, False),
                "q2": QuestionAnswer("q2", "Acme Corp", ["Acme Corp"], 0.9, False),
                "q3": QuestionAnswer("q3", "Senior Software Engineer", ["Senior Software Engineer"], 0.9, False),
            }
        )

        resolved = resolve_answers(questions, RESUME_TEXT, PROFILE, llm_client)

        self.assertEqual(len(llm_client.calls), 1, "expected exactly one batched infer() call")
        called_questions, called_resume_text, called_profile = llm_client.calls[0]
        self.assertEqual([q.id for q in called_questions], ["q1", "q2", "q3"])
        self.assertEqual(called_resume_text, RESUME_TEXT)
        self.assertIs(called_profile, PROFILE)
        self.assertEqual(len(resolved), 3)
        self.assertTrue(all(not r.needs_review for r in resolved))


class ResolveAnswersInsufficientEvidenceTests(SimpleTestCase):
    def test_insufficient_evidence_forces_review_regardless_of_confidence(self):
        question = Question(id="q1", text="What is your favorite programming paradigm?")
        llm_client = FakeAnswerInferenceClient(
            answers_by_id={
                "q1": QuestionAnswer(
                    question_id="q1",
                    answer="Functional programming",
                    evidence=[],
                    self_reported_confidence=0.99,
                    insufficient_evidence=True,
                )
            }
        )

        [resolved] = resolve_answers([question], RESUME_TEXT, PROFILE, llm_client)

        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.INSUFFICIENT_EVIDENCE)


class ResolveAnswersGroundednessTests(SimpleTestCase):
    def test_high_confidence_but_ungrounded_evidence_forces_review(self):
        """The deterministic groundedness check overrides self-reported
        confidence unconditionally -- this is the primary defense against a
        manipulated or hallucinated answer surviving into a draft."""
        question = Question(id="q1", text="What is your current job title?")
        llm_client = FakeAnswerInferenceClient(
            answers_by_id={
                "q1": QuestionAnswer(
                    question_id="q1",
                    answer="Chief Executive Officer",
                    evidence=["Chief Executive Officer of Acme Corp"],
                    self_reported_confidence=0.99,
                    insufficient_evidence=False,
                )
            }
        )

        [resolved] = resolve_answers([question], RESUME_TEXT, PROFILE, llm_client)

        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.UNGROUNDED_EVIDENCE)

    def test_low_confidence_grounded_answer_still_forces_review(self):
        question = Question(id="q1", text="What is your current job title?")
        llm_client = FakeAnswerInferenceClient(
            answers_by_id={
                "q1": QuestionAnswer(
                    question_id="q1",
                    answer="Senior Software Engineer",
                    evidence=["Senior Software Engineer"],
                    self_reported_confidence=0.2,
                    insufficient_evidence=False,
                )
            }
        )

        with override_settings(AUTO_APPLY_CONFIDENCE_THRESHOLD=0.75):
            [resolved] = resolve_answers([question], RESUME_TEXT, PROFILE, llm_client)

        self.assertTrue(resolved.needs_review)
        self.assertEqual(resolved.reason, ResolutionReason.LOW_CONFIDENCE)


class ResolveAnswersHardExcludedCategoryTests(SimpleTestCase):
    def test_hard_excluded_category_never_reaches_the_llm_client(self):
        question = Question(
            id="q1", text="Are you legally authorized to work in the United States?"
        )
        llm_client = FakeAnswerInferenceClient(answers_by_id={})

        [resolved] = resolve_answers([question], RESUME_TEXT, PROFILE, llm_client)

        self.assertEqual(llm_client.calls, [], "LLM client must never be called for hard-excluded categories")
        self.assertTrue(resolved.needs_review)
        self.assertIsNone(resolved.answer)
        self.assertEqual(resolved.reason, ResolutionReason.HARD_EXCLUDED_CATEGORY)
        self.assertEqual(resolved.category, QuestionCategory.WORK_AUTHORIZATION)

    def test_mixed_batch_only_sends_allowed_questions_to_llm(self):
        sponsorship_question = Question(id="q1", text="Will you now or in the future require visa sponsorship?")
        salary_question = Question(id="q2", text="What are your salary expectations?")
        generic_question = Question(id="q3", text="What company did you most recently work at?")
        llm_client = FakeAnswerInferenceClient(
            answers_by_id={
                "q3": QuestionAnswer("q3", "Acme Corp", ["Acme Corp"], 0.9, False),
            }
        )

        resolved = resolve_answers(
            [sponsorship_question, salary_question, generic_question],
            RESUME_TEXT,
            PROFILE,
            llm_client,
        )

        self.assertEqual(len(llm_client.calls), 1)
        called_questions = llm_client.calls[0][0]
        self.assertEqual([q.id for q in called_questions], ["q3"])

        by_id = {r.question_id: r for r in resolved}
        self.assertTrue(by_id["q1"].needs_review)
        self.assertEqual(by_id["q1"].reason, ResolutionReason.HARD_EXCLUDED_CATEGORY)
        self.assertTrue(by_id["q2"].needs_review)
        self.assertEqual(by_id["q2"].reason, ResolutionReason.HARD_EXCLUDED_CATEGORY)
        self.assertFalse(by_id["q3"].needs_review)


class ResolveAnswersErrorPathTests(SimpleTestCase):
    def test_llm_call_raising_resolves_to_needs_review_not_a_crash(self):
        question = Question(id="q1", text="What company did you most recently work at?")
        llm_client = FakeAnswerInferenceClient(raises=TimeoutError("upstream timed out"))

        [resolved] = resolve_answers([question], RESUME_TEXT, PROFILE, llm_client)

        self.assertTrue(resolved.needs_review)
        self.assertIsNone(resolved.answer)
        self.assertEqual(resolved.reason, ResolutionReason.LLM_CALL_FAILED)

    def test_llm_call_raising_forces_review_for_the_whole_batch(self):
        """Batch-level failure is atomic (accepted tradeoff per the plan):
        every co-batched allowed-category question degrades to
        needs_review together, not just one."""
        questions = [
            Question(id="q1", text="What company did you most recently work at?"),
            Question(id="q2", text="What is your current job title?"),
        ]
        llm_client = FakeAnswerInferenceClient(raises=RuntimeError("boom"))

        resolved = resolve_answers(questions, RESUME_TEXT, PROFILE, llm_client)

        self.assertEqual(len(llm_client.calls), 1)
        self.assertTrue(all(r.needs_review for r in resolved))
        self.assertTrue(all(r.reason == ResolutionReason.LLM_CALL_FAILED for r in resolved))


class EvidenceAppearsInTests(SimpleTestCase):
    def test_empty_evidence_is_never_grounded(self):
        self.assertFalse(evidence_appears_in([], RESUME_TEXT, PROFILE))

    def test_evidence_matched_case_insensitively(self):
        self.assertTrue(
            evidence_appears_in(["JANE DOE"], RESUME_TEXT, PROFILE)
        )

    def test_evidence_not_in_resume_or_profile_is_not_grounded(self):
        self.assertFalse(
            evidence_appears_in(["I have a PhD in astrophysics"], RESUME_TEXT, PROFILE)
        )

    def test_partial_match_of_multiple_spans_fails_closed(self):
        self.assertFalse(
            evidence_appears_in(
                ["6 years of Python experience", "PhD in astrophysics"],
                RESUME_TEXT,
                PROFILE,
            )
        )


class GetClientRegistryTests(SimpleTestCase):
    def test_default_provider_resolves_to_anthropic_client(self):
        with override_settings(AUTO_APPLY_LLM_PROVIDER="anthropic"):
            client = get_client(client=object())
        self.assertIsInstance(client, LangChainAnswerInferenceClient)
        self.assertEqual(client._provider_config.init_model, "anthropic")

    def test_unregistered_provider_raises(self):
        with override_settings(AUTO_APPLY_LLM_PROVIDER="some-unregistered-vendor"):
            with self.assertRaises(ValueError):
                get_client()

    def test_all_four_providers_resolve(self):
        for provider in ("anthropic", "openai", "google", "nvidia"):
            with self.subTest(provider=provider):
                client = get_client(provider=provider, client=object())
                self.assertIsInstance(client, LangChainAnswerInferenceClient)

    def test_get_client_signature_unchanged_for_drafting_call_site(self):
        # apps/auto_apply/services/drafting.py calls get_client() with zero
        # arguments -- this must keep working without a diff to that file.
        with override_settings(AUTO_APPLY_LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="test-key"):
            with mock.patch("apps.auto_apply.llm.langchain_client.init_chat_model"):
                get_client()
