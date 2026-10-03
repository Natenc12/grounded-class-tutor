"""Independent failure probes for the optional semantic cache; no models/network."""

from __future__ import annotations

import contextlib
import math
import select
import signal
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from gct.ingest.parse import ParsedUnit
from gct.local import embedding_index as index
from gct.local import embeddings
from gct.local.store import Library, LibraryError


def vector(axis=0):
    return struct.pack("<384f", *[float(i == axis) for i in range(384)])


@pytest.fixture
def fake_model(monkeypatch, tmp_path):
    controls = {"fingerprint": "fixture-fingerprint", "loads": 0, "queries": 0}

    def admit(_root):
        return embeddings.ModelAssets(tmp_path / "synthetic-model", controls["fingerprint"])

    class FakeEncoder:
        def __init__(self, _assets):
            controls["loads"] += 1

        def stream(self, chunks, *, check):
            for number, (chunk_id, _text) in enumerate(chunks):
                check()
                yield chunk_id, 0, vector(number % 2)

        def query(self, _question):
            controls["queries"] += 1
            return struct.unpack("<384f", vector())

    monkeypatch.setattr(embeddings, "admit", admit)
    monkeypatch.setattr(embeddings, "Encoder", FakeEncoder)
    monkeypatch.setattr(
        index,
        "_cosine",
        lambda raw, query: math.fsum(
            a * b for a, b in zip(struct.unpack("<384f", raw), query, strict=True)
        ),
    )
    controls["encoder"] = FakeEncoder
    return controls


def document(library, class_id, *, filename="Shared.pdf", original=b"first", text=None):
    return library.import_document(
        class_id,
        filename,
        original,
        [ParsedUnit(text or "Amber archive lending procedure.", filename, 1)],
        1,
    )


@pytest.fixture
def ready(tmp_path, fake_model):
    with Library(tmp_path / "library.sqlite3") as library:
        class_id = library.create_class("Synthetic primary")["id"]
        first = document(library, class_id)
        second = document(library, class_id, original=b"second", text="Blue archive stock ledger.")
        assert index.prepare(library, class_id, tmp_path)["state"] == "ready"
        assert index.status(library, class_id, tmp_path)["state"] == "ready"
        yield library, class_id, [first, second]


def cache_rows(library):
    with sqlite3.connect(library.path.with_suffix(".embeddings.sqlite3")) as connection:
        return (
            connection.execute("SELECT * FROM indexes ORDER BY class_id").fetchall(),
            connection.execute(
                "SELECT * FROM vectors ORDER BY class_id,position,window"
            ).fetchall(),
        )


def test_same_dimension_different_encoder_fingerprint_rejects_cache_before_loading(
    ready, fake_model
):
    library, class_id, _ = ready
    original = cache_rows(library)
    loads = fake_model["loads"]
    fake_model["fingerprint"] = "same-dimension-but-different-model-tokenizer-or-batching"
    assert index.status(library, class_id, None)["state"] == "stale"
    chunks, status = index.retrieve(library, class_id, "Amber?", None)
    assert chunks is None and status["mode"] == "lexical"
    assert fake_model["loads"] == loads
    assert cache_rows(library) == original


@pytest.mark.parametrize(
    "damage",
    [
        "short",
        "nonfinite",
        "zero",
        "norm",
        "missing",
        "wrong-id",
        "window-gap",
        "position-gap",
        "digest",
        "future-version",
    ],
)
def test_corrupt_cache_never_ranks_partial_evidence_or_mutates_originals(ready, damage):
    library, class_id, documents = ready
    saved = [library.original(class_id, d["id"]) for d in documents]
    path = library.path.with_suffix(".embeddings.sqlite3")
    with sqlite3.connect(path) as connection:
        if damage in {"short", "nonfinite", "zero", "norm"}:
            bad = {
                "short": b"short",
                "nonfinite": struct.pack("<384f", float("nan"), *([0] * 383)),
                "zero": bytes(1536),
                "norm": struct.pack("<384f", *([1] * 384)),
            }[damage]
            connection.execute("UPDATE vectors SET embedding=? WHERE position=0", (bad,))
        elif damage == "missing":
            connection.execute("DELETE FROM vectors WHERE position=1")
        elif damage == "wrong-id":
            connection.execute("UPDATE vectors SET chunk_id=? WHERE position=0", ("f" * 64,))
        elif damage == "window-gap":
            connection.execute("UPDATE vectors SET window=7 WHERE position=0")
        elif damage == "position-gap":
            connection.execute("UPDATE vectors SET position=9 WHERE position=1")
        elif damage == "digest":
            connection.execute("UPDATE indexes SET digest=?", ("f" * 64,))
        else:
            connection.execute("PRAGMA user_version=999")
    corrupted = path.read_bytes()
    assert index.status(library, class_id, None)["state"] == "failed"
    chunks, status = index.retrieve(library, class_id, "Amber?", None)
    assert chunks is None and status["mode"] == "lexical"
    assert path.read_bytes() == corrupted
    assert [library.original(class_id, d["id"]) for d in documents] == saved
    assert library.retrieve(class_id, "Amber"), "Optional corruption must preserve FTS."


def test_identical_filenames_and_bytes_in_other_classes_do_not_share_cache_or_citations(ready):
    library, class_id, documents = ready
    other = library.create_class("Synthetic foreign")["id"]
    copied = document(library, other)
    assert copied["id"] != documents[0]["id"]
    assert index.status(library, other, None)["state"] == "missing"
    assert index.prepare(library, other, None)["state"] == "ready"
    for selected in (class_id, other):
        chunks, status = index.retrieve(library, selected, "A meaning-only query", None)
        assert status["mode"] == "hybrid" and chunks
        expected = {
            row[0]
            for row in library.connection.execute(
                "SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id "
                "WHERE d.class_id=?",
                (selected,),
            )
        }
        assert {chunk.chunk_id for chunk in chunks} <= expected
        for chunk in chunks:
            library.citation(selected, chunk.chunk_id)
    selected, _ = index.retrieve(library, class_id, "archive", None, document_id=documents[1]["id"])
    assert len(selected) == 1
    assert library.citation(class_id, selected[0].chunk_id)["document_id"] == documents[1]["id"]
    with pytest.raises(LibraryError):
        index.retrieve(library, other, "archive", None, document_id=documents[1]["id"])


def test_empty_class_never_loads_an_encoder_or_uses_another_class_index(ready, fake_model):
    library, _, _ = ready
    empty = library.create_class("Empty control")["id"]
    loads = fake_model["loads"]
    assert index.prepare(library, empty, None)["state"] == "missing"
    result, status = index.retrieve(library, empty, "Amber", None)
    assert result is None and status["mode"] == "lexical"
    assert fake_model["loads"] == loads
    assert library.retrieve(empty, "Amber") == []


def test_class_growth_invalidates_complete_class_batches_without_overwriting_old_cache(ready):
    library, class_id, _ = ready
    old = cache_rows(library)
    document(library, class_id, original=b"third", text="A new added archive passage.")
    assert index.status(library, class_id, None)["state"] == "stale"
    result, status = index.retrieve(library, class_id, "archive", None)
    assert result is None and status["mode"] == "lexical"
    assert cache_rows(library) == old


def test_deleted_source_cannot_be_resurrected_by_ready_cached_vectors(ready):
    library, class_id, documents = ready
    removed_ids = {
        r[0]
        for r in library.connection.execute(
            "SELECT id FROM chunks WHERE document_id=?", (documents[0]["id"],)
        )
    }
    library.delete_document(class_id, documents[0]["id"])
    result, status = index.retrieve(library, class_id, "Amber", None)
    assert result is None and status["state"] == "stale"
    for chunk_id in removed_ids:
        with pytest.raises(LibraryError):
            library.citation(class_id, chunk_id)


def test_source_change_during_preparation_cannot_publish_the_old_snapshot(ready, monkeypatch):
    library, class_id, _ = ready
    old = cache_rows(library)

    class ChangingEncoder:
        def __init__(self, _assets):
            pass

        def stream(self, chunks, *, check):
            original = list(chunks)
            with Library(library.path) as other:
                document(other, class_id, original=b"racing import", text="Late archive material.")
            for chunk_id, _ in original:
                check()
                yield chunk_id, 0, vector()

    monkeypatch.setattr(embeddings, "Encoder", ChangingEncoder)
    assert index.prepare(library, class_id, None)["state"] == "stale"
    assert cache_rows(library) == old
    assert len(library.list_documents(class_id)) == 3


def test_interrupted_rebuild_rolls_back_and_preserves_ready_index(ready, monkeypatch):
    library, class_id, _ = ready
    old = cache_rows(library)

    class InterruptedEncoder:
        def __init__(self, _assets):
            pass

        def stream(self, chunks, *, check):
            first = next(iter(chunks))
            yield first[0], 0, vector(1)
            raise KeyboardInterrupt("Synthetic cancellation after a cache write")

    monkeypatch.setattr(embeddings, "Encoder", InterruptedEncoder)
    with pytest.raises(KeyboardInterrupt):
        index.prepare(library, class_id, None)
    assert cache_rows(library) == old
    assert index.status(library, class_id, None)["state"] == "ready"


def test_deadline_expiration_after_first_write_does_not_publish_partial_index(ready, monkeypatch):
    library, class_id, _ = ready
    old = cache_rows(library)
    now = [10.0]
    monkeypatch.setattr(index.time, "monotonic", lambda: now[0])

    class SlowEncoder:
        def __init__(self, _assets):
            pass

        def stream(self, chunks, *, check):
            for number, (chunk_id, _) in enumerate(chunks):
                yield chunk_id, 0, vector()
                if number == 0:
                    now[0] = 1000.0

    monkeypatch.setattr(embeddings, "Encoder", SlowEncoder)
    assert index.prepare(library, class_id, None, deadline_seconds=1)["state"] == "limited"
    assert cache_rows(library) == old
    assert index.status(library, class_id, None)["state"] == "ready"


def test_misordered_encoder_output_cannot_be_published_as_ready(ready, monkeypatch):
    library, class_id, _ = ready
    old = cache_rows(library)

    class MisorderedEncoder:
        def __init__(self, _assets):
            pass

        def stream(self, chunks, *, check):
            for chunk_id, _ in reversed(list(chunks)):
                yield chunk_id, 0, vector()

    monkeypatch.setattr(embeddings, "Encoder", MisorderedEncoder)
    assert index.prepare(library, class_id, None)["state"] != "ready"
    assert cache_rows(library) == old


def test_canonical_source_corruption_is_not_hidden_as_optional_lexical_fallback(ready):
    library, class_id, documents = ready
    library.connection.execute(
        "UPDATE documents SET original=? WHERE id=?", (b"tampered", documents[0]["id"])
    )
    for operation in (index.status, index.prepare):
        with pytest.raises(LibraryError) as exc:
            operation(library, class_id, None)
        assert exc.value.code == "invalid_library"
    with pytest.raises(LibraryError):
        index.retrieve(library, class_id, "archive", None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-kill durability probe")
def test_killed_writer_leaves_previous_ready_cache_recoverable_and_sources_unchanged(ready):
    library, class_id, documents = ready
    old = cache_rows(library)
    originals = [library.original(class_id, d["id"]) for d in documents]
    program = """
import sys, time, struct
from pathlib import Path
from gct.local import embeddings, embedding_index
from gct.local.store import Library
embeddings.admit=lambda _: embeddings.ModelAssets(Path('.'), 'fixture-fingerprint')
class Encoder:
    def __init__(self, _): pass
    def stream(self, chunks, *, check):
        chunk_id, _=next(iter(chunks))
        yield chunk_id, 0, struct.pack('<384f', 0.0, 1.0, *([0.0]*382))
        print('cache-write-completed',flush=True)
        while True: time.sleep(1)
embeddings.Encoder=Encoder
with Library(sys.argv[1]) as library:
    embedding_index.prepare(library,sys.argv[2],None)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(library.path), class_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    try:
        readable, _, _ = select.select([process.stdout], [], [], 5)
        assert readable, "Synthetic writer failed to reach its transaction."
        assert process.stdout.readline().strip() == "cache-write-completed"
        process.send_signal(signal.SIGKILL)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            with contextlib.suppress(Exception):
                stream.close()
    # SQLite's normal write-capable reopen recovers its hot journal, if present.
    assert cache_rows(library) == old
    assert index.status(library, class_id, None)["state"] == "ready"
    assert [library.original(class_id, d["id"]) for d in documents] == originals
