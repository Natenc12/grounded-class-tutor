"""Run an eval suite end to end: load a text course, ask every question, score the answers (#199).

Two halves, split on purpose (owner's choice at the #199 plan):

  - `score_answers` is pure. It takes the questions and one `AskResult` per question and returns
    per-question results and totals. No storage, no models. Every rule it applies lives in
    `gct.eval.scoring`; this module only assembles them.
  - `run_suite` is the thin "load and ask" half. It saves the course into a fresh temporary
    library through the library's own save, asks every question through the library (#230) with
    the model it is handed, and calls `score_answers`.

The course enters as TEXT, never as PDFs, so a PDF-library upgrade cannot change what the model
is shown. A course directory holds one JSON file per course file:
`{"class": "<slug>", "file": "<name>", "pages": ["page 1 text", ...]}`.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from gct.contracts import AskResult
from gct.eval.questions import EvalQuestion
from gct.eval.scoring import (
    AnswerAttribution,
    AttributionTotals,
    EvalMetrics,
    EvalRecord,
    Outcome,
    StringCheck,
    aggregate_attribution,
    check_strings,
    citation_hit,
    compute_metrics,
    measure_attribution,
    retrieval_hit,
    score_state,
)
from gct.grounder.answer import GrounderState
from gct.ingest.parse import ParsedUnit
from gct.library import Library
from gct.library.ask import ask
from gct.providers.base import Generation
from gct.search import DEFAULT_K


@dataclass(frozen=True)
class CourseFile:
    """One course file as text: its class, its file name, and its pages in order (page 1 first)."""

    class_slug: str
    file: str
    pages: tuple[str, ...]

    def units(self) -> list[ParsedUnit]:
        """The pages as the `ParsedUnit`s a parser would have produced, numbered from 1."""
        return [
            ParsedUnit(text=text, file=self.file, page_or_slide=number)
            for number, text in enumerate(self.pages, start=1)
        ]

    def original(self) -> bytes:
        """The course file's own text, as the bytes the library stores and fingerprints."""
        row = {"class": self.class_slug, "file": self.file, "pages": list(self.pages)}
        return json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")


def load_course(directory: str | Path) -> list[CourseFile]:
    """Read every `*.json` course file under `directory` (any depth), in sorted path order."""
    files: list[CourseFile] = []
    for path in sorted(Path(directory).rglob("*.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        pages = row["pages"]
        if not isinstance(pages, list) or not all(isinstance(p, str) for p in pages):
            raise ValueError(f"{path}: 'pages' must be a list of strings")
        files.append(
            CourseFile(class_slug=str(row["class"]), file=str(row["file"]), pages=tuple(pages))
        )
    if not files:
        raise ValueError(f"{directory}: no course files found")
    return files


@dataclass(frozen=True)
class QuestionResult:
    """One question's scored outcome. Each signal stays separate; `passed` combines two of them."""

    question_id: str
    expectation: str
    state: GrounderState
    outcome: Outcome
    retrieval_hit: bool | None
    citation_hit: bool | None
    strings: StringCheck
    attribution: AnswerAttribution | None

    @property
    def passed(self) -> bool:
        """The state scored PASS and the prose has every required word and no forbidden one."""
        return self.outcome is Outcome.PASS and self.strings.ok


@dataclass(frozen=True)
class SuiteResult:
    """Every question's result plus the totals. Counts, not just rates, so noise stays visible."""

    results: tuple[QuestionResult, ...]
    metrics: EvalMetrics
    attribution: AttributionTotals

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def citation_hits(self) -> tuple[int, int]:
        """(hits, applicable): answers citing an expected page, over questions that have one."""
        applicable = [r.citation_hit for r in self.results if r.citation_hit is not None]
        return sum(applicable), len(applicable)

    @property
    def forbidden_violations(self) -> int:
        """Answers that contained a forbidden word (for example a prompt-injection marker)."""
        return sum(1 for r in self.results if r.strings.forbidden_found)


def score_answers(
    questions: Sequence[EvalQuestion], answers: Mapping[str, AskResult]
) -> SuiteResult:
    """Score one `AskResult` per question, keyed by question id. Pure: no database, no models.

    A question with no answer is an error, not a skip: a suite that silently loses questions
    reports a clean rate over a set that quietly shrank.
    """
    missing = [q.id for q in questions if q.id not in answers]
    if missing:
        raise ValueError(f"no answer for question(s): {', '.join(missing)}")

    results: list[QuestionResult] = []
    records: list[EvalRecord] = []
    for question in questions:
        asked = answers[question.id]
        result = asked.result
        hit = (
            retrieval_hit(question.expected_sources, asked.retrieved)
            if asked.retrieval_ran
            else None
        )
        records.append(EvalRecord(question.expectation, result.state, hit=hit))
        results.append(
            QuestionResult(
                question_id=question.id,
                expectation=question.expectation,
                state=result.state,
                outcome=score_state(result.state, question.expectation),
                retrieval_hit=hit,
                citation_hit=citation_hit(question.expected_sources, result.citations),
                strings=check_strings(result.answer_prose, question.required, question.forbidden),
                attribution=measure_attribution(result.answer_prose),
            )
        )
    return SuiteResult(
        results=tuple(results),
        metrics=compute_metrics(records),
        attribution=aggregate_attribution([r.attribution for r in results]),
    )


def run_suite(
    course: Sequence[CourseFile],
    questions: Sequence[EvalQuestion],
    *,
    generator: Generation,
    k: int = DEFAULT_K,
) -> SuiteResult:
    """Save the course into a fresh temporary library, ask every question there, score it all.

    Keyword scores depend on word counts across the whole library, so the library holds exactly
    `course` and nothing else, and is thrown away afterwards. The same course gives the same
    library, and so the same passages for every question, whatever order the files come in.
    """
    slugs = sorted({f.class_slug for f in course})
    unknown = sorted({q.class_slug for q in questions} - set(slugs))
    if unknown:
        raise ValueError(f"questions name class(es) the course does not have: {', '.join(unknown)}")

    with tempfile.TemporaryDirectory() as folder, Library(Path(folder) / "suite.db") as library:
        class_ids = {slug: library.create_class(slug).id for slug in slugs}
        for course_file in course:
            library.save_document(
                class_ids[course_file.class_slug],
                course_file.file,
                course_file.original(),
                len(course_file.pages),
                course_file.units(),
            )
        answers = {
            q.id: ask(library, class_ids[q.class_slug], q.question, generator=generator, k=k)
            for q in questions
        }
    return score_answers(questions, answers)
