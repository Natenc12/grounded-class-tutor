"""Reproducible local retrieval measurements and an explicit claim-review queue.

Run ``python -m gct.eval.local_quality --suite eval/local-quality/suite.json``.
The public suite describes original synthetic PDF pages; reportlab (the existing
dev extra) renders them, and the real bounded parser, chunker and SQLite search
consume those PDFs. No generator, credentials, model, or network is used.

Use ``--questions eval/questions.jsonl --corpus data/dogfood/religion`` for the
existing private, single-class corpus. Reports omit questions and source text by
default. ``--packet`` explicitly includes them for review; keep private packets
outside Git. ``--export-pdfs DIR`` writes only the public synthetic documents so
the desktop app can exercise the identical material.

Retrieval coverage is not answerability or claim support. Those remain unmeasured
until ``--reviews FILE`` supplies a human or identified agent's claim-by-claim review.
Reviews bind to the corpus and exact selected evidence, so changed runs cannot
silently inherit old quality scores. This is a bench, not an accuracy guarantee.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from gct.eval.questions import load_questions
from gct.local.store import Library, LibraryError
from gct.local_proof import MAX_FILE_BYTES, ProofError, _load_document


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    expectation: str
    # One member of EACH group is required; members within a group are alternatives.
    source_groups: list[list[tuple[str, int]]]
    required_facts: list[str]
    forbidden_claims: list[str]
    tags: list[str]


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _list(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _texts(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return [_text(item, label) for item in value]


def load_suite(path: Path) -> tuple[dict, list[Case]]:
    data = _object(json.loads(path.read_text()), "suite")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Expected a version 1 local quality suite")
    documents = data.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("The suite needs synthetic documents")
    pages = {}
    for document in documents:
        document = _object(document, "document")
        filename = _text(document.get("file"), "document file")
        if Path(filename).name != filename or not filename.endswith(".pdf") or filename in pages:
            raise ValueError("Synthetic filenames must be distinct PDF basenames")
        texts = _texts(document.get("pages"), "document pages")
        if not texts:
            raise ValueError("A synthetic document must have pages")
        pages[filename] = len(texts)
    cases = []
    ids = set()
    for row in _list(data.get("cases"), "cases"):
        row = _object(row, "case")
        case_id = _text(row.get("id"), "case id")
        if case_id in ids:
            raise ValueError("Duplicate case id")
        ids.add(case_id)
        expectation = _text(row.get("expectation"), "expectation")
        if expectation not in {"answer", "partial", "refuse"}:
            raise ValueError("Unknown case expectation")
        groups = row.get("source_groups")
        if not isinstance(groups, list) or bool(groups) != (expectation != "refuse"):
            raise ValueError("Answer and partial cases need evidence groups; refusals have none")
        parsed_groups = []
        for group in groups:
            if not isinstance(group, list) or not group:
                raise ValueError("Each evidence group needs at least one possible source")
            parsed = []
            for source in group:
                source = _object(source, "expected source")
                filename = _text(source.get("file"), "expected filename")
                page = source.get("page")
                if (
                    filename not in pages
                    or type(page) is not int
                    or not 1 <= page <= pages[filename]
                ):
                    raise ValueError("Expected source does not exist in the synthetic corpus")
                parsed.append((filename, page))
            parsed_groups.append(parsed)
        cases.append(
            Case(
                case_id,
                _text(row.get("question"), "question"),
                expectation,
                parsed_groups,
                _texts(row.get("required_facts"), "required facts"),
                _texts(row.get("forbidden_claims"), "forbidden claims"),
                _texts(row.get("tags"), "tags"),
            )
        )
    if not cases:
        raise ValueError("The suite needs evaluation cases")
    return data, cases


def render_documents(suite: dict, directory: Path) -> list[Path]:
    """Generate original synthetic PDFs, one fixture page per physical PDF page."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import PageBreak, Preformatted, SimpleDocTemplate

    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    style = getSampleStyleSheet()["Code"]
    style.fontName = "Helvetica"
    style.fontSize = 10
    style.leading = 14
    for document in suite["documents"]:
        path = directory / document["file"]
        if path.exists():
            raise ValueError(f"Synthetic PDF already exists: {path.name}")
        story = []
        for index, text in enumerate(document["pages"]):
            if index:
                story.append(PageBreak())
            story.append(Preformatted(text, style, maxLineLength=85))
        SimpleDocTemplate(str(path), pagesize=letter, invariant=1).build(story)
        paths.append(path)
    return paths


def private_cases(path: Path) -> list[Case]:
    questions = load_questions(path)
    if not questions or len({question.class_slug for question in questions}) != 1:
        raise ValueError("Private evaluation requires one nonempty class suite")
    if len({question.id for question in questions}) != len(questions):
        raise ValueError("Duplicate case id")
    if any(bool(q.expected_sources) != (q.expectation == "answer") for q in questions):
        raise ValueError("Private answer cases need sources; refusal cases must have none")
    return [
        Case(
            question.id,
            question.question,
            question.expectation,
            [[(source.file, source.page_or_slide) for source in question.expected_sources]]
            if question.expected_sources
            else [],
            [question.answer_notes],
            [],
            question.tags,
        )
        for question in questions
    ]


def evaluate(paths: list[Path], cases: list[Case], *, packet: bool = False) -> dict:
    """Use a temporary library; never open, alter, or infer against the user's library."""
    if not paths or len({path.name for path in paths}) != len(paths):
        raise ValueError("The corpus needs files with distinct basenames")
    if not cases or any(not isinstance(case, Case) for case in cases):
        raise ValueError("Evaluation needs nonempty typed cases")
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Duplicate case id")
    for case in cases:
        if (
            case.expectation not in {"answer", "partial", "refuse"}
            or (bool(case.source_groups) != (case.expectation != "refuse"))
            or any(not group for group in case.source_groups)
        ):
            raise ValueError("Every answerable case needs evidence; refusals have none")
    metadata = []
    with tempfile.TemporaryDirectory(prefix="gct-quality-") as temporary:
        with Library(Path(temporary) / "library.sqlite3") as library:
            class_id = library.create_class("Offline quality evaluation")["id"]
            available = set()
            for path in sorted(paths):
                # Read once under the product byte cap; parse exactly those admitted bytes.
                if path.suffix.lower() not in {".pdf", ".pptx"}:
                    raise ValueError("Corpus files must be PDF or PPTX")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                with os.fdopen(os.open(path, flags), "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                        raise ValueError("Corpus files must be regular files of at most 10 MiB")
                    original = source.read(MAX_FILE_BYTES + 1)
                if not 0 < len(original) <= MAX_FILE_BYTES:
                    raise ValueError("Corpus files must be nonempty and at most 10 MiB")
                snapshot = Path(temporary) / path.name
                snapshot.write_bytes(original)
                document = _load_document({"file_path": str(snapshot)})
                library.import_document(
                    class_id, path.name, original, document.units, document.page_count
                )
                available.update((path.name, unit.page_or_slide) for unit in document.units)
                metadata.append({"file": path.name, "sha256": hashlib.sha256(original).hexdigest()})
            for case in cases:
                if any(not available.intersection(group) for group in case.source_groups):
                    raise ValueError(f"Expected evidence missing or unparseable for {case.id}")
            # Source text, not PDF creation timestamps, owns the reproducible corpus identity.
            corpus = library.connection.execute(
                "SELECT d.filename, p.page_or_slide, p.text FROM pages p "
                "JOIN documents d ON d.id = p.document_id "
                "ORDER BY d.filename, p.page_or_slide"
            ).fetchall()
            corpus_digest = _digest([tuple(row) for row in corpus])
            rows = []
            for case in cases:
                chunks = library.retrieve(class_id, case.question)
                sources = [
                    {
                        "label": f"S{index}",
                        "file": chunk.file,
                        "page": chunk.page_or_slide,
                        "text": chunk.text,
                    }
                    for index, chunk in enumerate(chunks, 1)
                ]
                ranks = [
                    next(
                        (
                            index
                            for index, chunk in enumerate(chunks, 1)
                            if (chunk.file, chunk.page_or_slide) in group
                        ),
                        None,
                    )
                    for group in case.source_groups
                ]
                row = {
                    "id": case.id,
                    "expectation": case.expectation,
                    "tags": case.tags,
                    "candidate_count": len(chunks),
                    "required_group_ranks": ranks,
                    "evidence_digest": _digest({"question": case.question, "sources": sources}),
                    "retrieved": [
                        {key: value for key, value in source.items() if key != "text"}
                        for source in sources
                    ],
                }
                if packet:
                    row.update(
                        question=case.question,
                        sources=sources,
                        required_facts=case.required_facts,
                        forbidden_claims=case.forbidden_claims,
                    )
                rows.append(row)
    answerable = [row for row in rows if row["required_group_ranks"]]
    unsupported = [row for row in rows if row["expectation"] == "refuse"]
    by_k = {}
    for k in (1, 3, 5, 8):
        by_k[str(k)] = {
            "any_required_source": sum(
                any(rank is not None and rank <= k for rank in row["required_group_ranks"])
                for row in answerable
            ),
            "all_required_groups": sum(
                all(rank is not None and rank <= k for rank in row["required_group_ranks"])
                for row in answerable
            ),
            "answerable_cases": len(answerable),
            "required_groups_found": sum(
                rank is not None and rank <= k
                for row in answerable
                for rank in row["required_group_ranks"]
            ),
            "required_groups_total": sum(len(row["required_group_ranks"]) for row in answerable),
        }
    return {
        "version": 1,
        "mode": "offline_retrieval_no_model_calls",
        "corpus_digest": corpus_digest,
        "case_digest": _digest([case.__dict__ for case in cases]),
        "corpus_files": metadata,
        "retrieval": {
            "by_k": by_k,
            "unsupported_cases": len(unsupported),
            "unsupported_with_candidates": sum(row["candidate_count"] > 0 for row in unsupported),
        },
        "answerability": {"reviewed": 0, "correct": 0, "rate": None},
        "claim_support": {
            "reviewed": 0,
            "supported": 0,
            "unsupported": 0,
            "contradicted": 0,
            "rate": None,
        },
        "limitations": [
            "Source retrieval does not establish answerability or factual support.",
            "Candidates for an unsupported question are not a retrieval error or a model refusal.",
            "Answerability and claim support require explicit human or agent review of responses.",
        ],
        "cases": rows,
    }


def apply_reviews(report: dict, reviews: dict, *, packet: bool = False) -> None:
    """Aggregate identified reviews atomically; never infer entailment from citation syntax."""
    reviews = _object(reviews, "reviews")
    for key in ("corpus_digest", "case_digest"):
        if reviews.get(key) != report[key]:
            raise ValueError("Reviews belong to a different corpus or case suite")
    rows = {row["id"]: row for row in report["cases"]}
    seen = set()
    judgments = []
    answerability = {"reviewed": 0, "correct": 0, "rate": None}
    support = {"reviewed": 0, "supported": 0, "unsupported": 0, "contradicted": 0, "rate": None}
    for review in _list(reviews.get("reviews"), "reviews"):
        review = _object(review, "review")
        case_id = _text(review.get("id"), "reviewed case id")
        if case_id in seen or case_id not in rows:
            raise ValueError("Unknown or duplicate reviewed case")
        seen.add(case_id)
        row = rows[case_id]
        if review.get("evidence_digest") != row["evidence_digest"]:
            raise ValueError("Review evidence differs from this retrieval run")
        reviewers = _list(review.get("reviewers"), "reviewers")
        if not reviewers:
            raise ValueError("Identify at least one human or agent reviewer")
        for reviewer in reviewers:
            reviewer = _object(reviewer, "reviewer")
            if _text(reviewer.get("kind"), "reviewer kind") not in {"human", "agent"}:
                raise ValueError("Reviewer kind must be human or agent")
            _text(reviewer.get("name"), "reviewer name")
        if _text(review.get("response_origin"), "response origin") != "model":
            raise ValueError("Mock answers are contract tests, not model-quality evidence")
        _text(review.get("model"), "recorded model")
        state = _text(review.get("state"), "answer state")
        if state not in {"GROUNDED", "PARTIAL", "REFUSAL", "INTEGRITY_FLAGGED"}:
            raise ValueError("Only completed answers/refusals can receive a quality review")
        _text(review.get("answer"), "reviewed answer or explicit refusal")
        if _text(review.get("answerability"), "answerability") not in {"correct", "incorrect"}:
            raise ValueError("A review must explicitly judge expected answerability")
        if review.get("all_claims_reviewed") is not True:
            raise ValueError("Incomplete claim review cannot enter a quality rate")
        claims = review.get("claims")
        if not isinstance(claims, list):
            raise ValueError("A claim review must list the asserted claims, or none for a refusal")
        if bool(claims) != (state != "REFUSAL"):
            raise ValueError("Factual answers require claims; refusals assert none")
        for claim in claims:
            claim = _object(claim, "claim")
            _text(claim.get("text"), "claim")
            _text(claim.get("reason"), "claim review rationale")
            verdict = _text(claim.get("verdict"), "claim verdict")
            if verdict not in {"supported", "unsupported", "contradicted"}:
                raise ValueError("Unknown claim-support verdict")
            support[verdict] += 1
            support["reviewed"] += 1
        answerability["reviewed"] += 1
        answerability["correct"] += review["answerability"] == "correct"
        judgments.append((row, review))
    for summary, numerator in ((answerability, "correct"), (support, "supported")):
        summary["rate"] = summary[numerator] / summary["reviewed"] if summary["reviewed"] else None
    report["answerability"], report["claim_support"] = answerability, support
    for row in rows.values():
        row.pop("review", None)
    for row, review in judgments:
        row["review"] = (
            review
            if packet
            else {
                "reviewers": [
                    {"kind": reviewer["kind"], "name": reviewer["name"]}
                    for reviewer in review["reviewers"]
                ],
                "model": review["model"],
                "state": review["state"],
                "answerability": review["answerability"],
                "claims_reviewed": len(review["claims"]),
            }
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--suite", type=Path)
    source.add_argument("--questions", type=Path)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--packet", action="store_true")
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--export-pdfs", type=Path)
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="gct-quality-pdfs-") as temporary:
            if args.suite:
                if args.corpus:
                    raise ValueError("--corpus applies only to --questions")
                suite, cases = load_suite(args.suite)
                paths = render_documents(suite, args.export_pdfs or Path(temporary))
            else:
                if args.corpus is None or args.export_pdfs:
                    raise ValueError(
                        "--questions requires --corpus and cannot export synthetic PDFs"
                    )
                cases = private_cases(args.questions)
                paths = sorted(
                    path
                    for path in args.corpus.iterdir()
                    if path.is_file() and path.suffix.lower() in {".pdf", ".pptx"}
                )
            report = evaluate(paths, cases, packet=args.packet)
        if args.reviews:
            apply_reviews(report, json.loads(args.reviews.read_text()), packet=args.packet)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, ProofError, LibraryError) as exc:
        parser.exit(2, f"Quality evaluation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
