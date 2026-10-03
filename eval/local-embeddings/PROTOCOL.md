# Local embedding study: predeclared decision rules

Set October 3, 2026 before inspecting candidate results. Baseline: GCT main
06b45e6129c456ae7736aea0f11469716341820d on this 16 GiB Apple Silicon Mac.

No money may be spent. Use only local CPU computation and publicly downloadable
free model files. No hosted inference, ChatGPT answer generation, paid API,
credits, new subscription, cloud compute, or purchased signing certificate.
Private course text remains on disk. The existing public-repository CI uses
standard free Ubuntu runners; do not upload models, evaluation packets or build
artifacts to Actions storage and do not add paid runners.

Compare current FTS with two fixed local model candidates (BGE-small English v1.5
and MiniLM L6 v2), each alone and with simple equal-weight reciprocal-rank fusion.
Use RRF k=60 and no per-case tuning. One independent reviewer authors a new unseen
holdout before results; freeze the preferred candidate and its configuration on
the existing suites before exposing that holdout. Label all development/tuning
decisions and preserve unsuccessful arms. If a hypothesis changes after results,
record the new hypothesis as a separate experiment, not the original prediction.

Primary measure is all required evidence groups represented in the final eight
passages. Also report any-source recall, required-group counts, top1/3/5 behavior,
and per-case regressions. Keep unsupported cases out of recall denominators;
candidate retrieval does not establish answerability, factual support or a correct
refusal. No new generated-answer accuracy claim is possible from offline retrieval.

Retain physical page/chunk identity and the production 40,000-character labeled
context limit. Use identical original chunks for all arms; if embedding windows
are needed for model token limits, aggregate back to those same chunk identities.
Never silently truncate passages. Scope before candidate ranking/limit. Include
exact terms/numbers, cross-class exclusion, distant paraphrases and multiple sources.

Advance a candidate only if it fixes the existing known paraphrase miss, retains
the currently retrieved evidence on all existing answerable cases (including all
8 private questions), and is no worse than FTS on independent holdout primary recall
with improved paraphrase coverage. Review individual exact-term/multi-source losses,
not just aggregate gains. If no candidate satisfies this, retain FTS and document why.

Measure model assets and additional installed runtime bytes, fresh-process imports
and model loading, warm query encoding, representative corpus indexing, and peak
resident memory with the same thread count and workload. Practical personal-preview
targets: fresh model load plus query under 5 seconds, warm query under 0.5 seconds,
embedding worker peak under 512 MiB, model+runtime growth under 250 MiB. These are
acceptance budgets to measure, not predictions. Serial timing runs avoid concurrent
build/test load. Larger-library indexing must remain cancellable and atomic; keep
existing local search usable if the model is missing, unavailable or an index fails.

An evidence win authorizes a separately reviewed integration and packaged-runtime
test. Downloaded models and private corpora never enter Git. Model revision, licenses,
tokenization/pooling, quantization and file hashes belong in the reproducibility record.
