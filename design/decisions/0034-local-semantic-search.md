# 0034. Add measured local semantic search as a rebuildable index

- **Date:** 2026-10-03
- **Status:** accepted; implementation and packaging require separate validation
- **Amends:** ADR 0033's initial lexical-only retrieval choice

## Evidence

The [preregistered local comparison](../local-embedding-validation.md) found that
MiniLM L6 v2 int8 plus keyword retrieval retained all existing successful cases
and improved independent holdout coverage from 23/28 to 26/28. Dense-only retrieval
lost private evidence; BGE exceeded the memory target. No answer generation was
performed. These are bounded retrieval results, not general answer accuracy.

## Decision

Bundle the pinned MiniLM ONNX model and tokenizer with the personal Mac preview.
Use local CPU inference, no runtime model downloads and no paid fallback. Keep
the Python 3.10 lexical core usable; the optional semantic dependencies require
Python 3.11+ and the bundled app uses the tested Python 3.13 runtime.

Add an explicit, account-independent **Prepare semantic search** action for the
selected class. Use the existing cancellable finite Python worker rather than
a resident inference service. Preparation is limited to 210 seconds and 50,000
embedding windows per class, with a 240-second parent deadline. These are resource
limits, not promises that every library below the limits finishes in that time.

Keep vectors in an optional private SQLite sidecar, separate from the canonical
version-one library. Bind a complete class snapshot and exact model, tokenizer,
runtime and preprocessing fingerprint to each published index. Retain unchanged
chunk identities and physical pages. Stream windows in canonical class order,
carrying batches of 16 across chunk boundaries: dynamic int8 quantization made
the batch/padding policy consequential in the experiment.

Publish only complete indexes atomically. Cancellation, deadline, invalid assets
or a changed class must not make partial evidence searchable or damage originals.
Check the current canonical snapshot and scoped source identities before use.
Missing, stale or damaged optional indexes keep keyword search available; canonical
library corruption remains a recovery error. Rebuildable vectors are excluded
from source-library backups, which retain the documents needed to prepare again.

For ready indexes, combine top 50 lexical and dense candidates with equal-weight
reciprocal-rank fusion at constant 60, then retain at most eight passages and
40,000 labeled-context characters. Scope candidates before ranking. Explicit page
selection still uses the selected evidence directly. Scores do not establish
support; Grounder keeps responsibility for answer states and citations.

Expose preparation, cancellation and actual search availability as functional
controls. Visual redesign remains separate. A failed query encoder must report
keyword fallback rather than leave the interface claiming semantic retrieval.

## Consequences and rejected alternatives

The app gains a free local model and additional runtime size. Index preparation
is explicit, so importing a document does not wait for a model or introduce
hidden generation. Changed classes need preparation again. Existing keyword
search remains useful before preparation, on Python 3.10, or without model assets.

A hosted embedding API violates the zero-spending requirement. Dense-only ranking
failed evidence retention. A resident service, background scheduler or vector
server would add lifecycle complexity without a measured need in this preview.
Per-document batching was not treated as equivalent to the measured class policy.

The accepted design requires tests for streamed parity, scope, stale/corrupt cache,
interruption, resource bounds, packaged relocation and offline use. A future change
to models, batching, ranking or index publication must be measured again.
