"""The swappable model provider layer (ADR 0004 / 0013).

One thin interface — Generation — with grounding logic sitting *above* it, so swapping one
model provider for another never changes product behavior.
"""
