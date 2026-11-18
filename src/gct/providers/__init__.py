"""The swappable model provider layer (ADR 0004 / 0013).

Two thin interfaces — Embeddings and Generation — with grounding logic sitting *above* them,
so swapping OpenAI for another provider never changes product behavior.
"""
