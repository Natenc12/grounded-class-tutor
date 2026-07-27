"""Load `eval/questions.jsonl` - the version-controlled eval artifact (ADR 0021) - into typed
records the runner and the V3 harness both read (issue #8).

The file is the durable asset; this module is just the reader. ADR 0021 §5 is explicit that the
file "grows in question count and suite coverage; its shape never changes", and that V1 and V3
read the IDENTICAL file. So this loader is written for that longevity in two opposite directions
at once, and both are deliberate:

  - **Strict about the PRESENCE of the nine fields ADR 0021 §3 names** - and about the TYPE only
    where a wrong type would change a verdict (`expectation`, `expected_sources`, `suites`,
    `tags`). The five free-text scalars are coerced with `str()`, not type-checked: `"id": 42`
    loads as `"42"`, which is harmless because nothing branches on their content. A missing or
    misspelled key is a hard ValueError, not a default. Defaulting is the invisible failure:
    `suites` typo'd as `suite`
    would silently become `[]`, the smoke filter would drop that question, and the run would
    report a clean rate over a suite that quietly shrank. A benchmark that loses questions
    without saying so stops being a benchmark - the whole point of ADR 0021 is comparability
    across the life of the project.
  - **Tolerant of fields it does not know.** Unknown keys are ignored, because V3 adds structure
    (ADR 0021 §2/§5) and the V1 loader must not go red the day someone lands a V3 field in the
    same file. Additive change is the file's designed growth path.

What this module deliberately does NOT do: judge the CONTENT. It does not check that a `refuse`
row has empty `expected_sources`, nor that a cited file exists in any corpus. Those are facts
about a corpus, and this loader has no corpus - conflating "the file is malformed" with "the
corpus moved" would make an unreadable error message out of a routine re-ingest. Scoring
(`gct.eval.scoring`, ADR 0023) is the other half and lives next door.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# The two legal `expectation` values (ADR 0021 §3). Exported because `gct.eval.scoring` keys its
# state->outcome table on exactly these strings: one spelling, two readers, no drift.
EXPECTATION_ANSWER = "answer"
EXPECTATION_REFUSE = "refuse"
EXPECTATIONS = frozenset({EXPECTATION_ANSWER, EXPECTATION_REFUSE})

# Every key ADR 0021 §3 names. Presence is required; see the module docstring for why absence is
# an error rather than a default.
_REQUIRED_KEYS = (
    "id",
    "question",
    "class",
    "expectation",
    "expected_sources",
    "answer_notes",
    "suites",
    "tags",
    "added",
)


@dataclass(frozen=True)
class ExpectedSource:
    """One `(file, page_or_slide)` the answer is expected to rest on (ADR 0021 §3).

    Field names and types match `RetrievedChunk`'s provenance pair EXACTLY, and that is not a
    coincidence: `scoring.retrieval_hit` compares the two by equality on this pair, so a drift in
    either name or type would turn every retrieval check into a silent miss rather than an error.
    `page_or_slide` is a scalar int for the same never-span reason the chunk's is (ADR 0019).
    """

    file: str
    page_or_slide: int


@dataclass(frozen=True)
class EvalQuestion:
    """One row of `eval/questions.jsonl`, typed.

    `class_slug` carries the JSON key `"class"`, renamed because `class` is a Python keyword and
    cannot be an attribute name. The RENAME IS HERE, once, at the file boundary - so nothing
    downstream has to remember the file spells it differently.

    `expectation`, `expected_sources`, and `answer_notes` map 1:1 onto the N1-N4 bars (ADR 0021
    §3), which is why they are carried verbatim rather than pre-digested: V3 computes different
    metrics from the same fields, and any interpretation baked in here would be V1's opinion
    frozen into the loader.
    """

    id: str
    question: str
    class_slug: str
    expectation: str
    expected_sources: list[ExpectedSource]
    answer_notes: str
    suites: list[str]
    tags: list[str]
    added: str


def _fail(path: Path, lineno: int, qid: object, problem: str) -> ValueError:
    """Build the one error shape this module raises: file, line, question id, problem.

    All three locators, always. The file is hand-edited, so the reader needs the LINE to open it
    at; the id is what they will search for and what the run report names; and on a bad-JSON line
    the id may be unknowable, which `<unknown>` says out loud rather than omitting the field and
    leaving the message shaped differently from every other one.
    """
    label = qid if isinstance(qid, str) and qid else "<unknown>"
    return ValueError(f"{path}:{lineno}: question {label!r}: {problem}")


def _parse_expected_sources(
    raw: object, path: Path, lineno: int, qid: object
) -> list[ExpectedSource]:
    """`expected_sources` -> `list[ExpectedSource]`, or raise.

    `page_or_slide` is required to be a real int, and `bool` is rejected explicitly: `True` is an
    `int` in Python, so a JSON `true` here would otherwise land as page 1 and match a chunk. A
    provenance value that quietly means "page 1" is exactly the kind of citation error the whole
    spine exists to prevent.
    """
    if not isinstance(raw, list):
        raise _fail(
            path, lineno, qid, f"'expected_sources' must be a list, got {type(raw).__name__}"
        )

    sources: list[ExpectedSource] = []
    for position, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise _fail(path, lineno, qid, f"expected_sources[{position}] must be an object")
        if "file" not in entry or "page_or_slide" not in entry:
            raise _fail(
                path,
                lineno,
                qid,
                f"expected_sources[{position}] needs both 'file' and 'page_or_slide'",
            )
        page = entry["page_or_slide"]
        if isinstance(page, bool) or not isinstance(page, int):
            raise _fail(
                path,
                lineno,
                qid,
                f"expected_sources[{position}].page_or_slide must be an int, got {page!r}",
            )
        sources.append(ExpectedSource(file=str(entry["file"]), page_or_slide=page))
    return sources


def _parse_str_list(raw: object, field: str, path: Path, lineno: int, qid: object) -> list[str]:
    """A list-of-strings field (`suites`, `tags`), or raise.

    A bare string is rejected rather than wrapped into a one-element list. `"suites": "smoke"`
    would then work by accident, and set-membership is the ADR 0021 §4 decision the array shape
    exists to protect - accepting the scalar form would let the file drift back toward the scalar
    `tier` that ADR explicitly rejected, one row at a time, with nothing complaining.
    """
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise _fail(path, lineno, qid, f"'{field}' must be a list of strings, got {raw!r}")
    return list(raw)


def load_questions(path: str | Path, *, suite: str | None = None) -> list[EvalQuestion]:
    """Read a JSONL eval file into `EvalQuestion`s, in file order.

    `suite` filters by SET MEMBERSHIP in the row's `suites` array - "does `suites` contain X"
    (ADR 0021 §4), never equality against a scalar. `suite=None` returns every row. An unknown
    suite name returns `[]` rather than raising: a suite is not declared anywhere in the file, so
    "no such suite" and "a real suite with no members yet" are genuinely the same observation and
    this loader cannot honestly tell them apart.

    Blank lines are skipped (a hand-edited file collects them); every other line must parse and
    must carry all nine ADR 0021 §3 fields. Every failure raises `ValueError` naming the file,
    the LINE NUMBER, and the question id - see `_fail`.

    Line numbers are 1-based and count blank lines, i.e. they are the numbers an editor shows,
    not an index into the surviving rows. That is the point: the error exists to be opened.
    """
    path = Path(path)
    questions: list[EvalQuestion] = []

    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue

        try:
            row = json.loads(line)
        except json.JSONDecodeError as err:
            raise _fail(path, lineno, None, f"not valid JSON: {err}") from err

        if not isinstance(row, dict):
            raise _fail(path, lineno, None, f"must be a JSON object, got {type(row).__name__}")

        qid = row.get("id")
        missing = [key for key in _REQUIRED_KEYS if key not in row]
        if missing:
            raise _fail(path, lineno, qid, f"missing required field(s): {', '.join(missing)}")

        expectation = row["expectation"]
        # `isinstance` FIRST, and not for tidiness: `expectation not in EXPECTATIONS` hashes the
        # value against a frozenset, so a JSON array or object here raises `TypeError:
        # unhashable type` - escaping this module's one documented error shape (a `ValueError`
        # naming file, line and id) and, because `ask_smoke._load` catches only
        # `(OSError, ValueError)`, surfacing as a raw traceback instead of `SETUP FAILED`.
        if not isinstance(expectation, str) or expectation not in EXPECTATIONS:
            # The load-bearing validation. `expectation` is the key `scoring.score_state` reads,
            # and an unrecognised value there is unscoreable - caught HERE, at the file, where the
            # error can point at the line to fix, rather than deep in a run that already paid for
            # retrieval and generation.
            raise _fail(
                path,
                lineno,
                qid,
                f"'expectation' must be one of {sorted(EXPECTATIONS)}, got {expectation!r}",
            )

        question = EvalQuestion(
            id=str(row["id"]),
            question=str(row["question"]),
            class_slug=str(row["class"]),
            expectation=expectation,
            expected_sources=_parse_expected_sources(row["expected_sources"], path, lineno, qid),
            answer_notes=str(row["answer_notes"]),
            suites=_parse_str_list(row["suites"], "suites", path, lineno, qid),
            tags=_parse_str_list(row["tags"], "tags", path, lineno, qid),
            added=str(row["added"]),
        )

        # Filter AFTER validating, never before: a malformed row outside the requested suite is
        # still a malformed row, and a loader that only checks what it happens to return would
        # let the file rot everywhere the smoke suite doesn't look.
        if suite is None or suite in question.suites:
            questions.append(question)

    return questions
