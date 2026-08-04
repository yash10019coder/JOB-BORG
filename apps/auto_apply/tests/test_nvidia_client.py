"""Tests for ``NvidiaAnswerInferenceClient`` (apps.auto_apply.llm.nvidia_client).

The underlying OpenAI SDK client (NIM's OpenAI-compatible endpoint) is
faked, so no network call is ever made. Mirrors
``test_llm_answer_inference.py``'s ``AnthropicAnswerInferenceClientTests``
structure -- same ``AnswerInferenceClient`` contract, different transport
(hand-parsed JSON from chat-completion content instead of native
structured-output parsing).
"""
import json
from types import SimpleNamespace

from django.test import SimpleTestCase, override_settings

from apps.auto_apply.llm.base import Question, get_client
from apps.auto_apply.llm.nvidia_client import NvidiaAnswerInferenceClient

RESUME_TEXT = (
    "Jane Doe. Senior Software Engineer with 6 years of Python experience. "
    "Built and shipped a Django-based billing platform at Acme Corp."
)
PROFILE = SimpleNamespace(full_name="Jane Doe", headline="Senior Software Engineer")


class GetClientNvidiaRegistrationTests(SimpleTestCase):
    def test_nvidia_provider_resolves_to_nvidia_client(self):
        with override_settings(AUTO_APPLY_LLM_PROVIDER="nvidia"):
            client = get_client(client=object())
        self.assertIsInstance(client, NvidiaAnswerInferenceClient)


def _chat_response(content):
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


class _FakeCompletionsResource:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class _FakeChatResource:
    def __init__(self, response):
        self.completions = _FakeCompletionsResource(response)


class _FakeOpenAISDKClient:
    def __init__(self, response):
        self.chat = _FakeChatResource(response)


class NvidiaAnswerInferenceClientTests(SimpleTestCase):
    def _batch_json(self, items):
        return json.dumps({"answers": items})

    def test_infer_makes_a_single_call_for_multiple_questions(self):
        content = self._batch_json(
            [
                {
                    "question_id": "q1",
                    "answer": "6 years",
                    "evidence": ["6 years of Python experience"],
                    "self_reported_confidence": 0.9,
                    "insufficient_evidence": False,
                },
                {
                    "question_id": "q2",
                    "answer": "Acme Corp",
                    "evidence": ["Acme Corp"],
                    "self_reported_confidence": 0.9,
                    "insufficient_evidence": False,
                },
            ]
        )
        fake_sdk_client = _FakeOpenAISDKClient(_chat_response(content))
        client = NvidiaAnswerInferenceClient(client=fake_sdk_client)
        questions = [
            Question(id="q1", text="Years of Python experience?"),
            Question(id="q2", text="Most recent employer?"),
        ]

        answers = client.infer(questions, RESUME_TEXT, PROFILE)

        self.assertEqual(len(fake_sdk_client.chat.completions.calls), 1)
        self.assertEqual(len(answers), 2)
        self.assertEqual(answers[0].question_id, "q1")
        self.assertEqual(answers[0].answer, "6 years")
        self.assertEqual(answers[1].question_id, "q2")

        # Untrusted question text is wrapped in explicit delimiters in the
        # prompt sent to the model, same mitigation as anthropic_client.
        sent_messages = fake_sdk_client.chat.completions.calls[0]["messages"]
        user_content = sent_messages[-1]["content"]
        self.assertIn('<question id="q1">', user_content)
        self.assertIn("Years of Python experience?", user_content)

    def test_infer_with_no_questions_does_not_call_the_client(self):
        fake_sdk_client = _FakeOpenAISDKClient(_chat_response(self._batch_json([])))
        client = NvidiaAnswerInferenceClient(client=fake_sdk_client)

        answers = client.infer([], RESUME_TEXT, PROFILE)

        self.assertEqual(answers, [])
        self.assertEqual(fake_sdk_client.chat.completions.calls, [])

    def test_infer_strips_a_markdown_code_fence_around_the_json(self):
        raw = self._batch_json(
            [
                {
                    "question_id": "q1",
                    "answer": "6 years",
                    "evidence": ["6 years of Python experience"],
                    "self_reported_confidence": 0.9,
                    "insufficient_evidence": False,
                }
            ]
        )
        fenced = f"```json\n{raw}\n```"
        fake_sdk_client = _FakeOpenAISDKClient(_chat_response(fenced))
        client = NvidiaAnswerInferenceClient(client=fake_sdk_client)
        questions = [Question(id="q1", text="Years of Python experience?")]

        answers = client.infer(questions, RESUME_TEXT, PROFILE)

        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0].answer, "6 years")

    def test_infer_raises_on_empty_content(self):
        fake_sdk_client = _FakeOpenAISDKClient(_chat_response(""))
        client = NvidiaAnswerInferenceClient(client=fake_sdk_client)
        questions = [Question(id="q1", text="Most recent employer?")]

        with self.assertRaises(ValueError):
            client.infer(questions, RESUME_TEXT, PROFILE)

    def test_infer_raises_on_malformed_json(self):
        fake_sdk_client = _FakeOpenAISDKClient(_chat_response("not json at all"))
        client = NvidiaAnswerInferenceClient(client=fake_sdk_client)
        questions = [Question(id="q1", text="Most recent employer?")]

        with self.assertRaises(ValueError):
            client.infer(questions, RESUME_TEXT, PROFILE)

    def test_infer_raises_when_json_does_not_match_schema(self):
        fake_sdk_client = _FakeOpenAISDKClient(
            _chat_response(json.dumps({"unexpected": "shape"}))
        )
        client = NvidiaAnswerInferenceClient(client=fake_sdk_client)
        questions = [Question(id="q1", text="Most recent employer?")]

        with self.assertRaises(ValueError):
            client.infer(questions, RESUME_TEXT, PROFILE)
