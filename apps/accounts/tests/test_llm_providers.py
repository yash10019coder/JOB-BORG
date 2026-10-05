"""The provider table and chat-model construction shared with auto-apply."""
import ast
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.accounts import llm_providers
from apps.accounts.llm_providers import (
    PROVIDER_CONFIGS,
    ProviderConfig,
    build_chat_model,
    build_structured_model,
)


class ProviderTableTests(SimpleTestCase):
    def test_the_four_providers_keep_their_settings(self):
        self.assertEqual(set(PROVIDER_CONFIGS), {"anthropic", "openai", "google", "nvidia"})
        self.assertEqual(PROVIDER_CONFIGS["google"].max_retries, 1)
        self.assertEqual(PROVIDER_CONFIGS["nvidia"].structured_output_method, "json_mode")
        self.assertEqual(PROVIDER_CONFIGS["nvidia"].init_model, "openai")
        self.assertEqual(PROVIDER_CONFIGS["anthropic"].api_key_setting, "ANTHROPIC_API_KEY")

    def test_the_auto_apply_client_re_exports_the_same_objects(self):
        from apps.auto_apply.llm import langchain_client

        self.assertIs(langchain_client._PROVIDER_CONFIGS, PROVIDER_CONFIGS)
        self.assertIs(langchain_client.ProviderConfig, ProviderConfig)

    def test_the_module_never_imports_upward_or_langchain_at_import_time(self):
        tree = ast.parse(Path(llm_providers.__file__).read_text())
        top_level = [
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (node.names if isinstance(node, ast.Import) else [None])
        ]
        for name in filter(None, top_level):
            self.assertFalse(name.startswith(("apps.auto_apply", "apps.web", "langchain")), name)


class BuildChatModelTests(SimpleTestCase):
    @override_settings(OPENAI_API_KEY="sk-test")
    def test_arguments_come_from_the_provider_config(self):
        init = mock.Mock()
        build_chat_model(PROVIDER_CONFIGS["openai"], timeout=12, init=init)
        init.assert_called_once_with(
            "openai:gpt-5.1", api_key="sk-test", timeout=12, max_retries=2, base_url=None
        )

    @override_settings(NVIDIA_API_KEY="nv-test")
    def test_extra_generation_kwargs_and_base_url_are_forwarded(self):
        init = mock.Mock()
        build_chat_model(PROVIDER_CONFIGS["nvidia"], timeout=5, init=init)
        _, kwargs = init.call_args
        self.assertEqual((kwargs["base_url"], kwargs["max_tokens"], kwargs["temperature"]),
                         ("https://integrate.api.nvidia.com/v1", 1024, 0.2))

    def test_an_explicit_key_and_model_override_the_defaults(self):
        init = mock.Mock()
        build_chat_model(PROVIDER_CONFIGS["anthropic"], api_key="k", model="claude-x", timeout=1, init=init)
        self.assertEqual(init.call_args.args[0], "anthropic:claude-x")
        self.assertEqual(init.call_args.kwargs["api_key"], "k")

    def test_a_timeout_is_mandatory(self):
        with self.assertRaises(TypeError):
            build_chat_model(PROVIDER_CONFIGS["openai"], init=mock.Mock())


class BuildStructuredModelTests(SimpleTestCase):
    @override_settings(OPENAI_API_KEY="sk-test", AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS=30)
    def test_the_schema_and_the_providers_structured_output_method_are_used(self):
        chat = mock.Mock()
        with mock.patch.object(llm_providers, "_default_init", return_value=chat) as init:
            result = build_structured_model("openai", dict)
        self.assertEqual(init.call_args.kwargs["timeout"], 30)
        chat.with_structured_output.assert_called_once_with(dict, method="function_calling")
        self.assertIs(result, chat.with_structured_output.return_value)

    @override_settings(NVIDIA_API_KEY="nv", AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS=30)
    def test_nvidia_uses_json_mode_and_an_explicit_timeout_wins(self):
        chat = mock.Mock()
        with mock.patch.object(llm_providers, "_default_init", return_value=chat) as init:
            build_structured_model("nvidia", dict, timeout=7)
        self.assertEqual(init.call_args.kwargs["timeout"], 7)
        chat.with_structured_output.assert_called_once_with(dict, method="json_mode")

    def test_an_unknown_provider_is_a_key_error(self):
        with self.assertRaises(KeyError):
            build_structured_model("nope", dict)
