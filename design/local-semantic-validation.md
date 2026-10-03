# Local semantic integration evidence

Observed October 3, 2026 on the same Apple M1 Pro and isolated Python 3.13.14 used
for the [model comparison](local-embedding-validation.md). The selected model and
ranking policy were fixed before integration. All inference below was local;
generation responses in bridge tests were mocked. No credits, paid APIs, hosted
inference, account access or source uploads were used.

## Production parity

The bounded production encoder produced bit-identical float32 window vectors to
the frozen study on all four corpora. Every final selected ranking matched across
82 questions: public development 28, Aster 8, private 12, and the now-exposed
holdout 34. That preserves the measured 26/28 holdout source coverage versus
23/28 FTS, including the two remaining misses; it is not answer-accuracy evidence.

The production index also matched all 34 after-foreign-import rankings, returned
no foreign-class passages, and returned no candidates for the empty-class checks.
Vectors remain attached to the original stored chunk/page identity. Complete class
batching and query pooling are unchanged from the selected experiment.

Private-corpus preparation took 3.79 seconds for 122 chunks; status validation took
39.6 ms and retrieval p95 was 127.7 ms across its twelve questions. Public-corpus
retrieval p95 was 84.7 ms. These core calls include model loading, asset hashes,
canonical original/chunk checks, cache validation and ranking. They exclude process
startup, app IPC and generation. The earlier 5 ms figure measured a narrower warm
benchmark boundary and is not the app's latency.

## Independent failure and resource checks

The independent tester exercised different-model fingerprints, corrupt vector
blobs/norms/dimensions, incomplete indexes, same-name files in different classes,
empty classes, source growth/deletion, source mutation during preparation,
deadline/interruption rollback, misordered encoder output and canonical corruption.
A killed SQLite writer preserves a recoverable prior generation: writable recovery
is tested explicitly; read-only search does not promise to recover every hot journal.

The desktop boundary rejects missing, late, duplicate, contradictory and
out-of-scope search-mode events. Semantic preparation cannot request generation.
Cancellation and timeout reap even a test child that ignores SIGTERM, and a late
preparation result cannot publish readiness. Preparation works signed out; account
changes do not cancel this local operation.

An actual-model synthetic probe reached the maximum admitted **one-document**
boundary of 500 pages and one million extracted characters: 1,000 chunks and
1,500 windows prepared in 26.22 seconds, with 426.86 MiB peak process memory.
A deliberately five-second rebuild stopped at 5.21 seconds and preserved the prior
cache exactly; a subsequent retrieval returned eight passages in 165.8 ms.
A 30,000-character passage retained its tail across 68 windows. Overall probe peak
was 432.89 MiB, below the 512 MiB acceptance budget. This is not a measurement of
the 50,000-window class cap or every legal input shape.

The [sanitized integration record](../eval/local-embeddings/results/integration-2026-10-03.json)
binds metrics to code hashes and records every parity case. No private source text,
filenames, model weights, account state or live-library copy is published.

## Package and final checks

The complete local suite passed: **539 Python tests and 160 desktop tests**, plus
Ruff and formatting. The real packaged Python was relocated outside the checkout
and exercised semantic preparation/restart, a paraphrase, missing/corrupt-model
fallback, stale cache, source citations/deletion and canonical-only backup. Generation
was mocked. All 76 native-library dependency checks passed; ad-hoc signing was verified.

The app contains 557,075,339 regular-file bytes, up 144.70 MiB from the prior preview,
below the 250 MiB growth budget. The ZIP is 215,484,719 bytes, up 53.08 MiB. Its SHA-256
is `a2efbde6c4932390e41b0310328b1d0533a6ed24c0ddf7bf0351b48c1eec526f`.
The artifact remains in ignored `desktop/dist`; it was not uploaded or publicly
distributed. Model assets, source/model hashes and dependency licenses are included
in the bundle's manifest and notices. Supplemental wheel notices retain 588 original
files or header excerpts from 130 pinned sources; independent review verified their
bytes and reproduced the consolidated text. Packaging binds these notices to the
installed wheel metadata and tokenizer SBOM. These checks did not open the existing library
or account store, launch native UI or make SDK requests.

## Remaining limits

The lexical component still uses global BM25 statistics. Stale vectors can remain
in the private sidecar until replacement/rebuild, though canonical checks prevent
their use. An unrecognized or damaged sidecar is preserved and can require manual
cache recovery; canonical corruption is never silently treated as missing support.
The optional semantic extra is unavailable in the Python 3.10 source environment;
that environment keeps keyword search.

Native account continuity still awaits the existing macOS Keychain permission
check. Fresh-Mac installation, guided restore, visual redesign and broader real
course answer-quality evaluation remain separate release work. No paid signing,
hosting or inference is introduced to complete those tasks.
