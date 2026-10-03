# Local embedding retrieval experiment

This directory reproduces an offline retrieval comparison. It does not enable
embeddings in the app. The [predeclared protocol](PROTOCOL.md) fixes the comparison
and advancement rules. Source-page coverage is measured separately from generated
answers: these scripts never instantiate a generator or use ChatGPT credits.

Use the [runtime setup](runtime/README.md) to create the isolated model environment
and download the pinned public assets. Keep that environment, models, prepared
inputs and output reports in ignored `.ship/` or `desktop/build/`. Downloading the
free public assets is the only network step. Both preparation and inference block
Python socket connections. No private source text is sent to a model host.

The encoder uses CPU execution, two intra-op threads, one inter-op thread, batches
of 16 windows and 32-token overlap. It explicitly covers the last token of every
passage; windows retain their original parent chunk identity. MiniLM uses masked
mean pooling and BGE uses CLS pooling with its query-only instruction. Dense ranking
takes the maximum window cosine for each original chunk. Hybrid ranking combines
the top 50 lexical and dense candidates with equal-weight RRF at constant 60,
then applies the eight-passage and 40,000-character labeled-context limits.
The FTS baseline calls the production eight-passage retriever unchanged.

## Prepare and run

Run commands from the repository root after syncing its existing dev dependencies.
The runtime README creates the model Python at the path used below; adjust paths
when using a different local experiment directory.

```sh
uv run python eval/local-embeddings/runner.py specs \
  --assets-root desktop/build/model-study \
  --output-dir .ship/embedding-study/specs

uv run python eval/local-embeddings/runner.py prepare \
  --suite eval/local-quality/suite.json --name public \
  --output .ship/embedding-study/inputs/public.json

uv run python eval/local-embeddings/runner.py prepare \
  --suite eval/local-quality/aster.json --name aster \
  --output .ship/embedding-study/inputs/aster.json

.ship/embedding-reproduce-venv/bin/python eval/local-embeddings/runner.py run \
  --spec .ship/embedding-study/specs/minilm-spec.json \
  --inputs .ship/embedding-study/inputs/public.json .ship/embedding-study/inputs/aster.json \
  --output .ship/embedding-study/minilm-results.json
```

Repeat `run` with `bge-spec.json` for the other prespecified model. Each report keeps
the FTS, dense-only and hybrid arms, all selected source identities, per-case gains
and losses, tag strata and unsupported-candidate counts. The prepared input contains
the exact source text and UUID chunk mapping; preserve it with its report. Exact
input, scope, encoding-group and encoder fingerprints bind the measurements.

The optional private-corpus preparation command uses the existing single-class
question manifest. Its prepared inputs contain private text and must remain ignored:

```sh
uv run python eval/local-embeddings/runner.py prepare \
  --questions eval/questions.jsonl --corpus data/dogfood/religion --name private \
  --output .ship/embedding-study/inputs/private.json
```

Outputs are atomically created with mode `0600`; symlink output targets are rejected.
Original course files and the user's app library are not modified.

## Class controls and timing

The [originally unseen fixture](holdout/README.md) is now public regression material.
Its adapter imports foreign documents into separate classes, preserves primary-class
encoding batch boundaries and prepares before/after/empty-class measurements:

```sh
uv run python eval/local-embeddings/holdout_adapter.py \
  --suite eval/local-quality/embedding-holdout.json \
  --foreign eval/local-embeddings/holdout/foreign-classes.json \
  --output-dir .ship/embedding-study/controls

.ship/embedding-reproduce-venv/bin/python eval/local-embeddings/runner.py run \
  --spec .ship/embedding-study/specs/minilm-spec.json \
  --inputs .ship/embedding-study/controls/holdout-before.json \
    .ship/embedding-study/controls/holdout-after.json \
    .ship/embedding-study/controls/holdout-empty.json \
  --output .ship/embedding-study/holdout-results.json
```

`runner.py` query timings include encoding, dense scoring and fusion; lexical
candidates were prepared earlier. `full_timing.py` adds a real local SQLite query,
scoped text fetch and final context packing over three repeats of every case:

```sh
.ship/embedding-reproduce-venv/bin/python eval/local-embeddings/full_timing.py \
  --spec .ship/embedding-study/specs/minilm-spec.json \
  --inputs .ship/embedding-study/inputs/public.json \
  --output .ship/embedding-study/full-timing.json
```

This latter boundary assumes an already loaded vector cache. It excludes app IPC,
initial database validation, vector loading and generation. The runtime benchmark
separately measures fresh-process load, resident memory and fixed-workload encoding.
Run timing jobs serially to avoid competing CPU loads.

## Checks and limitations

```sh
uv run python eval/local-embeddings/test_runner.py
GCT_STUDY_SPECS=.ship/embedding-study/specs \
  .ship/embedding-reproduce-venv/bin/python eval/local-embeddings/test_encoder.py
```

The encoder tests use actual local model assets. They cover token tails, normalized
float32 output, bounded windows and offline inference. Runner tests cover scope,
the production FTS baseline, context bounds, RRF, privacy and scoring denominators.
Model downloads and these model-dependent tests are deliberately outside default CI.

Dynamic int8 quantization can change vectors when batch companions or padding change.
The experiment's batched class encoding is not equivalent to arbitrary per-document
encoding. A production cache must bind the batching policy and complete class snapshot,
then measure parity after implementing its bounded streaming path. Current SQLite
BM25 statistics are global, so other classes can affect lexical ordering even though
class predicates prevent their passages from being returned. Neither candidate scores
nor finding an expected page establish that a generated claim is supported.

These portable scripts were moved and formatted after the original measurements.
Both models reproduced every original public-suite selected ranking and metric after
that change. Historical code/configuration/result hashes remain in the evidence record;
new runs report the actual current encoder and runner hashes. Private reports and all
downloaded model bytes stay outside Git.
