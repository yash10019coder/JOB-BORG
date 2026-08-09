"""Tests for ``LangChainAnswerInferenceClient`` (apps.auto_apply.llm.langchain_client).

``init_chat_model`` is patched at this module's import site for construction
tests (mirrors this repo's established mocking convention), and the
structured-output runnable itself is faked via dependency injection (the
``client=`` constructor seam) for inference-behavior tests -- no real network
call is ever made, and no real API key is required for the suite to pass.

``LangChainAnswerInferenceClientRealConstructionTests`` is the one exception:
it constructs a real (unmocked) ``init_chat_model()`` chat model per provider
with a fake API key and inspects the resulting object's own timeout field.
This makes no network call (construction only) but catches a future
langchain-* version silently dropping the ``timeout`` kwarg, which the mocked
construction tests above cannot -- they only assert the kwarg was *passed*,
not that the underlying integration actually binds it.
"""
import dataclasses
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.auto_apply.llm.base import Question
from apps.auto_apply.llm.langchain_client import (
    _PROVIDER_CONFIGS,
    LangChainAnswerInferenceClient,
    ProviderConfig,
    _QuestionAnswerBatchSchema,
    _QuestionAnswerSchema,
)

RESUME_TEXT = (
    "Jane Doe. Senior Software Engineer with 6 years of Python experience. "
    "Built and shipped a Django-based billing platform at Acme Corp."
)
PROFILE = SimpleNamespace(full_name="Jane Doe", headline="Senior Software Engineer")


class _FakeStructuredClient:
    """Test double standing in for the bound ``with_structured_output()``
    runnable -- records every prompt passed to ``invoke()`` and returns (or
    raises) a pre-programmed result."""

    def __init__(self, result=None, raises: Exception | None = None):
        self.result = result
        self.raises = raises
        self.invocations: list = []

    def invoke(self, prompt):
        self.invocations.append(prompt)
        if self.raises is not None:
            raise self.raises
        return self.result


class LangChainAnswerInferenceClientInferTests(SimpleTestCase):
    def _provider_config(self):
        return _PROVIDER_CONFIGS["anthropic"]

    def test_infer_maps_batch_response_to_question_answers(self):
        questions = [
            Question(id="q1", text="What is your name?"),
            Question(id="q2", text="Years of experience?"),
            Question(id="q3", text="Do you have a PhD?"),
        ]
        batch = _QuestionAnswerBatchSchema(
            answers=[
                _QuestionAnswerSchema(
                    question_id="q1",
                    answer="Jane Doe",
                    evidence=["Jane Doe"],
                    self_reported_confidence=0.95,
                ),
                _QuestionAnswerSchema(
                    question_id="q2",
                    answer="6 years",
                    evidence=["6 years of Python experience"],
                    self_reported_confidence=0.9,
                ),
                _QuestionAnswerSchema(
                    question_id="q3",
                    answer="",
                    evidence=[],
                    self_reported_confidence=0.0,
                    insufficient_evidence=True,
                ),
            ]
        )
        fake = _FakeStructuredClient(result=batch)
        client = LangChainAnswerInferenceClient(self._provider_config(), client=fake)

        answers = client.infer(questions, RESUME_TEXT, PROFILE)

        self.assertEqual(len(answers), 3)
        self.assertEqual(answers[0].question_id, "q1")
        self.assertEqual(answers[0].answer, "Jane Doe")
        self.assertEqual(answers[0].evidence, ["Jane Doe"])
        self.assertEqual(answers[0].self_reported_confidence, 0.95)
        self.assertFalse(answers[0].insufficient_evidence)
        self.assertTrue(answers[2].insufficient_evidence)

    def test_infer_with_no_questions_returns_empty_without_invoking(self):
        fake = _FakeStructuredClient(result=_QuestionAnswerBatchSchema(answers=[]))
        client = LangChainAnswerInferenceClient(self._provider_config(), client=fake)

        answers = client.infer([], RESUME_TEXT, PROFILE)

        self.assertEqual(answers, [])
        self.assertEqual(fake.invocations, [])

    def test_infer_makes_exactly_one_call_regardless_of_question_count(self):
        questions = [Question(id=f"q{i}", text=f"Question {i}?") for i in range(5)]
        batch = _QuestionAnswerBatchSchema(
            answers=[
                _QuestionAnswerSchema(
                    question_id=q.id,
                    answer="answer",
                    evidence=["Jane Doe"],
                    self_reported_confidence=0.9,
                )
                for q in questions
            ]
        )
        fake = _FakeStructuredClient(result=batch)
        client = LangChainAnswerInferenceClient(self._provider_config(), client=fake)

        client.infer(questions, RESUME_TEXT, PROFILE)

        self.assertEqual(len(fake.invocations), 1)

    def test_infer_propagates_structured_output_failure_unchanged(self):
        fake = _FakeStructuredClient(raises=ValueError("schema validation failed"))
        client = LangChainAnswerInferenceClient(self._provider_config(), client=fake)

        with self.assertRaises(ValueError):
            client.infer([Question(id="q1", text="?")], RESUME_TEXT, PROFILE)

    def test_infer_raises_value_error_when_structured_output_returns_none(self):
        # with_structured_output() can return None when the model declines
        # the forced tool call -- must surface as an explicit ValueError,
        # not an opaque AttributeError from `None.answers`.
        fake = _FakeStructuredClient(result=None)
        client = LangChainAnswerInferenceClient(self._provider_config(), client=fake)

        with self.assertRaises(ValueError):
            client.infer([Question(id="q1", text="?")], RESUME_TEXT, PROFILE)


class LangChainAnswerInferenceClientConstructionTests(SimpleTestCase):
    """Real (unfaked) client construction must always go through
    ``init_chat_model`` with an explicit, bounded timeout -- regression guard
    for a previously-fixed silent-hang bug (an unbounded NVIDIA NIM call
    blocked a worker slot for 5+ minutes with nothing to kill it). This must
    hold for every provider, not only the ones that had the fix before
    LangChain."""

    def _patched_init_chat_model(self):
        return mock.patch("apps.auto_apply.llm.langchain_client.init_chat_model")

    _API_KEYS_BY_SETTING = {
        "ANTHROPIC_API_KEY": "anthropic-key",
        "OPENAI_API_KEY": "openai-key",
        "GOOGLE_API_KEY": "google-key",
        "NVIDIA_API_KEY": "nvidia-key",
    }

    @override_settings(
        ANTHROPIC_API_KEY="anthropic-key",
        OPENAI_API_KEY="openai-key",
        GOOGLE_API_KEY="google-key",
        NVIDIA_API_KEY="nvidia-key",
        AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS=30,
    )
    def test_every_registered_provider_constructs_with_explicit_timeout(self):
        for provider_key, provider_config in _PROVIDER_CONFIGS.items():
            with self.subTest(provider=provider_key), self._patched_init_chat_model() as mock_init:
                LangChainAnswerInferenceClient(provider_config)

                mock_init.assert_called_once()
                args, kwargs = mock_init.call_args
                self.assertIn("timeout", kwargs)
                self.assertEqual(kwargs["timeout"], 30)
                self.assertEqual(
                    kwargs["api_key"], self._API_KEYS_BY_SETTING[provider_config.api_key_setting]
                )
                self.assertEqual(
                    args[0], f"{provider_config.init_model}:{provider_config.default_model}"
                )

                chat_model_mock = mock_init.return_value
                chat_model_mock.with_structured_output.assert_called_once_with(
                    _QuestionAnswerBatchSchema, method=provider_config.structured_output_method
                )

    @override_settings(NVIDIA_API_KEY="nvidia-key", AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS=30)
    def test_nvidia_provider_forwards_generation_constraints(self):
        # Regression guard: the deleted hand-rolled nvidia_client.py
        # constrained the small open-weight model's generation
        # (max_tokens/temperature/top_p); this must survive the LangChain
        # refactor via ProviderConfig.model_kwargs, not be silently dropped.
        with self._patched_init_chat_model() as mock_init:
            LangChainAnswerInferenceClient(_PROVIDER_CONFIGS["nvidia"])

        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["max_tokens"], 1024)
        self.assertEqual(kwargs["temperature"], 0.2)
        self.assertEqual(kwargs["top_p"], 0.7)

    @override_settings(NVIDIA_API_KEY="nvidia-key", AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS=30)
    def test_nvidia_provider_passes_nim_base_url(self):
        with self._patched_init_chat_model() as mock_init:
            LangChainAnswerInferenceClient(_PROVIDER_CONFIGS["nvidia"])

        _, kwargs = mock_init.call_args
        self.assertEqual(kwargs["base_url"], "https://integrate.api.nvidia.com/v1")

    @override_settings(ANTHROPIC_API_KEY="anthropic-key", AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS=30)
    def test_non_nvidia_provider_passes_no_base_url(self):
        with self._patched_init_chat_model() as mock_init:
            LangChainAnswerInferenceClient(_PROVIDER_CONFIGS["anthropic"])

        _, kwargs = mock_init.call_args
        self.assertIsNone(kwargs["base_url"])

    def test_injected_client_bypasses_init_chat_model_entirely(self):
        with self._patched_init_chat_model() as mock_init:
            LangChainAnswerInferenceClient(
                _PROVIDER_CONFIGS["anthropic"], client=_FakeStructuredClient()
            )

        mock_init.assert_not_called()


class LangChainAnswerInferenceClientRealConstructionTests(SimpleTestCase):
    """Constructs a real (unmocked) chat model per provider with a fake API
    key and inspects its own timeout field -- catches a future langchain-*
    version silently dropping the ``timeout`` kwarg, which the mocked
    construction tests above cannot (they only prove the kwarg was passed to
    ``init_chat_model``, not that the specific integration bound it). Makes
    no network call: constructing a chat model object does not itself
    contact the provider."""

    @override_settings(ANTHROPIC_API_KEY="fake-key", AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS=30)
    def test_anthropic_binds_timeout(self):
        client = LangChainAnswerInferenceClient(_PROVIDER_CONFIGS["anthropic"])
        self.assertEqual(client._structured_client.first.default_request_timeout, 30.0)

    @override_settings(OPENAI_API_KEY="fake-key", AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS=30)
    def test_openai_binds_timeout(self):
        client = LangChainAnswerInferenceClient(_PROVIDER_CONFIGS["openai"])
        self.assertEqual(client._structured_client.first.request_timeout, 30.0)

    @override_settings(GOOGLE_API_KEY="fake-key", AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS=30)
    def test_google_binds_timeout(self):
        client = LangChainAnswerInferenceClient(_PROVIDER_CONFIGS["google"])
        self.assertEqual(client._structured_client.first.timeout, 30.0)

    @override_settings(NVIDIA_API_KEY="fake-key", AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS=30)
    def test_nvidia_binds_timeout(self):
        client = LangChainAnswerInferenceClient(_PROVIDER_CONFIGS["nvidia"])
        self.assertEqual(client._structured_client.first.request_timeout, 30.0)


class ProviderConfigTests(SimpleTestCase):
    def test_registered_providers_include_all_four_vendors(self):
        self.assertEqual(
            set(_PROVIDER_CONFIGS), {"anthropic", "openai", "google", "nvidia"}
        )

    def test_provider_config_is_frozen(self):
        config = ProviderConfig(
            init_model="anthropic", default_model="m", api_key_setting="ANTHROPIC_API_KEY"
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.default_model = "other"
