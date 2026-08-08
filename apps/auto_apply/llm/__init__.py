"""Pluggable LLM answer-inference service for auto-apply drafts (U4).

``base.py`` defines the vendor-agnostic ``AnswerInferenceClient`` protocol
and ``resolve_answers`` -- the deterministic groundedness/confidence gate
that decides whether an LLM-inferred answer can be trusted or must be
flagged for human review. ``categories.py`` is the rule-based classifier
that keeps sensitive questions out of LLM inference entirely.
``langchain_client.py`` is the single LangChain-backed
``AnswerInferenceClient`` implementation that drives every registered
provider (Anthropic, OpenAI, Google Gemini, NVIDIA NIM) through its
``_PROVIDER_CONFIGS`` table -- ``base.get_client()`` is the entry point that
resolves a provider name to a configured instance.
"""
