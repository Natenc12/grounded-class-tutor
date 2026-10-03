# Local embedding comparison

Measured October 3, 2026 against `06b45e6129c456ae7736aea0f11469716341820d`.
The experiment used local CPU inference and free public downloads. No paid API,
credits, hosted inference, answer generation, or course-data upload was used.

## Decision and limits

Advance **MiniLM L6 v2 int8 combined with FTS** to a separately reviewed app
integration. The combined retriever passed the criteria written before results:
it recovered the existing paraphrase miss, retained every previously successful
answerable case at eight passages, and improved independently authored holdout
coverage without losing an exact-detail or multi-source success.

This document records an experiment. It does not mean the app already uses
embeddings, prove generated answers correct, or establish performance on a large
library. Preserve lexical search if the model or a complete, current index is
unavailable. No paid fallback is permitted.

## Retrieval evidence

Full coverage means every required source group appears among the final eight
passages. Unsupported questions are excluded from that denominator.

| Corpus | Answerable cases | Existing FTS | MiniLM dense only | MiniLM + FTS |
|---|---:|---:|---:|---:|
| Public development | 23 | 22 | 23 | 23 |
| Earlier synthetic Aster corpus | 6 | 6 | 6 | 6 |
| Existing private course corpus | 8 | 8 | 6 | 8 |
| Independent holdout | 28 | 23 | 26 | 26 |

The 34-question holdout contained 28 answerable questions, six unsupported
questions, 34 required source groups, eight paraphrases, exact names/numbers,
conflicting distractors and six multi-source cases. An independent agent froze
its sources and rubrics before candidate results. The lead froze MiniLM's
configuration before the holdout was released. That fixture is now public and
must not be represented as unseen in future experiments.

Holdout paraphrase coverage improved from 3/8 to 6/8. Gains were `h02`, `h03` and
`h04`; misses `h01` and `h08` remain. An independent reviewer recomputed each
result from the frozen rubrics and actual returned chunk identities, without
using the builder's scoring functions. Foreign-class insertion leaked no chunks;
34 empty-class queries returned no candidates. Global BM25 statistics still
changed some within-class rankings after foreign imports, so class-independent
lexical ranking remains unresolved.

Earlier ranks are not uniformly better. MiniLM hybrid lost public
`injection-definition` at rank one, private `q007` at one, `q008` at three and
`q004` at five. All retained full coverage at eight. Dense-only retrieval lost
two private cases and was rejected. BGE hybrid also reached 37/37 existing cases,
but its tested batch size exceeded the memory budget.

## Runtime evidence

Apple M1 Pro, 8 CPU cores, 16 GiB RAM, macOS 26.5.2, CPython 3.13.14. CPU only,
two intra-op threads, one inter-op thread, batch 16. Three fresh-process trials
per model; filesystem caches were not flushed.

| Measurement | BGE small int8 | MiniLM L6 int8 |
|---|---:|---:|
| Fresh process through first query, median | 211 ms | 216 ms |
| Warm fixed-query encoding, p95 | 4.64 ms | 2.48 ms |
| Peak process memory in fixed workload | 691–736 MiB | 391–399 MiB |
| Added model and installed dependencies, uncompressed | 152.73 MiB | 142.26 MiB |

MiniLM indexed the private corpus's 122 chunks in 3.84 seconds, retaining all
35,093 tokens across 209 windows. A separate warm search measurement included
SQLite scope/text reads, lexical retrieval, query encoding, vector ranking,
fusion and final context packing. Across three repetitions of every existing
question, p95 was 3.02 ms public, 3.41 ms Aster and 5.03 ms private. It excluded
process startup, app IPC, library validation, initial vector loading and generation.
These timings are not end-to-end app latency or worst-case resource guarantees.

## Reproducibility and integration requirements

See the [predeclared protocol](../eval/local-embeddings/PROTOCOL.md),
[portable experiment](../eval/local-embeddings/README.md) and
[sanitized measurements](../eval/local-embeddings/results/2026-10-03.json).
Private prose, filenames, paths and vectors remain outside Git. Model assets
remain outside Git; pinned revisions, hashes and license evidence accompany the
download instructions.

The fixed retriever uses top 50 candidates from each method, equal reciprocal
rank fusion with constant 60, at most eight unchanged parent chunks, and the
same 40,000-character labeled context ceiling. Scope precedes ranking. MiniLM
uses masked mean pooling, normalized float32 vectors, a 256-token model limit,
32-token overlap and complete tail coverage. Passage scores use maximum window
cosine; long queries average their window vectors and normalize.

Quantized output depends slightly on batch ordering and padding. Independent
individual/reversed-batch probes changed rankings while preserving existing
37/37 coverage. Production indexing must therefore preserve the tested batching
policy or explicitly retest its changed policy; fingerprints must bind it.
Integration also requires bounded streaming, atomic complete index publication,
cancellation, stale/deleted-source exclusion, offline packaged-runtime checks,
and visible lexical fallback. No production performance claim follows solely
from the experiment above.
