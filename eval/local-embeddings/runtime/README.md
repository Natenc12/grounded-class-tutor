# Reproduce the local runtime measurements

These tools download free, public model artifacts and run inference on the local
CPU. They never use ChatGPT, an inference endpoint, paid API, credits, cloud
compute or course uploads. Keep downloaded weights and new measurements in ignored
`desktop/build` or `.ship` directories, never in Git.

The measured environment was an Apple M1 Pro (8 cores, 16 GiB), macOS 26.5.2 and
CPython 3.13.14. The pinned ONNX Runtime wheel requires macOS 14 or newer on arm64;
ONNX Runtime 1.30.0 and NumPy 2.4.4 require Python 3.11 or newer. These are study
requirements, not a change to GCT's Python 3.10 core requirement.

From the repository root, using uv and an existing Python installation or its
free managed Python download:

```sh
uv venv --python 3.13.14 .ship/embedding-reproduce-venv
uv pip install --python .ship/embedding-reproduce-venv/bin/python -r eval/local-embeddings/runtime/requirements.lock.txt
.ship/embedding-reproduce-venv/bin/python eval/local-embeddings/runtime/download_assets.py --assets-root desktop/build/model-study
.ship/embedding-reproduce-venv/bin/python -I -B eval/local-embeddings/runtime/benchmark.py --encoder eval/local-embeddings/encoder.py --assets-root desktop/build/model-study --output-root .ship/embedding-reproduced-runtime
```

`requirements.lock.txt` freezes every resolved study dependency. It is independent
of production `pyproject.toml` and `uv.lock`. The full dependency set was measured;
no untested removal of Hub/download dependencies is assumed. The encoder loads
local ONNX/tokenizer files directly, with no Hub loader or remote-code execution.

`assets.json` records immutable model revisions, file sizes, SHA256 values,
original and converted model provenance, token limits, pooling and query-prefix
policy. The downloader checks every file, reuses matching assets, publishes only
verified complete downloads, and preserves any existing differing file. Its only
network activity is retrieving these public artifacts. Full license texts and
original model cards are included; BGE's original card specifies CLS pooling even
though a conversion README gives a generic mean-pooling example.

`protocol.json` is byte-identical to the protocol frozen before the original
measurements. The benchmark preflights all asset hashes, then launches three fresh
processes per model in the recorded counterbalanced order. Each process rejects
Python socket connections and uses a minimal environment. Each trial times imports,
model construction, its first query, 20 warm queries and five warmed batches of
16 synthetic 250-word passages. The encoder uses two CPU threads, batch 16 and
32-token overlap without dropping passage tails. Run the benchmark without another
indexing job or heavy build to reduce contention.

`original-benchmark-raw.json`, `original-benchmark-summary.json` and
`original-dependency-size.json` preserve the recorded October 3 measurements.
Those results are historical observations, not a promise of matching timings on
another machine. Fresh-process latency does not imply flushed filesystem caches;
peak RSS is the process high-water memory for this specified workload, not a
worst-case guarantee. Uncompressed model/dependency bytes are not measured ZIP
growth. Retrieval quality and answer accuracy require separate evaluation.

Since those measurements, the benchmark driver gained portable CLI paths, asset
hash preflight outside the timed child, a first-result timeout, a macOS RSS-unit
check and Ruff formatting. The synthetic texts, trial order, candidate settings
and timing boundaries within the child remain the same. The original measured
encoder SHA256 remains in the original result files; new runs record their actual
encoder hash. The tracked encoder may have formatting changes from the original;
compare its declared preprocessing and model fingerprints before interpreting a
reproduction as an identical configuration.

The downloader tests use synthetic bytes and mocked network responses:

```sh
uv run pytest eval/local-embeddings/runtime/test_download_assets.py
uv run ruff check eval/local-embeddings/runtime
uv run ruff format --check eval/local-embeddings/runtime
```
