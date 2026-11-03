"""Provider configuration and chat-model construction, shared by auto-apply
answering and profile import.

A data-only table, deliberately NOT a registry of classes: adding a provider is a
new ``PROVIDER_CONFIGS`` entry, never a new class or file. It lives here (a leaf
below ``apps.auto_apply``) so that ``apps.accounts`` can use it without importing
upward; ``apps.auto_apply.llm.langchain_client`` re-exports it under its old names.

Nothing here knows about retention or consent: whether a provider may *see a
resume* is decided by the import gate (``PROFILE_IMPORT_LLM_ALLOWED_PROVIDERS``),
not by this table.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings


@dataclass(frozen=True)
class ProviderConfig:
    # The `init_chat_model()` provider prefix, e.g. "anthropic", "openai",
    # "google_genai".
    init_model: str
    default_model: str
    # Name of the Django setting holding this provider's API key.
    api_key_setting: str
    base_url: str | None = None
    # "function_calling" works for every frontier-model provider registered
    # here; kept per-provider because NVIDIA NIM's small open-weight instruct
    # model is not guaranteed to support tool-calling, so it uses "json_mode"
    # (closer to the prompt-instructed raw-JSON generation it replaced).
    structured_output_method: str = "function_calling"
    # Extra generation kwargs forwarded to init_chat_model() (max_tokens,
    # temperature, top_p, ...). Only NVIDIA's small model needs these today.
    model_kwargs: dict = field(default_factory=dict)
    # Preserve Anthropic/OpenAI defaults; Google needs a smaller retry budget.
    max_retries: int = 2


PROVIDER_CONFIGS: dict[str, ProviderConfig] = {
    "anthropic": ProviderConfig(
        init_model="anthropic",
        default_model="claude-sonnet-4-5",
        api_key_setting="ANTHROPIC_API_KEY",
    ),
    "openai": ProviderConfig(
        init_model="openai",
        default_model="gpt-5.1",
        api_key_setting="OPENAI_API_KEY",
    ),
    "google": ProviderConfig(
        init_model="google_genai",
        default_model="gemini-2.5-flash",
        api_key_setting="GOOGLE_API_KEY",
        # langchain-google-genai retries up to 6 attempts by default; with a
        # per-request timeout that can exceed a task's hard time limit and kill
        # it before it persists anything. 1 = a single attempt (0 would fall
        # back to the SDK default).
        max_retries=1,
    ),
    "nvidia": ProviderConfig(
        # NIM exposes an OpenAI-compatible chat-completions endpoint, so the
        # "openai" LangChain integration is the client, pointed at a different
        # base_url.
        init_model="openai",
        default_model="meta/llama-3.2-3b-instruct",
        api_key_setting="NVIDIA_API_KEY",
        base_url="https://integrate.api.nvidia.com/v1",
        structured_output_method="json_mode",
        model_kwargs={"max_tokens": 1024, "temperature": 0.2, "top_p": 0.7},
    ),
}


def _default_init(*args, **kwargs):
    # Imported lazily so importing ``apps.accounts`` never pulls LangChain in.
    from langchain.chat_models import init_chat_model

    return init_chat_model(*args, **kwargs)


def build_chat_model(provider_config, *, api_key=None, model=None, timeout, init=None):
    """A configured LangChain chat model for ``provider_config``.

    ``timeout`` is required: both provider SDKs default to a very long request
    timeout (minutes), and an unbounded call can block a worker slot
    indefinitely. ``init`` lets a caller supply its own ``init_chat_model`` (the
    auto-apply client passes its module-level one so tests can patch it).
    """
    return (init or _default_init)(
        f"{provider_config.init_model}:{model or provider_config.default_model}",
        api_key=api_key or getattr(settings, provider_config.api_key_setting),
        timeout=timeout,
        max_retries=provider_config.max_retries,
        base_url=provider_config.base_url,
        **provider_config.model_kwargs,
    )


def build_structured_model(provider, schema, *, timeout=None, model=None):
    """A chat model for the named provider that returns ``schema`` instances."""
    config = PROVIDER_CONFIGS[provider]
    chat_model = build_chat_model(
        config, model=model, timeout=timeout or settings.AUTO_APPLY_LLM_REQUEST_TIMEOUT_SECONDS
    )
    return chat_model.with_structured_output(schema, method=config.structured_output_method)
