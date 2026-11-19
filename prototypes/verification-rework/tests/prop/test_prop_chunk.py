"""PROTOTYPE property tests for the chunker (public `chunk_units` only)."""
import pytest
from hypothesis import given, strategies as st

from gct.ingest.chunk import chunk_units
from gct.ingest.parse import ParsedUnit

units = st.lists(st.builds(ParsedUnit, text=st.lists(st.sampled_from(["a", "b", "c", "dd"]), max_size=60).map(" ".join),
                           file=st.sampled_from(["x.pdf", "y.pptx"]), page_or_slide=st.integers(1, 9)), max_size=4)
sizes = st.integers(1, 12).flatmap(lambda size: st.tuples(st.just(size), st.integers(0, size - 1)))


@given(units, sizes)
def test_no_word_is_lost_duplicated_or_moved_to_another_page(units, size_overlap):
    size, overlap = size_overlap
    chunks = chunk_units(units, size=size, overlap=overlap)
    rebuilt = []  # per unit: glue the chunks back together, dropping each chunk's overlap prefix
    it = iter(chunks)
    for unit in units:
        words, got, n = unit.text.split(), [], 0
        while len(got) < len(words):
            chunk = next(it)
            assert (chunk.file, chunk.page_or_slide) == (unit.file, unit.page_or_slide)
            piece = chunk.text.split()
            assert 0 < len(piece) <= size
            got += piece if n == 0 else piece[overlap:]
            assert n == 0 or piece[:overlap] == words[len(got) - len(piece):][:overlap]
            n += 1
        rebuilt.append(got)
        assert got == words
    assert next(it, None) is None  # and nothing extra


@given(units, st.integers(-3, 12), st.integers(-3, 12))
def test_a_window_that_cannot_advance_is_refused_never_looped(units, size, overlap):
    if 0 <= overlap < size:
        chunk_units(units, size=size, overlap=overlap)
    else:
        with pytest.raises(ValueError):
            chunk_units(units, size=size, overlap=overlap)
