"""Unit tests for the chunk stage (issue #2, ADR 0019 never-span contract).

Pure function under test - no files, no DB. Fixtures are hand-built `ParsedUnit` lists.
Mirrors the happy-paths / boundaries / provenance shape of test_parse.py.
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


def _unit(n_words: int, file: str = "lecture3.pdf", page: int = 1) -> ParsedUnit:
    """Build a ParsedUnit whose text is `n_words` distinct, order-checkable words (w0 w1 ...)."""
    return ParsedUnit(text=" ".join(f"w{i}" for i in range(n_words)), file=file, page_or_slide=page)


class TestWordWindows:
    """The isolated windowing math - stride = size - overlap, tail covered, no drops."""

    def test_empty_input_yields_no_windows(self):
        """n_words == 0 -> []."""
        assert _word_windows(0, 250, 40) == []

    def test_shorter_than_size_is_one_window(self):
        """0 < n_words <= size -> exactly one window (0, n_words)."""
        assert _word_windows(100, 250, 40) == [(0, 100)]

    def test_exactly_size_is_one_window(self):
        """n_words == size -> one window, end lands exactly on size (no spurious remainder)."""
        assert _word_windows(250, 250, 40) == [(0, 250)]

    def test_consecutive_windows_overlap_by_overlap_words(self):
        """stride == size - overlap; each start advances by stride, neighbors share `overlap`."""
        windows = _word_windows(600, 250, 40)

        assert windows == [(0, 250), (210, 460), (420, 600)]
        stride = 250 - 40
        starts = [start for start, _ in windows]
        assert all(b - a == stride for a, b in zip(starts, starts[1:]))
        # full-size neighbors physically share `overlap` words
        assert windows[0][1] - windows[1][0] == 40

    def test_final_window_covers_the_tail(self):
        """Last window's end == n_words - no trailing words dropped, even for a short tail."""
        windows = _word_windows(470, 250, 40)

        assert windows[-1] == (420, 470)
        assert windows[-1][1] == 470

    def test_no_redundant_tail_window(self):
        """When the tail is already covered, don't emit a duplicate window fully inside the last."""
        # n=460 with stride 210: a naive range() would add a bogus (420, 460); the break must not.
        assert _word_windows(460, 250, 40) == [(0, 250), (210, 460)]

    def test_rejects_overlap_not_less_than_size(self):
        """Self-guard: overlap >= size means stride <= 0 -> would loop forever. Assert, don't hang."""
        with pytest.raises(AssertionError):
            _word_windows(100, 50, 50)


class TestChunkHappyPath:
    def test_long_unit_splits_into_multiple_chunks(self):
        """A unit longer than CHUNK_SIZE_WORDS produces > 1 TextChunk."""
        chunks = chunk_units([_unit(CHUNK_SIZE_WORDS * 2)])

        assert len(chunks) > 1

    def test_chunks_are_frozen_text_chunks(self):
        """Output items are TextChunk (not ParsedUnit); the type is the has-been-chunked signal."""
        [chunk] = chunk_units([_unit(10)])

        assert isinstance(chunk, TextChunk)
        with pytest.raises(AttributeError):
            chunk.text = "mutated"  # frozen - provenance can't drift post-chunk

    def test_chunk_text_is_flattened_join(self):
        """Chunk text is a single-spaced join of its window words (decision 4, flattening)."""
        unit = ParsedUnit(text="alpha\nbeta\n\ngamma", file="a.pdf", page_or_slide=1)

        [chunk] = chunk_units([unit])

        assert chunk.text == "alpha beta gamma"

    def test_multiple_units_grouped_in_order(self):
        """Chunks come out grouped per source unit, preserving unit order."""
        units = [_unit(CHUNK_SIZE_WORDS * 2, page=1), _unit(10, page=2)]

        pages = [chunk.page_or_slide for chunk in chunk_units(units)]

        # all page-1 chunks precede all page-2 chunks (grouped, in unit order)
        assert pages == sorted(pages)
        assert set(pages) == {1, 2}


class TestBoundaries:
    def test_unit_shorter_than_size_yields_one_chunk(self):
        """A short unit (< CHUNK_SIZE_WORDS) -> exactly one chunk holding the whole unit."""
        chunks = chunk_units([_unit(CHUNK_SIZE_WORDS - 1)])

        assert len(chunks) == 1
        assert chunks[0].text.split() == [f"w{i}" for i in range(CHUNK_SIZE_WORDS - 1)]

    def test_unit_exactly_size_yields_one_chunk(self):
        """n_words == CHUNK_SIZE_WORDS -> exactly one chunk, no spurious overlap remainder."""
        assert len(chunk_units([_unit(CHUNK_SIZE_WORDS)])) == 1

    def test_empty_unit_list_yields_no_chunks(self):
        """chunk_units([]) -> []."""
        assert chunk_units([]) == []


class TestNeverSpanProvenance:
    """The honesty guarantee - provenance carried, never mixed across units (ADR 0019)."""

    def test_every_chunk_carries_source_provenance(self):
        """Each chunk's (file, page_or_slide) equals its source unit's."""
        unit = _unit(CHUNK_SIZE_WORDS * 3, file="deck.pptx", page=7)

        chunks = chunk_units([unit])

        assert chunks  # a real-text unit always yields >= 1 chunk
        assert all(c.file == "deck.pptx" and c.page_or_slide == 7 for c in chunks)

    def test_no_chunk_mixes_two_units(self):
        """Given two units on different pages, no chunk's text contains words from both."""
        a = ParsedUnit(text=" ".join(f"a{i}" for i in range(CHUNK_SIZE_WORDS * 2)),
                       file="doc.pdf", page_or_slide=1)
        b = ParsedUnit(text=" ".join(f"b{i}" for i in range(CHUNK_SIZE_WORDS * 2)),
                       file="doc.pdf", page_or_slide=2)

        for chunk in chunk_units([a, b]):
            prefixes = {word[0] for word in chunk.text.split()}
            assert prefixes in ({"a"}, {"b"})  # never both -> text never crossed a unit boundary

    def test_page_or_slide_stays_scalar_int(self):
        """page_or_slide is an int on every chunk (never a range) - never-span."""
        chunks = chunk_units([_unit(CHUNK_SIZE_WORDS * 2, page=3)])

        assert all(isinstance(c.page_or_slide, int) for c in chunks)
