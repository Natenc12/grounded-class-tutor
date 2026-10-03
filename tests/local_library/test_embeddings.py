"""Default-runtime checks; real model parity is run separately with pinned local assets."""

import hashlib
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from gct.local import embeddings


def test_token_windows_cover_tail_without_truncating():
    for length in (1, 253, 254, 255, 900, 30_000):
        ranges = list(embeddings.spans(length))
        assert ranges[0][0] == 0
        assert ranges[-1][1] == length
        assert max(end - start for start, end in ranges) <= 254
        assert set().union(*(set(range(start, end)) for start, end in ranges)) == set(range(length))
    with pytest.raises(embeddings.EmbeddingLimit):
        list(embeddings.spans(1_000_000))


def test_stream_carries_exact_batches_across_parent_chunks():
    encoder = embeddings.Encoder.__new__(embeddings.Encoder)
    encoder.tokenizer = SimpleNamespace(
        encode=lambda text, **_: SimpleNamespace(ids=list(range(int(text))))
    )
    encoder.cls, encoder.sep, encoder.pad = 101, 102, 0
    batches = []

    class Vector:
        def astype(self, *args, **kwargs):
            return self

        def tobytes(self):
            return struct.pack("<384f", 1, *([0] * 383))

    def batch(windows):
        batches.append([list(window) for window in windows])
        return [Vector() for _ in windows]

    encoder._batch = batch
    source = [(str(i), str(700 if i % 3 == 0 else 20)) for i in range(19)]
    actual = list(encoder.stream(iter(source)))
    expected = []
    expected_windows = []
    for chunk, text in source:
        for ordinal, (start, end) in enumerate(embeddings.spans(int(text))):
            expected.append((chunk, ordinal))
            expected_windows.append([101, *range(start, end), 102])
    assert [(chunk, ordinal) for chunk, ordinal, _ in actual] == expected
    assert [window for batch in batches for window in batch] == expected_windows
    assert all(len(batch) == 16 for batch in batches[:-1])
    assert 0 < len(batches[-1]) <= 16


def test_admission_pins_bytes_and_runtime_fingerprint(tmp_path, monkeypatch):
    files = {}
    for name, data in (("model.onnx", b"originalmodel"), ("tokenizer.json", b"tokenizer")):
        (tmp_path / name).write_bytes(data)
        files[name] = (len(data), hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(embeddings, "FILES", files)
    monkeypatch.setattr(embeddings.importlib.metadata, "version", lambda _: "first")
    first = embeddings.admit(tmp_path)
    monkeypatch.setattr(embeddings.importlib.metadata, "version", lambda _: "second")
    assert embeddings.admit(tmp_path).fingerprint != first.fingerprint
    (tmp_path / "model.onnx").write_bytes(b"wrong___model")
    with pytest.raises(embeddings.EmbeddingError):
        embeddings.admit(tmp_path)


def test_admission_rejects_symlink(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_bytes(b"test")
    (tmp_path / "model.onnx").symlink_to(target)
    monkeypatch.setattr(
        embeddings, "FILES", {"model.onnx": (4, hashlib.sha256(b"test").hexdigest())}
    )
    with pytest.raises((OSError, embeddings.EmbeddingError)):
        embeddings.admit(Path(tmp_path))
