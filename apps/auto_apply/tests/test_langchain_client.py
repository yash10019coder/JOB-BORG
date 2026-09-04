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
    _build_prompt,
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


class BuildPromptOptionsTests(SimpleTestCase):
    """U2: option-bearing questions must render their exact option set in
    the prompt so the LLM can be instructed to answer within it."""

    def test_option_bearing_question_renders_each_option_as_its_own_element(self):
        question = Question(
            id="q1",
            text="What is your highest level of education?",
            field_type="single_select",
            options=("High School", "Bachelor's", "Other"),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        self.assertIn("<option>High School</option>", prompt)
        # Apostrophes are XML-escaped to &#x27;
        self.assertIn("<option>Bachelor&#x27;s</option>", prompt)
        self.assertIn("<option>Other</option>", prompt)

    def test_option_containing_delimiter_like_punctuation_survives_intact(self):
        # Regression guard: a single pipe-delimited options="..." attribute
        # is ambiguous when an employer-authored option label itself
        # contains "|" -- the per-<option>-element shape has no such
        # collision, since each option is its own text node.
        question = Question(
            id="q1",
            text="Employment type?",
            field_type="single_select",
            options=("Full-time | Part-time", "Contract", "Other"),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        self.assertIn("<option>Full-time | Part-time</option>", prompt)
        self.assertIn("<option>Contract</option>", prompt)

    def test_free_text_question_renders_no_options_element(self):
        question = Question(id="q1", text="Tell us about yourself", field_type="text")

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        self.assertNotIn("<options>", prompt)
        self.assertIn('<question id="q1">', prompt)

    def test_option_containing_xml_unsafe_less_than_sign(self):
        # Regression guard: option labels containing < must be XML-escaped
        # to &lt; to avoid breaking the option element structure.
        # Without escaping, <option>Java < 8</option> is invalid XML.
        question = Question(
            id="q1",
            text="Java version?",
            field_type="single_select",
            options=("Java < 8", "Java >= 8", "Other"),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        # The prompt must contain escaped version of the less-than sign
        self.assertIn("Java &lt; 8", prompt)
        # The original unsafe version should not appear in option tags
        self.assertNotIn("<option>Java < 8</option>", prompt)

    def test_option_containing_xml_unsafe_greater_than_sign(self):
        # Regression guard: > must be escaped to &gt;
        question = Question(
            id="q1",
            text="Experience level?",
            field_type="single_select",
            options=("Years > 5", "Years <= 5", "Other"),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        self.assertIn("Years &gt; 5", prompt)
        self.assertIn("Years &lt;= 5", prompt)

    def test_option_containing_xml_unsafe_ampersand(self):
        # Regression guard: & must be escaped to &amp;
        question = Question(
            id="q1",
            text="Technology?",
            field_type="single_select",
            options=("C++ & C#", "Python & Java", "Other"),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        self.assertIn("C++ &amp; C#", prompt)
        self.assertIn("Python &amp; Java", prompt)

    def test_option_containing_multiple_xml_unsafe_characters(self):
        # Regression guard: complex labels with multiple unsafe characters
        question = Question(
            id="q1",
            text="Stack?",
            field_type="single_select",
            options=("Node.js & React < 18", "Python > 3.8 & Django", "Other"),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        self.assertIn("Node.js &amp; React &lt; 18", prompt)
        self.assertIn("Python &gt; 3.8 &amp; Django", prompt)

    def test_empty_options_list_renders_no_options_element(self):
        # Regression guard: empty options list should not render the
        # <options> element (empty tuple is falsy, so the if check prevents it)
        question = Question(
            id="q1",
            text="Select something?",
            field_type="single_select",
            options=(),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        # No options element should be rendered for empty options
        self.assertNotIn("<options>", prompt)
        self.assertNotIn("</options>", prompt)
        # Count <option> tags - should be 0
        option_count = prompt.count("<option>")
        self.assertEqual(option_count, 0)

    def test_very_long_option_list_renders_all_options(self):
        # Regression guard: very long option lists should all render
        options = tuple(f"Option {i}" for i in range(100))
        question = Question(
            id="q1",
            text="Pick one?",
            field_type="single_select",
            options=options,
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        # All options should be present
        option_count = prompt.count("<option>")
        self.assertEqual(option_count, 100)
        self.assertIn("<option>Option 0</option>", prompt)
        self.assertIn("<option>Option 50</option>", prompt)
        self.assertIn("<option>Option 99</option>", prompt)

    def test_options_differing_only_in_whitespace(self):
        # Regression guard: options that look similar but differ in whitespace
        # should remain distinct and render correctly
        question = Question(
            id="q1",
            text="Whitespace test?",
            field_type="single_select",
            options=("Option A", "Option  A", "  Option A", "Option A "),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        # All variants should be present
        self.assertIn("<option>Option A</option>", prompt)
        self.assertIn("<option>Option  A</option>", prompt)
        self.assertIn("<option>  Option A</option>", prompt)
        self.assertIn("<option>Option A </option>", prompt)
        option_count = prompt.count("<option>")
        self.assertEqual(option_count, 4)

    def test_options_differing_only_in_case(self):
        # Regression guard: options that differ only in case should remain distinct
        question = Question(
            id="q1",
            text="Case test?",
            field_type="single_select",
            options=("YES", "Yes", "yes", "yeS"),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        # All variants should be present
        self.assertIn("<option>YES</option>", prompt)
        self.assertIn("<option>Yes</option>", prompt)
        self.assertIn("<option>yes</option>", prompt)
        self.assertIn("<option>yeS</option>", prompt)
        option_count = prompt.count("<option>")
        self.assertEqual(option_count, 4)

    def test_option_with_both_single_and_double_quotes(self):
        # Regression guard: quotes in option text should not break XML structure.
        # Both single and double quotes are HTML-escaped.
        question = Question(
            id="q1",
            text="Quote test?",
            field_type="single_select",
            options=('He said "hello"', "She said 'goodbye'", "Other"),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        # Double quotes are escaped to &quot;, single quotes to &#x27;
        # The text inside the quotes should still be present
        self.assertIn("hello", prompt)
        self.assertIn("goodbye", prompt)
        # Verify we have three options
        option_count = prompt.count("<option>")
        self.assertEqual(option_count, 3)
        # Verify quote escaping
        self.assertIn("&quot;hello&quot;", prompt)
        self.assertIn("&#x27;goodbye&#x27;", prompt)

    def test_prompt_validity_with_xml_unsafe_options(self):
        # Regression guard: the resulting prompt must be well-formed XML
        # even with unsafe option values. This is a structural test that
        # verifies the <options> element is properly closed.
        question = Question(
            id="q1",
            text="Tech test?",
            field_type="single_select",
            options=("C++ & C#", "Java < 8", "Python > 3.8", "Other"),
        )

        prompt = _build_prompt([question], RESUME_TEXT, PROFILE)

        # Check that options block is properly structured
        # Count opening and closing tags should match
        options_open = prompt.count("<options>")
        options_close = prompt.count("</options>")
        self.assertEqual(options_open, 1)
        self.assertEqual(options_close, 1)

        # All options should be present (4 total)
        option_count = prompt.count("<option>")
        option_close_count = prompt.count("</option>")
        self.assertEqual(option_count, 4)
        self.assertEqual(option_close_count, 4)

        # Verify structure integrity by checking that each option opens and closes properly
        lines = prompt.split("\n")
        for line in lines:
            if "<option>" in line:
                self.assertTrue(
                    line.endswith("</option>"),
                    f"Option line not properly closed: {line}",
                )


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
