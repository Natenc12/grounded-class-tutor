"""Experimental local-only ONNX encoding. No Hub/client imports or network calls.

Frozen policy: CPU/2 intra-op threads/1 inter-op thread, batches of 16, 32-token
overlap. Tokenize without truncation; every token belongs to at least one window.
BGE: CLS, query-only instruction. MiniLM: masked mean, no instruction. All
window embeddings are L2-normalized float32. Preserve parent passage identities
outside this module; windows never become citation IDs.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
ort.disable_telemetry_events()

OVERLAP = 32
BATCH_SIZE = 16
MAX_WINDOWS_PER_TEXT = 256


def spans(length: int, capacity: int, overlap: int = OVERLAP) -> list[tuple[int, int]]:
    if length <= 0 or capacity <= overlap or overlap < 0:
        raise ValueError("Need nonempty tokens and a window larger than the overlap")
    result = []
    start = 0
    while start < length:
        end = min(start + capacity, length)
        result.append((start, end))
        if len(result) > MAX_WINDOWS_PER_TEXT:
            raise ValueError("Input exceeds the explicit window budget; no text was truncated")
        if end == length:
            break
        start = end - overlap
    return result


def normalized(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float32)
    if not np.isfinite(values).all():
        raise ValueError("Non-finite embedding")
    norms = np.linalg.norm(values, axis=-1, keepdims=True)
    if (norms < 1e-12).any():
        raise ValueError("Zero embedding")
    return np.asarray(values / norms, dtype=np.float32)


class Encoder:
    def __init__(self, spec: dict | str | Path):
        started = time.perf_counter()
        if not isinstance(spec, dict):
            spec = json.loads(Path(spec).read_text())
        self.spec = spec
        self.model_path = Path(spec["model_path"])
        self.tokenizer_path = Path(spec["tokenizer_path"])
        if not self.model_path.is_file() or not self.tokenizer_path.is_file():
            raise ValueError("Pinned local model and tokenizer files are required")
        self.limit = spec["max_tokens"]
        self.pooling = spec["pooling"]
        if self.pooling not in {"cls", "mean"} or self.limit not in {256, 512}:
            raise ValueError("This experiment admits only the two prespecified encoder contracts")
        token_start = time.perf_counter()
        self.tokenizer = Tokenizer.from_file(str(self.tokenizer_path))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self.cls = self.tokenizer.token_to_id("[CLS]")
        self.sep = self.tokenizer.token_to_id("[SEP]")
        self.pad = self.tokenizer.token_to_id("[PAD]")
        if None in (self.cls, self.sep, self.pad):
            raise ValueError("Expected the pinned BERT-family special tokens")
        self.prefix = self.tokenizer.encode(
            spec.get("query_prefix", ""), add_special_tokens=False
        ).ids
        self.tokenizer_load_ms = (time.perf_counter() - token_start) * 1000
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = 3
        session_start = time.perf_counter()
        self.session = ort.InferenceSession(
            str(self.model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.session_load_ms = (time.perf_counter() - session_start) * 1000
        self.inputs = self.session.get_inputs()
        if any(
            item.name not in {"input_ids", "attention_mask", "token_type_ids"}
            for item in self.inputs
        ):
            raise ValueError("Unexpected ONNX input contract")
        if any(item.type != "tensor(int64)" for item in self.inputs):
            raise ValueError("Expected integer tokenizer inputs")
        self.output = self.session.get_outputs()[0].name
        self.load_ms = (time.perf_counter() - started) * 1000
        self.last_stats = {}

    def encode(self, texts: list[str], is_query: bool = False) -> list[np.ndarray]:
        started = time.perf_counter()
        if not texts:
            return []
        prefix = self.prefix if is_query else []
        capacity = self.limit - 2 - len(prefix)
        windows = []
        owners = []
        token_counts = []
        window_counts = []
        for index, text in enumerate(texts):
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Embedding inputs must be nonempty text")
            ids = self.tokenizer.encode(text, add_special_tokens=False).ids
            token_counts.append(len(ids))
            ranges = spans(len(ids), capacity)
            window_counts.append(len(ranges))
            for start, end in ranges:
                windows.append([self.cls, *prefix, *ids[start:end], self.sep])
                owners.append(index)
        output = [[] for _ in texts]
        inference_ms = 0.0
        batches = 0
        for begin in range(0, len(windows), BATCH_SIZE):
            batch = windows[begin : begin + BATCH_SIZE]
            length = max(map(len, batch))
            inputs = np.full((len(batch), length), self.pad, dtype=np.int64)
            attention = np.zeros_like(inputs)
            for row, ids in enumerate(batch):
                inputs[row, : len(ids)] = ids
                attention[row, : len(ids)] = 1
            feeds = {
                "input_ids": inputs,
                "attention_mask": attention,
                "token_type_ids": np.zeros_like(inputs),
            }
            tick = time.perf_counter()
            hidden = self.session.run(
                [self.output], {item.name: feeds[item.name] for item in self.inputs}
            )[0]
            inference_ms += (time.perf_counter() - tick) * 1000
            if hidden.ndim != 3 or hidden.shape[:2] != inputs.shape or hidden.shape[2] != 384:
                raise ValueError("Unexpected token-embedding shape")
            if self.pooling == "cls":
                pooled = hidden[:, 0, :]
            else:
                mask = attention[..., None].astype(hidden.dtype)
                pooled = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
            vectors = normalized(pooled)
            for offset, vector in enumerate(vectors):
                output[owners[begin + offset]].append(vector)
            batches += 1
        self.last_stats = {
            "texts": len(texts),
            "tokens": sum(token_counts),
            "max_tokens_in_text": max(token_counts),
            "windows": len(windows),
            "multiwindow_texts": sum(count > 1 for count in window_counts),
            "largest_window_count": max(window_counts),
            "batches": batches,
            "inference_ms": inference_ms,
            "total_ms": (time.perf_counter() - started) * 1000,
            "truncated_tokens": 0,
        }
        return [np.stack(vectors).astype(np.float32) for vectors in output]

    def query(self, text: str) -> np.ndarray:
        """Normalize the average of all query windows, retaining each query token."""
        return normalized(self.encode([text], is_query=True)[0].mean(axis=0))

    def fingerprint(self) -> str:
        # Hashes bind caches to exact model/tokenizer bytes and preprocessing.
        def digest(path):
            value = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    value.update(block)
            return value.hexdigest()

        identity = {
            "model": digest(self.model_path),
            "tokenizer": digest(self.tokenizer_path),
            "limit": self.limit,
            "pooling": self.pooling,
            "query_prefix": self.spec.get("query_prefix", ""),
            "overlap": OVERLAP,
            "normalization": "l2-float32",
            "query_windows": "normalized-mean",
            "passage_windows": "max-cosine",
            "schema": 1,
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
