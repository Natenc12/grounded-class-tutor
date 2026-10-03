"""Pinned, optional CPU embedding encoder. Importing this module performs no inference.

Stream complete parent chunks in canonical class order. Carry sixteen windows
across chunk boundaries to preserve the measured int8 batching/padding policy.
No Hub loader, remote code, download, provider, or credential is involved.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import stat
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

DIMENSIONS = 384
TOKENS = 256
OVERLAP = 32
BATCH_SIZE = 16
MAX_WINDOWS_PER_CHUNK = 256
MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
FILES = {
    "model.onnx": (23_026_053, "4278337fd0ff3c68bfb6291042cad8ab363e1d9fbc43dcb499fe91c871902474"),
    "tokenizer.json": (466_247, "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037"),
}


class EmbeddingError(Exception):
    """An optional semantic capability failed; lexical search can remain usable."""


class EmbeddingLimit(EmbeddingError):
    """A complete index cannot be made within the explicit resource limit."""


@dataclass(frozen=True)
class ModelAssets:
    root: Path
    fingerprint: str


def admit(model_root: str | Path | None) -> ModelAssets:
    """Verify pinned local bytes and bind preprocessing plus installed runtimes."""
    if model_root is None:
        raise EmbeddingError("Local model unavailable")
    root = Path(model_root).absolute()
    for filename, (size, expected) in FILES.items():
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(root / filename, flags), "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise EmbeddingError("Unexpected local model file")
            digest = hashlib.sha256()
            while block := source.read(1024 * 1024):
                digest.update(block)
            if digest.hexdigest() != expected:
                raise EmbeddingError("Local model integrity check failed")
    try:
        versions = {
            name: importlib.metadata.version(name)
            for name in ("numpy", "onnxruntime", "tokenizers")
        }
    except importlib.metadata.PackageNotFoundError as exc:
        raise EmbeddingError("Optional local model runtime unavailable") from exc
    identity = {
        "revision": MODEL_REVISION,
        "files": FILES,
        "dimensions": DIMENSIONS,
        "tokens": TOKENS,
        "overlap": OVERLAP,
        "pooling": "masked-mean-float32-l2",
        "query_prefix": "",
        "query_windows": "normalized-mean",
        "passage_windows": "max-cosine",
        "batch_size": BATCH_SIZE,
        "padding": "batch-longest",
        "batch_scope": "complete-class",
        "order": "filename-sha256-page-ordinal",
        "threads": [2, 1],
        "runtime": versions,
        "encoding_version": 1,
    }
    return ModelAssets(
        root, hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    )


def spans(length: int) -> Iterator[tuple[int, int]]:
    if length <= 0:
        raise EmbeddingError("Empty token sequence")
    start = 0
    count = 0
    while start < length:
        count += 1
        if count > MAX_WINDOWS_PER_CHUNK:
            raise EmbeddingLimit("Too many windows in a source passage")
        end = min(start + TOKENS - 2, length)
        yield start, end
        if end == length:
            return
        start = end - OVERLAP


class Encoder:
    def __init__(self, assets: ModelAssets):
        # Optional imports remain inside the operation, so base installs retain CRUD/FTS.
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        ort.disable_telemetry_events()
        self.tokenizer = Tokenizer.from_file(str(assets.root / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.cls, self.sep, self.pad = (
            self.tokenizer.token_to_id(name) for name in ("[CLS]", "[SEP]", "[PAD]")
        )
        if None in (self.cls, self.sep, self.pad):
            raise EmbeddingError("Invalid tokenizer special tokens")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(assets.root / "model.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self.inputs = self.session.get_inputs()
        if any(
            item.name not in {"input_ids", "attention_mask", "token_type_ids"}
            or item.type != "tensor(int64)"
            for item in self.inputs
        ):
            raise EmbeddingError("Invalid local model input contract")
        self.output = self.session.get_outputs()[0].name

    def _normalize(self, values):
        np = self.np
        values = np.asarray(values, dtype=np.float32)
        norms = np.linalg.norm(values, axis=-1, keepdims=True)
        if not np.isfinite(values).all() or (norms < 1e-12).any():
            raise EmbeddingError("Invalid local model vector")
        return np.asarray(values / norms, dtype=np.float32)

    def _batch(self, windows):
        np = self.np
        width = max(map(len, windows))
        tokens = np.full((len(windows), width), self.pad, dtype=np.int64)
        attention = np.zeros_like(tokens)
        for index, ids in enumerate(windows):
            tokens[index, : len(ids)] = ids
            attention[index, : len(ids)] = 1
        feeds = {
            "input_ids": tokens,
            "attention_mask": attention,
            "token_type_ids": np.zeros_like(tokens),
        }
        hidden = self.session.run(
            [self.output], {item.name: feeds[item.name] for item in self.inputs}
        )[0]
        if hidden.ndim != 3 or hidden.shape != (*tokens.shape, DIMENSIONS):
            raise EmbeddingError("Invalid local model output contract")
        mask = attention[..., None].astype(hidden.dtype)
        pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
        return self._normalize(pooled)

    def stream(
        self,
        chunks: Iterable[tuple[str, str]],
        *,
        check: Callable[[], None] = lambda: None,
    ) -> Iterator[tuple[str, int, bytes]]:
        windows = []
        owners = []
        for chunk_id, text in chunks:
            check()
            if not isinstance(text, str) or not text.strip() or len(text) > 30_000:
                raise EmbeddingError("Invalid source passage")
            ids = self.tokenizer.encode(text, add_special_tokens=False).ids
            for ordinal, (start, end) in enumerate(spans(len(ids))):
                windows.append([self.cls, *ids[start:end], self.sep])
                owners.append((chunk_id, ordinal))
                if len(windows) == BATCH_SIZE:
                    check()
                    for owner, vector in zip(owners, self._batch(windows), strict=True):
                        yield (*owner, vector.astype("<f4", copy=False).tobytes())
                    windows.clear()
                    owners.clear()
        if windows:
            check()
            for owner, vector in zip(owners, self._batch(windows), strict=True):
                yield (*owner, vector.astype("<f4", copy=False).tobytes())
        check()

    def query(self, question: str):
        np = self.np
        vectors = [
            np.frombuffer(vector, dtype="<f4")
            for _, _, vector in self.stream([("query", question)])
        ]
        return self._normalize(np.stack(vectors).mean(axis=0))
