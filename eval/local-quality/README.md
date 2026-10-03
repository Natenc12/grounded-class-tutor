# Local quality bench

Run from the repository with the existing development dependencies installed:

```sh
uv run python -m gct.eval.local_quality --suite eval/local-quality/suite.json
uv run python -m gct.eval.local_quality --suite eval/local-quality/aster.json
uv run python -m gct.eval.local_quality --questions eval/questions.jsonl --corpus data/dogfood/religion
```

The commands make no model or network calls and use temporary databases. Public
manifests contain original synthetic material, rendered to deterministic PDFs and
read through the production parser, chunker and search. Private PDFs/PPTX are read
once into bounded snapshots. Neither the original files nor the app library changes.

`--export-pdfs NEW_DIRECTORY` retains the public PDFs for desktop checks, refusing
existing filenames. `--packet` includes questions, ordered source passages, expected
facts and prohibited claims for review. Private packets and reviews contain course
material: keep them in ignored local storage. Default output omits this prose.

Retrieval reports distinguish any expected source, every required evidence group,
and total groups found at top 1/3/5/8. Members within a group are alternatives;
each group is required. Unsupported questions are outside the recall denominator.
Finding candidates for an unsupported question is not evidence of a correct answer.
The private legacy suite's expected sources retain their existing any-match meaning.

Answerability and claim support remain **unmeasured** without recorded model answers
and explicit human or agent review. `--reviews FILE` imports this shape:

```json
{
  "corpus_digest": "from report",
  "case_digest": "from report",
  "reviews": [{
    "id": "case id",
    "evidence_digest": "from case row",
    "reviewers": [{"kind": "agent", "name": "identified reviewer"}],
    "response_origin": "model",
    "model": "recorded model name",
    "state": "GROUNDED",
    "answer": "actual answer, or explicit refusal",
    "answerability": "correct",
    "all_claims_reviewed": true,
    "claims": [{"text": "one substantive claim", "verdict": "supported", "reason": "source comparison"}]
  }]
}
```

Reviewer kind is `human` or `agent`; judgments are declared reviews, not automatic
entailment checks. Answerability is `correct` or `incorrect`; claim verdicts are
`supported`, `unsupported` or `contradicted`. Refusals have no claims and receive no
free claim-support credit. Mock responses and provider errors cannot enter quality
rates. Digests reject reviews from a changed corpus, question suite or evidence order.

`aster-observed.json` records the separate eight-question subscription pilot at
`e2cd255`, inspected by the root and governance agents. It is historical evidence,
not a review file automatically applied to new runs. The suite was independently
authored before the implementation changes; it is now public, not a continuing blind
holdout. A tiny synthetic domain does not establish general tutor accuracy.

Known limits: the development suite exposes a low-overlap paraphrase miss. No
case-specific synonym list or new embedding dependency masks it. FTS5 BM25 also uses
global index statistics: adding documents to another class can change ordering
within the selected class, although SQL scope prevents returning those other-class
chunks. A minimal reproduction ranks `beta beta` above `alpha` for `alpha beta`,
then flips after adding twenty `beta beta` documents to a different class. Class-local
ranking needs separate design and measurement. Exact relevance ties now use stable
filename/content/page/ordinal keys instead of fresh random document IDs.
