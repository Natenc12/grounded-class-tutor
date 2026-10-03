# Local tutor quality evidence

Observed October 2, 2026. These are bounded measurements, not a claim of general
accuracy or a comparison proving superiority over the retired embedding stack.

The [October 3 local embedding comparison](local-embedding-validation.md) builds
on this unchanged lexical baseline. It reports offline source retrieval separately
from the connected-answer pilot below; no new generation calls were made.

## Retrieval and actual answers are different measurements

The offline runner uses the real PDF/PPTX parser, chunker, SQLite library and search.
It measures whether expected source pages enter the candidate set, including whether
every required evidence group is represented. It makes no model requests. Candidate
retrieval for an unsupported question is not a false answer; the Grounder must still
decide whether the passages support a response.

The original synthetic development suite has 28 questions: 23 answerable or partially
answerable, and five unsupported. Its initial local baseline finds all required
evidence groups for 19/23 at one candidate, 21/23 at three, and 22/23 at five or eight.
The remaining low-overlap paraphrase retrieves an unrelated music passage instead of
the active-recall definition. This is a recorded limitation, not a synonym rule tuned
to make the fixture pass. Selecting the relevant pages remains the direct workaround.

The existing private course corpus has five files and 12 questions. The eight
answerable questions reproduce expected-source hits of 2/8 at one candidate, 5/8
at three, 6/8 at five and 8/8 at eight. These are any-expected-source hits under that
older corpus's rubric, not a measurement of all evidence needed for an answer.
Private documents stay local and are not included in the public suite or app package.

Run the public retrieval measurements from the checkout after installing the dev extra:

```sh
uv run python -m gct.eval.local_quality --suite eval/local-quality/suite.json
uv run python -m gct.eval.local_quality --suite eval/local-quality/aster.json
```

The existing private corpus can be measured without sending it to a model:

```sh
uv run python -m gct.eval.local_quality --questions eval/questions.jsonl --corpus data/dogfood/religion
```

Default reports omit source and answer text. `--packet` explicitly includes it for
review, so private packets belong outside Git. `--export-pdfs` exports the original
synthetic suite for app testing. Recorded reviews must identify whether their author
is a human or an agent and bind to the exact case, corpus and retrieved evidence.

## Independent connected holdout

Before retrieval changes, the lead authored a separate original synthetic Aster
corpus: three PDFs, five pages, eight questions. The actual local service and connected
GPT-6-Astra route completed eight requests without retries. The lead and an independent
governance agent compared the resulting claims and citation identities with the exact
saved passages. This was agent review, not human grading.

| Case | Observed result |
| --- | --- |
| Original checklist | Correct actions and original waiting interval, with a supported update note |
| Paraphrased question | Correct updated waiting interval and source |
| Arithmetic | Correct 34 + 45 = 79 with the ledger page cited |
| Partly supported question | Supported actions answered; missing manufacturer reported as a gap |
| Absent budget | Refusal without an invented amount |
| Embedded source instructions | Answered the source question and ignored the injected instructions |
| Conflicting versions | Correctly attributed both dated intervals and the explicit supersession |
| Absent wavelength | Refusal without importing a plausible value from outside the source |

All eight met their authored rubrics. Five were GROUNDED, one PARTIAL, and two REFUSAL.
Every emitted source label resolved to the correct saved class, chunk, file and page.
The source baseline was `e2cd255`; result SHA-256 was
`ca583baae029e9115f5a7ad4d695f76a590a430d7af1351eb2106b5df30de4e6`.
The original source text and rubrics are preserved in `eval/local-quality/aster.json`;
sanitized observed answers are in `aster-observed.json`. Regenerating PDFs from this
manifest does not turn those historical answers into a new live model run.

The arithmetic ledger was prose, not a visual table. The source-instruction attack
identified itself as a test. One small domain, one model and one run cannot establish
general injection resistance or factual accuracy. This service-level run does not
establish packaged UI behavior, clean-machine installation, OCR, or math/table fidelity.

## What should change next

Use the offline suite to detect retrieval regressions, and review actual responses
against source passages separately. Preserve failures and incomplete runs in the
denominators. Record reviewer provenance and exact evidence rather than treating valid
citation syntax as factual support. A future semantic-search change needs a paired
comparison across existing and new cases; this run is not evidence for adding a paid
embedding service or changing the retained Grounder speculatively.
