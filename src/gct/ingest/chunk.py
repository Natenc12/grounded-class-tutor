"""Chunk - split parsed page/slide units into smaller, self-contained, embeddable
passages under the never-span contract: citation-spine honor-point ① is preserved
through chunking (design/decisions/0019-chunking-contract-never-span.md).

Pure function: no DB, no job/status/lease state, no scope IDs, no chunk identity
(the PM-4 seam - Slice 2 wraps this in worker/queue machinery without rewriting it).
Scope (`owner_id`/`class_id`/`file_id`), the embedding + `embedding_model_id` stamp
(ADR 0018), and `chunk_id` are all attached by LATER stages, never here.

CONTRACT (locked, ADR 0019 - downstream Retriever/Grounder bind to this):
  1. Every chunk carries `(file, page_or_slide)` provenance - honor-point ①.
  2. Every chunk is self-contained / independently embeddable + citable.
  3. Every chunk maps to EXACTLY ONE page/slide - `page_or_slide` stays a scalar
     int, never a range (never-span).
  4. Pure `list[ParsedUnit] -> list[TextChunk]`.

STRATEGY (spike - tuned against the eval set later, ADR 0019 / 0021): boundary method,
size, overlap, tokenizer tooling. Provisional V1 strategy = fixed-size + overlap over
whitespace-split words. Sizes below are spike-tunable constants, not commitments - and
so is the `size`/`overlap` SIGNATURE that carries them: ADR 0019 leaves the whole
strategy to the spike, so the knobs may change shape (or vanish) when it settles.

Never-span falls out for FREE: we chunk WITHIN each `ParsedUnit` independently and never
merge text across units. A `ParsedUnit` is already exactly one page/slide, so a chunk
physically cannot straddle a boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from gct.ingest.parse import ParsedUnit

# --- Provisional chunking strategy (SPIKE - ADR 0019 / 0021) --------------------------------
# Word-based fixed-size + overlap. Words are a dep-free token proxy; true-token sizing
# (tiktoken) is the spike upgrade that enters at #3 embed-adapter (the real per-request cap).
CHUNK_SIZE_WORDS = 250
CHUNK_OVERLAP_WORDS = 40

# Overlap must leave forward progress, or windowing never advances. Guard at import time.
assert 0 <= CHUNK_OVERLAP_WORDS < CHUNK_SIZE_WORDS, "overlap must be in [0, size)"


@dataclass(frozen=True)
class TextChunk:
    """One sub-page/-slide passage ready to embed.

    Distinct from `ParsedUnit` (a whole page/slide) even though the fields coincide today:
    the type is what tells a caller this text has been chunked and is safe to embed. Carries
    ONLY text + provenance - scope, embedding, and identity are attached downstream (the PM-4
    seam; see module docstring, embedding stamp per ADR 0018). `page_or_slide` is scalar by
    never-span (ADR 0019).
    """

    text: str
    file: str
    page_or_slide: int


def chunk_units(
    units: list[ParsedUnit],
    *,
    size: int = CHUNK_SIZE_WORDS,
    overlap: int = CHUNK_OVERLAP_WORDS,
) -> list[TextChunk]:
    """Chunk every parsed unit into `TextChunk`s, in order, under the never-span contract.

    Units are chunked INDEPENDENTLY - never merging text across them - which is what makes
    never-span hold for free. Chunks come back grouped per source unit, in unit order.

    A unit whose text is shorter than `size` yields exactly ONE chunk (the whole unit). A unit
    with real text always yields >= 1 chunk.

    `size`/`overlap` default to the module constants, so the strategy is unchanged unless a
    caller overrides it. Both the values AND this signature are provisional spike parameters
    (ADR 0019, SPIKE half - see module docstring).
    """
    # Validated here, at the public entry, because the failure mode is a HANG rather than a
    # crash: `overlap >= size` makes stride <= 0 and `_word_windows` appends windows forever.
    # Raise (not assert) for the same reason `compose`'s alignment guard in pipeline.py does -
    # this validates CALLER input and must hold under `python -O`, which strips asserts.
    if not (0 <= overlap < size):
        raise ValueError(f"overlap must be in [0, size); got overlap={overlap}, size={size}")

    chunks: list[TextChunk] = []
    for unit in units:
        chunks.extend(_chunk_one(unit, size, overlap))
    return chunks


def _chunk_one(unit: ParsedUnit, size: int, overlap: int) -> list[TextChunk]:
    """Chunk a single unit's text into `TextChunk`s carrying that unit's provenance.

    The whitespace split + join FLATTENS newline structure. That is part of the provisional
    whitespace-word strategy, not an oversight (see module docstring, ADR 0019 / 0021).
    """
    words = unit.text.split()
    windows = _word_windows(len(words), size, overlap)
    return [
        TextChunk(
            text=" ".join(words[start:end]),
            file=unit.file,
            page_or_slide=unit.page_or_slide,
        )
        for start, end in windows
    ]


def _word_windows(n_words: int, size: int, overlap: int) -> list[tuple[int, int]]:
    """Return `(start, end)` index pairs (end exclusive) tiling `n_words` words into
    size-`size` windows that step by `stride = size - overlap`.

    The fiddly bit - isolated here so it's unit-testable on its own. Requirements:
      - Consecutive windows overlap by `overlap` words (stride = size - overlap).
      - The final window covers the tail - no trailing words dropped.
      - `n_words == 0` -> `[]`; `0 < n_words <= size` -> exactly one window `(0, n_words)`.
      - Never emit an empty window; never advance by 0 (see the assert below).

    Since `chunk_units` became parameterized there are THREE guards on `overlap < size`, at
    different reaches, and naming only one of them here would name the wrong one for most
    callers: the module-level assert covers the DEFAULTS at import, `chunk_units` raises on
    CALLER-supplied values, and the assert below is this function's own self-guard for the
    direct-call path the tests use. The assert is the only one that reaches every caller of
    THIS function, which is why it stays even though `chunk_units` already raised.
    """
    assert 0 <= overlap < size, "overlap must be in [0, size)"  # stride > 0, no infinite loop
    if n_words == 0:
        return []

    windows = []
    start = 0
    stride = size - overlap

    while True:
        end = min(start + size, n_words)
        windows.append((start, end))
        if end == n_words:
            break
        start += stride

    return windows
