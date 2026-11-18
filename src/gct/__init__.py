"""Grounded Class Tutor — the callable RAG core (ADR 0009).

The API, the eval harness, and the spike scripts are all thin *peer callers* of this
library; none owns logic. Slice 0 lays the foundation: config, db access, and the
swappable provider interfaces.
"""
