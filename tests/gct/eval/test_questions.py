"""Tests for the eval-file loader (issue #8) - pure, offline, no DB and no provider.

Two halves, matching the loader's two opposite obligations (ADR 0021 §5):

  - `TestRealFile` - the SHIPPED `eval/questions.jsonl` loads, and loads correctly. This suite
    deliberately reads the real artifact rather than only fixtures: the file is hand-edited
    infrastructure that gates the whole spike slate, so a typo in it is a real defect and the
    only place it can be caught is here.
  - `TestMalformed` / `TestForwardCompatibility` - what must fail loudly, and what must NOT.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gct.eval.questions import EvalQuestion, ExpectedSource, load_questions

# The shipped artifact. tests/gct/eval/ -> repo root is three levels up.
REAL_EVAL_FILE = Path(__file__).resolve().parents[3] / "eval" / "questions.jsonl"

# A minimal well-formed row, used as the base every malformed fixture mutates - so each bad-row
# test differs from a GOOD row in exactly ONE way, and a failure names that one thing.
GOOD_ROW = {
    "id": "q001",
    "question": "What is a cosmogony?",
    "class": "religion-rph202",
    "expectation": "answer",
    "expected_sources": [{"file": "Lecture 18 Cosmogony.pptx", "page_or_slide": 2}],
    "answer_notes": "An account of the emergence of world order.",
    "suites": ["smoke"],
    "tags": ["cosmogony"],
    "added": "2026-07-16",
}


def write_jsonl(tmp_path: Path, *rows: object) -> Path:
    """Write rows to a temp JSONL file. A raw `str` row is written verbatim (for bad JSON)."""
    path = tmp_path / "questions.jsonl"
    lines = [row if isinstance(row, str) else json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestRealFile:
    """The shipped `eval/questions.jsonl` - the artifact ADR 0021 makes version-controlled."""

    def test_loads_the_seed_smoke_suite(self):
        """12 questions, 8 in-corpus + 4 out-of-corpus.

        The split is asserted, not just the total, because it is the shape ADR 0023 scores: the
        two primaries have SEPARATE denominators (8 and 4), so a row silently flipping its
        `expectation` would move both rates while leaving the count at 12.
        """
        questions = load_questions(REAL_EVAL_FILE)

        assert len(questions) == 12
        assert sum(q.expectation == "answer" for q in questions) == 8
        assert sum(q.expectation == "refuse" for q in questions) == 4
        assert all(isinstance(q, EvalQuestion) for q in questions)

    def test_class_key_is_renamed_to_class_slug(self):
        """The file spells it `class`; Python cannot. The rename happens once, at the loader."""
        questions = load_questions(REAL_EVAL_FILE)

        assert {q.class_slug for q in questions} == {"religion-rph202"}

    def test_expected_sources_are_typed_provenance_pairs(self):
        """q001 is the suite's only MULTI-source row - the shape `retrieval_hit` any-matches."""
        q001 = next(q for q in load_questions(REAL_EVAL_FILE) if q.id == "q001")

        assert q001.expected_sources == [
            ExpectedSource(file="Lecture 18 Cosmogony.pptx", page_or_slide=4),
            ExpectedSource(file="Lecture 19 Types of Cosmogony.pptx", page_or_slide=2),
        ]
        # Ints, not strings - `retrieval_hit` compares against `RetrievedChunk.page_or_slide`,
        # which is an int, and `"4" != 4` would make every check a silent miss.
        assert all(isinstance(s.page_or_slide, int) for s in q001.expected_sources)

    def test_refuse_rows_carry_no_expected_sources(self):
        """`expected_sources` is in-corpus only (ADR 0021 §3) - this is what makes the retrieval
        signal return `None` for them rather than a guaranteed miss (ADR 0023 §1)."""
        refusals = [q for q in load_questions(REAL_EVAL_FILE) if q.expectation == "refuse"]

        assert len(refusals) == 4
        assert all(q.expected_sources == [] for q in refusals)

    def test_suite_filter_is_set_membership(self):
        """ADR 0021 §4: subset selection is "does `suites` contain X"."""
        every = load_questions(REAL_EVAL_FILE)
        smoke = load_questions(REAL_EVAL_FILE, suite="smoke")

        assert len(smoke) == 12
        assert [q.id for q in smoke] == [q.id for q in every]

    def test_unknown_suite_returns_empty_not_error(self):
        """A suite is never declared anywhere, so "no such suite" and "an empty suite" are the
        same observation - the loader cannot honestly distinguish them, so it does not try."""
        assert load_questions(REAL_EVAL_FILE, suite="regression") == []

    def test_file_order_is_preserved(self):
        """Report order must be reproducible across runs, or two spike reports can't be diffed."""
        assert [q.id for q in load_questions(REAL_EVAL_FILE)] == [f"q{n:03d}" for n in range(1, 13)]


class TestMalformed:
    """What must fail LOUDLY - and with a message that points at the line to fix."""

    def test_bad_expectation_raises_naming_line_and_id(self, tmp_path):
        """The load-bearing validation: `expectation` is the key ADR 0023 scores on."""
        path = write_jsonl(tmp_path, GOOD_ROW, {**GOOD_ROW, "id": "q002", "expectation": "maybe"})

        with pytest.raises(ValueError) as excinfo:
            load_questions(path)

        message = str(excinfo.value)
        assert "'q002'" in message  # WHICH question
        assert ":2:" in message  # WHICH line - the file is hand-edited; the error must open it
        assert "maybe" in message  # WHAT was wrong

    def test_missing_field_raises_naming_the_field(self, tmp_path):
        """Absent is an error, never a default - a defaulted `suites` would silently drop the row
        from the smoke suite and shrink the benchmark without a word."""
        row = {k: v for k, v in GOOD_ROW.items() if k != "suites"}
        path = write_jsonl(tmp_path, row)

        with pytest.raises(ValueError, match="missing required field"):
            load_questions(path)

    def test_every_required_field_is_actually_required(self, tmp_path):
        """Each of ADR 0021 §3's nine keys, dropped one at a time.

        Parametrising by hand would let a key be added to the dataclass and forgotten in the
        required list; iterating over the good row itself cannot drift from it.
        """
        for key in GOOD_ROW:
            path = write_jsonl(tmp_path, {k: v for k, v in GOOD_ROW.items() if k != key})
            with pytest.raises(ValueError, match=f"missing required field.*{key}"):
                load_questions(path)

    def test_invalid_json_raises_naming_the_line(self, tmp_path):
        """The id is unknowable on an unparseable line, so the message says `<unknown>` rather
        than quietly changing shape."""
        path = write_jsonl(tmp_path, GOOD_ROW, "{not json")

        with pytest.raises(ValueError) as excinfo:
            load_questions(path)

        assert ":2:" in str(excinfo.value)
        assert "<unknown>" in str(excinfo.value)

    def test_expected_source_missing_page_raises(self, tmp_path):
        path = write_jsonl(tmp_path, {**GOOD_ROW, "expected_sources": [{"file": "a.pdf"}]})

        with pytest.raises(ValueError, match="page_or_slide"):
            load_questions(path)

    def test_boolean_page_or_slide_is_rejected(self, tmp_path):
        """`True` IS an `int` in Python, so a JSON `true` would otherwise land as page 1 and
        match a real chunk - a fabricated citation produced by the benchmark itself."""
        path = write_jsonl(
            tmp_path, {**GOOD_ROW, "expected_sources": [{"file": "a.pdf", "page_or_slide": True}]}
        )

        with pytest.raises(ValueError, match="must be an int"):
            load_questions(path)

    def test_scalar_suites_is_rejected(self, tmp_path):
        """ADR 0021 §4 chose an ARRAY over a scalar tier. Accepting `"suites": "smoke"` would let
        the file drift back to the rejected scalar shape one row at a time."""
        path = write_jsonl(tmp_path, {**GOOD_ROW, "suites": "smoke"})

        with pytest.raises(ValueError, match="'suites' must be a list"):
            load_questions(path)

    def test_malformed_row_outside_the_filtered_suite_still_raises(self, tmp_path):
        """Validation happens BEFORE filtering, on purpose: a loader that only checked what it
        returned would let the file rot everywhere the smoke suite doesn't look."""
        bad = {**GOOD_ROW, "id": "q002", "expectation": "nope", "suites": ["regression"]}
        path = write_jsonl(tmp_path, GOOD_ROW, bad)

        with pytest.raises(ValueError, match="q002"):
            load_questions(path, suite="smoke")


class TestForwardCompatibility:
    """What must NOT fail - the file's designed growth path (ADR 0021 §5)."""

    def test_unknown_fields_are_ignored(self, tmp_path):
        """V3 adds structure to the SAME file. A V1 loader that rejected a V3 field would make
        the "one file, two readers" property false the day V3 lands."""
        path = write_jsonl(tmp_path, {**GOOD_ROW, "difficulty": "hard", "n4_gold": ["S1"]})

        loaded = load_questions(path)

        assert len(loaded) == 1
        assert loaded[0].id == "q001"

    def test_blank_lines_are_skipped_but_still_counted_in_line_numbers(self, tmp_path):
        """A hand-edited file collects blank lines. Skipping them must not renumber the others,
        or the error message points at the wrong line in the editor."""
        path = write_jsonl(tmp_path, GOOD_ROW, "", {**GOOD_ROW, "id": "q003", "expectation": "x"})

        with pytest.raises(ValueError) as excinfo:
            load_questions(path)

        assert ":3:" in str(excinfo.value)

    def test_empty_file_loads_as_empty(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("", encoding="utf-8")

        assert load_questions(path) == []

    def test_accepts_a_str_path(self, tmp_path):
        """Scripts pass strings; the runner passes a Path. Both are callers we already have."""
        path = write_jsonl(tmp_path, GOOD_ROW)

        assert len(load_questions(str(path))) == 1
