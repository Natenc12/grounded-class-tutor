"""Storage- and provider-independent contracts shared by local and legacy adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievedChunk:
    """One ranked evidence passage with its exact source provenance.

    A chunk belongs to one page or slide, never a range (ADR 0019). Its
    ``chunk_id`` identifies the passage that the Grounder resolves a citation to.

    ``score`` is an adapter-defined ranking value, higher is better within that
    retrieval method. It is not a calibrated probability of relevance or support;
    scores from different methods must not be compared as though they were.
    The legacy cosine adapter retains its normalized [0, 1] similarity semantics
    (ADR 0017, clamped per ADR 0024). Explicit page selection uses a constant score.
    The Grounder does not use this value to decide whether an answer is supported.
    """

    chunk_id: str
    text: str
    file: str
    page_or_slide: int
    score: float
