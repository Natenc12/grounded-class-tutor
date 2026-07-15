"""Unit tests for the chunk stage (issue #2, ADR 0019 never-span contract).

Pure function under test - no files, no DB. Fixtures are hand-built `ParsedUnit` lists.
Mirrors the happy-paths / boundaries / provenance shape of test_parse.py.

Stubs skip (not pass) until implemented - fill in the assertions per WIP-chunk.md
"Edge cases to handle".
"""
from __future__ import annotations

import pytest

from gct.ingest.chunk import (
    CHUNK_OVERLAP_WORDS,
    CHUNK_SIZE_WORDS,
    TextChunk,
    _word_windows,
    chunk_units,
)
from gct.ingest.parse import ParsedUnit

_TODO = "TODO(nate): implement"


def _unit(n_words: int, file: str = "lecture3.pdf", page: int = 1) -> ParsedUnit:
    """Build a ParsedUnit whose text is `n_words` distinct, order-checkable words (w0 w1 ...)."""
    return ParsedUnit(text=" ".join(f"w{i}" for i in range(n_words)), file=file, page_or_slide=page)


class TestWordWindows:
    """The isolated windowing math - stride = size - overlap, tail covered, no drops."""

    def test_empty_input_yields_no_windows(self):
        """n_words == 0 -> []."""
        pytest.skip(_TODO)

    def test_shorter_than_size_is_one_window(self):
        """0 < n_words <= size -> exactly one window (0, n_words)."""
        pytest.skip(_TODO)

    def test_consecutive_windows_overlap_by_overlap_words(self):
        """stride == size - overlap; window[i+1].start - window[i].start == stride."""
        pytest.skip(_TODO)

    def test_final_window_covers_the_tail(self):
        """Last window's end == n_words - no trailing words dropped."""
        pytest.skip(_TODO)


class TestChunkHappyPath:
    def test_long_unit_splits_into_multiple_chunks(self):
        """A unit longer than CHUNK_SIZE_WORDS produces > 1 TextChunk."""
        pytest.skip(_TODO)

    def test_chunks_are_frozen_text_chunks(self):
        """Output items are TextChunk (not ParsedUnit); the type is the has-been-chunked signal."""
        pytest.skip(_TODO)

    def test_chunk_text_is_flattened_join(self):
        """Chunk text is a single-spaced join of its window words (decision 4, flattening)."""
        pytest.skip(_TODO)

    def test_multiple_units_grouped_in_order(self):
        """Chunks come out grouped per source unit, preserving unit order."""
        pytest.skip(_TODO)


class TestBoundaries:
    def test_unit_shorter_than_size_yields_one_chunk(self):
        """A short unit (< CHUNK_SIZE_WORDS) -> exactly one chunk holding the whole unit."""
        pytest.skip(_TODO)

    def test_unit_exactly_size_yields_one_chunk(self):
        """n_words == CHUNK_SIZE_WORDS -> exactly one chunk, no spurious overlap remainder."""
        pytest.skip(_TODO)

    def test_empty_unit_list_yields_no_chunks(self):
        """chunk_units([]) -> []."""
        pytest.skip(_TODO)


class TestNeverSpanProvenance:
    """The honesty guarantee - provenance carried, never mixed across units (ADR 0019)."""

    def test_every_chunk_carries_source_provenance(self):
        """Each chunk's (file, page_or_slide) equals its source unit's."""
        pytest.skip(_TODO)

    def test_no_chunk_mixes_two_units(self):
        """Given two units on different pages, no chunk's text contains words from both."""
        pytest.skip(_TODO)

    def test_page_or_slide_stays_scalar_int(self):
        """page_or_slide is an int on every chunk (never a range) - never-span."""
        pytest.skip(_TODO)
