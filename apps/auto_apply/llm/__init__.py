"""Pluggable LLM answer-inference service for auto-apply drafts (U4).

``base.py`` defines the vendor-agnostic ``AnswerInferenceClient`` protocol,
the provider registry, and ``resolve_answers`` -- the deterministic
groundedness/confidence gate that decides whether an LLM-inferred answer can
be trusted or must be flagged for human review. ``categories.py`` is the
rule-based classifier that keeps sensitive questions out of LLM inference
entirely. ``anthropic_client.py`` is the only registered provider in this
slice.
"""
