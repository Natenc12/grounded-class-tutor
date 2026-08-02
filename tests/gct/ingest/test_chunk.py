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
        # strict=False on purpose: this zips a list against its own tail to walk consecutive
        # pairs, so the operands differ in length by one BY CONSTRUCTION.
        assert all(b - a == stride for a, b in zip(starts, starts[1:], strict=False))
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
        """Self-guard: overlap >= size means stride <= 0 -> would loop forever.

        Assert, don't hang.
        """
        with pytest.raises(AssertionError):
            _word_windows(100, 50, 50)


class TestWordWindowsAtNonDefaultSizes:
    """The same math at windows the spike may actually retune to (ADR 0019 / 0021).

    Every other `_word_windows` case above happens to run at 250/40 — the current defaults — so a
    stride bug that only bites at some *other* (size, overlap) pair would be invisible. These pin
    the tiling at two candidate windows the spike is likely to try, on a real page's word count.
    """

    # 835 words is the real word count of `Livingston Cosmogony.pdf` page 4 in the dogfood corpus,
    # not a round number picked to make the arithmetic land: a tuning window has to behave on the
    # ragged tails real pages produce, and 835 divides evenly by neither stride below.
    REAL_PAGE_WORDS = 835

    def test_tiles_a_real_page_at_150_25(self):
        """stride 125 over 835 words -> 7 windows, the last one a short tail (750, 835)."""
        windows = _word_windows(self.REAL_PAGE_WORDS, 150, 25)

        assert windows == [
            (0, 150),
            (125, 275),
            (250, 400),
            (375, 525),
            (500, 650),
            (625, 775),
            (750, 835),
        ]
        assert len(windows) == 7
        assert windows[-1][1] == self.REAL_PAGE_WORDS  # tail covered, nothing dropped

    def test_tiles_a_real_page_at_500_80(self):
        """stride 420 over 835 words -> 2 windows; the second is the whole remainder, not a
        full-size window, because 835 < 2 * 500 - 80."""
        windows = _word_windows(self.REAL_PAGE_WORDS, 500, 80)

        assert windows == [(0, 500), (420, 835)]
        assert windows[0][1] - windows[1][0] == 80  # neighbors share exactly `overlap` words


class TestNonDefaultWindow:
    """`chunk_units` must actually USE the size/overlap it is handed.

    A pass-through parameter that is accepted and then silently ignored is the failure mode with no
    symptom: callers tune the window, nothing changes, and the spike concludes the window doesn't
    matter (ADR 0019 / 0021). Comparing two windows against each other — rather than against a
    hand-written count — is what makes that visible.
    """

    def test_two_windows_over_one_unit_yield_different_counts(self):
        """The SAME unit chunked at two windows produces two different chunk counts, and the
        smaller window produces more chunks. Both also differ from the default, so a silent
        fallback to `CHUNK_SIZE_WORDS`/`CHUNK_OVERLAP_WORDS` cannot pass this."""
        units = [_unit(600)]

        wide = chunk_units(units, size=400, overlap=50)
        narrow = chunk_units(units, size=100, overlap=20)
        default = chunk_units(units)

        assert len(narrow) > len(wide)
        assert len({len(wide), len(narrow), len(default)}) == 3

    def test_smaller_window_still_covers_every_word(self):
        """Retuning the window changes how text is cut up, never what text survives: the first
        chunk's opening word and the last chunk's closing word still bracket the whole unit. A
        window that quietly dropped the tail would keep the count assertions above green."""
        chunks = chunk_units([_unit(600)], size=100, overlap=20)

        assert chunks[0].text.split()[0] == "w0"
        assert chunks[-1].text.split()[-1] == "w599"
        # And no chunk exceeds the window it was asked for — the bracket above holds even for a
        # chunker that ignored `size` entirely, so without this the coverage claim is window-blind.
        assert all(len(chunk.text.split()) <= 100 for chunk in chunks)

    def test_overlap_is_honored_not_just_size(self):
        """At a FIXED size, changing only `overlap` changes the tiling — and adjacent chunks
        physically share exactly `overlap` words. Every other test in this class varies size and
        overlap together, so a `_chunk_one` that forwarded `size` and quietly dropped `overlap`
        would pass all of them; this is the one that dies."""
        units = [_unit(600)]

        assert len(chunk_units(units, size=100, overlap=0)) < len(
            chunk_units(units, size=100, overlap=50)
        )
        chunks = chunk_units(units, size=100, overlap=20)
        assert chunks[0].text.split()[-20:] == chunks[1].text.split()[:20]

    def test_defaults_are_the_module_constants(self):
        """An omitted window must equal an explicit one at CHUNK_SIZE_WORDS/CHUNK_OVERLAP_WORDS —
        the same constant-not-literal pinning the argparse layer already has. Without it, either
        default could drift off its constant and nothing would fail."""
        units = [_unit(600)]

        assert chunk_units(units) == chunk_units(
            units, size=CHUNK_SIZE_WORDS, overlap=CHUNK_OVERLAP_WORDS
        )


class TestWindowValidation:
    """`chunk_units` rejects a caller's bad window with `ValueError`, at the public entry.

    Not an `AssertionError`: `overlap >= size` makes stride <= 0, which HANGS rather than crashes,
    and `python -O` strips asserts — so the guard that faces callers has to be a real raise. The
    inner `_word_windows` self-guard stays an assert on purpose and is pinned separately by
    `TestWordWindows.test_rejects_overlap_not_less_than_size`; these tests exist to keep the two
    from being conflated into one.
    """

    def test_rejects_overlap_equal_to_size(self):
        """overlap == size -> stride 0, the exact infinite-loop boundary."""
        with pytest.raises(ValueError) as excinfo:
            chunk_units([_unit(100)], size=50, overlap=50)

        # The message must name BOTH values: a caller who passed them through from config needs to
        # see which pair was rejected, not just that "a" window was bad.
        assert "overlap=50" in str(excinfo.value)
        assert "size=50" in str(excinfo.value)

    def test_rejects_overlap_greater_than_size(self):
        """overlap > size -> stride negative; windows would march backwards forever."""
        with pytest.raises(ValueError) as excinfo:
            chunk_units([_unit(100)], size=50, overlap=80)

        assert "overlap=80" in str(excinfo.value)
        assert "size=50" in str(excinfo.value)

    def test_rejects_negative_overlap(self):
        """overlap < 0 makes stride EXCEED size, which silently skips words between windows —
        text present in the file that no chunk contains, so it can never be retrieved or cited.
        A hang is loud; this one would just quietly lose corpus, which is why the guard is a
        two-sided range check and not merely `overlap < size`."""
        with pytest.raises(ValueError) as excinfo:
            chunk_units([_unit(100)], size=50, overlap=-10)

        assert "overlap=-10" in str(excinfo.value)

    def test_validation_precedes_any_chunking(self):
        """The bad window is rejected even with NO units to chunk, i.e. the guard is on the
        parameters themselves and not a side effect of hitting `_word_windows`. If it ever moved
        inward, an empty-input call would return `[]` and a caller's broken window would go
        unreported until the first real file arrived."""
        with pytest.raises(ValueError):
            chunk_units([], size=50, overlap=50)


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
        a = ParsedUnit(
            text=" ".join(f"a{i}" for i in range(CHUNK_SIZE_WORDS * 2)),
            file="doc.pdf",
            page_or_slide=1,
        )
        b = ParsedUnit(
            text=" ".join(f"b{i}" for i in range(CHUNK_SIZE_WORDS * 2)),
            file="doc.pdf",
            page_or_slide=2,
        )

        for chunk in chunk_units([a, b]):
            prefixes = {word[0] for word in chunk.text.split()}
            assert prefixes in ({"a"}, {"b"})  # never both -> text never crossed a unit boundary

    def test_never_span_holds_at_an_arbitrary_window(self):
        """Never-span is window-INDEPENDENT, and that is worth pinning precisely because it looks
        like it might not be: a small window fragments each unit maximally, which is exactly when a
        naive chunker that windowed over concatenated text would start bleeding page 1's tail into
        page 2's head. It holds here by construction — units are chunked independently — so this
        test is really a guard on that construction surviving a future rewrite of `_chunk_one`.

        The window below (7/2) is far from the defaults and cuts both units into many pieces; if
        provenance can be mixed at all, this is where it shows.
        """
        a = ParsedUnit(text=" ".join(f"a{i}" for i in range(40)), file="doc.pdf", page_or_slide=1)
        b = ParsedUnit(text=" ".join(f"b{i}" for i in range(40)), file="doc.pdf", page_or_slide=2)

        chunks = chunk_units([a, b], size=7, overlap=2)

        assert len(chunks) > 2  # the window really did fragment the units, not just re-emit them
        for chunk in chunks:
            prefixes = {word[0] for word in chunk.text.split()}
            assert prefixes in ({"a"}, {"b"})  # no chunk mixes words from two units
            # ...and each fragment still carries its OWN source unit's provenance, not the first
            # unit's or the last one's — honor-point ① survives the extra fragmentation.
            assert (chunk.file, chunk.page_or_slide) == ("doc.pdf", 1 if prefixes == {"a"} else 2)

    def test_page_or_slide_stays_scalar_int(self):
        """page_or_slide is an int on every chunk (never a range) - never-span."""
        chunks = chunk_units([_unit(CHUNK_SIZE_WORDS * 2, page=3)])

        assert all(isinstance(c.page_or_slide, int) for c in chunks)
