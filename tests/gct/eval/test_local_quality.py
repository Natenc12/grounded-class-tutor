"""Offline measurements cannot masquerade as model or claim-support accuracy."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from gct.eval import local_quality as quality

SUITE = Path(__file__).resolve().parents[3] / "eval/local-quality/suite.json"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    suite, cases = quality.load_suite(SUITE)
    directory = tmp_path_factory.mktemp("quality-pdfs")
    return quality.render_documents(suite, directory), cases


@pytest.fixture(scope="module")
def measured(corpus):
    return quality.evaluate(*corpus)


def review_payload(report, *, state="GROUNDED"):
    row = report["cases"][0]
    return {
        "corpus_digest": report["corpus_digest"],
        "case_digest": report["case_digest"],
        "reviews": [
            {
                "id": row["id"],
                "evidence_digest": row["evidence_digest"],
                "reviewers": [{"kind": "agent", "name": "Named independent reviewer"}],
                "response_origin": "model",
                "model": "recorded-model-name",
                "state": state,
                "answer": "PRIVATE ANSWER FROM REVIEW",
                "answerability": "correct",
                "all_claims_reviewed": True,
                "claims": []
                if state == "REFUSAL"
                else [
                    {"text": "PRIVATE CLAIM", "verdict": "supported", "reason": "PRIVATE REASON"}
                ],
            }
        ],
    }


def test_real_pdf_retrieval_does_not_claim_answer_or_support_accuracy(measured):
    assert measured["mode"] == "offline_retrieval_no_model_calls"
    assert measured["retrieval"]["by_k"]["8"]["answerable_cases"] == 23
    assert measured["retrieval"]["by_k"]["8"]["required_groups_total"] == 25
    assert measured["retrieval"]["unsupported_cases"] == 5
    assert measured["retrieval"]["unsupported_with_candidates"] == 5
    assert measured["answerability"] == {"reviewed": 0, "correct": 0, "rate": None}
    assert measured["claim_support"]["reviewed"] == 0
    assert measured["claim_support"]["rate"] is None
    # The denominator includes the hard paraphrase miss, not just successfully found pages.
    hard = next(row for row in measured["cases"] if row["id"] == "no-overlap-paraphrase")
    assert len(hard["required_group_ranks"]) == 1
    assert all("question" not in row and "sources" not in row for row in measured["cases"])


def test_any_source_hit_does_not_mask_missing_second_evidence_group(measured):
    # Multi-source full coverage has its own count even when one source is already found.
    top_one = measured["retrieval"]["by_k"]["1"]
    assert top_one["any_required_source"] > top_one["all_required_groups"]
    assert top_one["required_groups_found"] < top_one["required_groups_total"]
    partial = next(row for row in measured["cases"] if row["id"] == "table-partial")
    assert partial["expectation"] == "partial"
    assert partial["required_group_ranks"]


def test_repeated_fresh_runs_have_identical_pdfs_digests_and_evidence(tmp_path, corpus, measured):
    suite, _ = quality.load_suite(SUITE)
    rerendered = quality.render_documents(suite, tmp_path)
    assert [path.read_bytes() for path in corpus[0]] == [path.read_bytes() for path in rerendered]
    assert quality.evaluate(rerendered, corpus[1]) == measured
    # Reviews remain usable after both PDF regeneration and new library UUIDs.
    repeated = quality.evaluate(rerendered, corpus[1])
    quality.apply_reviews(repeated, review_payload(measured))
    assert repeated["answerability"]["reviewed"] == 1


def test_reviews_keep_agent_provenance_and_default_report_omits_private_prose(measured):
    report = copy.deepcopy(measured)
    quality.apply_reviews(report, review_payload(report))
    assert report["claim_support"]["rate"] == 1
    assert report["cases"][0]["review"]["reviewers"][0]["kind"] == "agent"
    assert "PRIVATE" not in json.dumps(report)
    quality.apply_reviews(report, review_payload(report), packet=True)
    assert "PRIVATE ANSWER" in json.dumps(report)


def test_refusal_counts_answerability_without_free_claim_support_credit(measured):
    report = copy.deepcopy(measured)
    quality.apply_reviews(report, review_payload(report, state="REFUSAL"))
    assert report["answerability"]["reviewed"] == 1
    assert report["claim_support"]["reviewed"] == 0
    assert report["claim_support"]["rate"] is None


def test_supported_claim_rate_uses_claim_count_not_answer_count(measured):
    report = copy.deepcopy(measured)
    reviews = review_payload(report)
    reviews["reviews"][0]["claims"].extend(
        [
            {"text": "B", "reason": "Uncovered", "verdict": "unsupported"},
            {"text": "C", "reason": "Opposed", "verdict": "contradicted"},
        ]
    )
    quality.apply_reviews(report, reviews)
    assert report["claim_support"]["rate"] == 1 / 3
    assert report["claim_support"]["contradicted"] == 1


@pytest.mark.parametrize("key", ["corpus_digest", "case_digest", "evidence_digest"])
def test_reviews_cannot_attach_to_changed_sources_question_or_ranked_evidence(measured, key):
    report = copy.deepcopy(measured)
    reviews = review_payload(report)
    target = reviews["reviews"][0] if key == "evidence_digest" else reviews
    target[key] = "changed"
    with pytest.raises(ValueError):
        quality.apply_reviews(report, reviews)
    assert report == measured


@pytest.mark.parametrize(
    "mutation",
    [
        lambda review: review.update(claims=[]),
        lambda review: review.update(all_claims_reviewed=False),
        lambda review: review.update(response_origin="mock"),
        lambda review: review.update(state="ERROR"),
        lambda review: review.update(reviewers=[]),
        lambda review: review.update(reviewers=[{"kind": "unknown", "name": "x"}]),
        lambda review: review.update(claims=[{"text": "x", "verdict": "supported"}]),
    ],
)
def test_incomplete_mock_or_unattributed_reviews_do_not_improve_quality(measured, mutation):
    report = copy.deepcopy(measured)
    reviews = review_payload(report)
    mutation(reviews["reviews"][0])
    with pytest.raises(ValueError):
        quality.apply_reviews(report, reviews)
    assert report == measured


def test_invalid_later_review_does_not_leave_partially_mutated_scores(measured):
    report = copy.deepcopy(measured)
    reviews = review_payload(report)
    reviews["reviews"].append(None)
    with pytest.raises(ValueError):
        quality.apply_reviews(report, reviews)
    assert report == measured


@pytest.mark.parametrize(
    "field,value",
    [
        ("documents", [None]),
        ("cases", [False]),
        ("cases", None),
    ],
)
def test_malformed_manifest_rows_fail_cleanly(tmp_path, field, value):
    suite, _ = quality.load_suite(SUITE)
    suite[field] = value
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(suite))
    with pytest.raises(ValueError):
        quality.load_suite(path)


def test_missing_evidence_never_disappears_from_the_denominator(tmp_path, corpus):
    paths, cases = corpus
    missing = quality.Case("missing", "Question", "answer", [], [], [], [])
    with pytest.raises(ValueError):
        quality.evaluate(paths, [missing])
    with pytest.raises(ValueError):
        quality.evaluate(paths, [])
    with pytest.raises(ValueError):
        quality.evaluate(paths, [cases[0], cases[0]])
    absent = quality.Case("absent", "Question", "answer", [[("missing.pdf", 1)]], [], [], [])
    with pytest.raises(ValueError, match="missing or unparseable"):
        quality.evaluate(paths, [absent])


def test_private_question_schema_cannot_hide_an_answer_without_sources(tmp_path):
    row = {
        "id": "one",
        "question": "Question?",
        "class": "private",
        "expectation": "answer",
        "expected_sources": [],
        "answer_notes": "note",
        "suites": [],
        "tags": [],
        "added": "now",
    }
    path = tmp_path / "questions.jsonl"
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="answer cases need sources"):
        quality.private_cases(path)


def test_oversized_corpus_is_rejected_before_parser_or_unbounded_read(tmp_path, monkeypatch):
    path = tmp_path / "huge.pdf"
    with path.open("wb") as stream:
        stream.truncate(quality.MAX_FILE_BYTES + 1)
    monkeypatch.setattr(quality, "_load_document", lambda *_: pytest.fail("must not parse"))
    case = quality.Case("none", "Question?", "refuse", [], [], [], [])
    with pytest.raises(ValueError, match="at most 10 MiB"):
        quality.evaluate([path], [case])


def test_parser_sees_immutable_snapshot_even_if_admitted_file_changes(tmp_path, monkeypatch):
    suite = {"documents": [{"file": "one.pdf", "pages": ["Amber particles glow."]}]}
    [path] = quality.render_documents(suite, tmp_path)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    original_parser = quality._load_document

    def changing_source(request):
        path.write_bytes(b"changed after admission")
        assert Path(request["file_path"]) != path
        return original_parser(request)

    monkeypatch.setattr(quality, "_load_document", changing_source)
    case = quality.Case("one", "Amber particles?", "answer", [[("one.pdf", 1)]], [], [], [])
    report = quality.evaluate([path], [case], packet=True)
    assert report["corpus_files"][0]["sha256"] == original_hash
    assert "Amber particles glow" in report["cases"][0]["sources"][0]["text"]


def test_cli_public_suite_is_offline_and_exports_pdf_material(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gct.eval.local_quality",
            "--suite",
            str(SUITE),
            "--export-pdfs",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert len(list(tmp_path.glob("*.pdf"))) == 4
    assert json.loads(result.stdout)["answerability"]["rate"] is None
