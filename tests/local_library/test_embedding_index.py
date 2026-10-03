import sqlite3
import struct
from pathlib import Path

import pytest

from gct.ingest.parse import ParsedUnit
from gct.local import embedding_index as index
from gct.local import embeddings
from gct.local.store import Library, LibraryError


@pytest.fixture
def library(tmp_path):
    with Library(tmp_path / "library.sqlite3") as library:
        yield library


@pytest.fixture
def fake_model(monkeypatch):
    raw = struct.pack("<384f", 1, *([0] * 383))

    class Encoder:
        def __init__(self, assets):
            pass

        def stream(self, chunks, check):
            for chunk_id, _ in chunks:
                check()
                yield chunk_id, 0, raw

        def query(self, question):
            return None

    monkeypatch.setattr(
        embeddings, "admit", lambda _: embeddings.ModelAssets(Path("unused"), "model-a")
    )
    monkeypatch.setattr(embeddings, "Encoder", Encoder)
    monkeypatch.setattr(index, "_cosine", lambda raw, query: 1)
    return Encoder


def add(library, class_id, name="course.pdf", text="Cedar observatory closes at six."):
    return library.import_document(class_id, name, text.encode(), [ParsedUnit(text, name, 1)], 1)


def test_prepare_ready_scope_and_stale_fallback(library, fake_model):
    first = library.create_class("first")["id"]
    second = library.create_class("second")["id"]
    doc = add(library, first)
    add(library, second, "course.pdf", "Foreign clock closes at nine.")
    assert index.status(library, first, None)["state"] == "missing"
    assert index.prepare(library, first, None)["state"] == "ready"
    result, status = index.retrieve(library, first, "When does Cedar close?", None)
    assert status["mode"] == "hybrid"
    assert len(result) == 1 and result[0].text == "Cedar observatory closes at six."
    assert index.status(library, second, None)["state"] == "missing"
    with pytest.raises(LibraryError):
        index.retrieve(library, second, "clock", None, doc["id"])
    add(library, first, "next.pdf", "Cedar appointments take three minutes.")
    assert index.status(library, first, None)["state"] == "stale"
    assert index.retrieve(library, first, "Cedar", None)[0] is None
    cache = library.path.with_suffix(".embeddings.sqlite3")
    assert cache.stat().st_mode & 0o777 == 0o600
    assert library.connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_failed_build_rolls_back_old_ready_generation(library, fake_model, monkeypatch):
    class_id = library.create_class("class")["id"]
    add(library, class_id)
    assert index.prepare(library, class_id, None)["state"] == "ready"

    def fail(self, chunks, check):
        yield next(iter(chunks))[0], 0, struct.pack("<384f", 1, *([0] * 383))
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(fake_model, "stream", fail)
    assert index.prepare(library, class_id, None)["state"] == "failed"
    assert index.status(library, class_id, None)["state"] == "ready"


def test_cache_failure_falls_back_but_canonical_failure_propagates(library, fake_model):
    class_id = library.create_class("class")["id"]
    document = add(library, class_id)
    assert index.prepare(library, class_id, None)["state"] == "ready"
    cache = library.path.with_suffix(".embeddings.sqlite3")
    with sqlite3.connect(cache) as connection:
        connection.execute("UPDATE vectors SET embedding=?", (b"bad",))
    assert index.retrieve(library, class_id, "Cedar", None)[1]["state"] == "failed"
    library.connection.execute(
        "UPDATE documents SET original=? WHERE id=?", (b"bad", document["id"])
    )
    with pytest.raises(LibraryError):
        index.status(library, class_id, None)


def test_wrong_fingerprint_and_unrecognized_file_preserved(library, fake_model, monkeypatch):
    class_id = library.create_class("class")["id"]
    add(library, class_id)
    cache = library.path.with_suffix(".embeddings.sqlite3")
    cache.write_bytes(b"keep this unrelated file")
    cache.chmod(0o600)
    assert index.prepare(library, class_id, None)["state"] == "failed"
    assert cache.read_bytes() == b"keep this unrelated file"
    cache.unlink()
    assert index.prepare(library, class_id, None)["state"] == "ready"
    monkeypatch.setattr(
        embeddings, "admit", lambda _: embeddings.ModelAssets(Path("unused"), "model-b")
    )
    assert index.status(library, class_id, None)["state"] == "stale"


def test_bound_and_deadline_leave_no_partial_ready_index(library, fake_model, monkeypatch):
    class_id = library.create_class("class")["id"]
    add(library, class_id)
    monkeypatch.setattr(index, "MAX_WINDOWS", 0)
    assert index.prepare(library, class_id, None)["state"] == "limited"
    monkeypatch.setattr(index, "MAX_WINDOWS", 50_000)
    ticks = iter([0, 211])
    monkeypatch.setattr(index.time, "monotonic", lambda: next(ticks))
    assert index.prepare(library, class_id, None)["state"] == "limited"


def test_missing_model_retains_base_runtime(library):
    class_id = library.create_class("class")["id"]
    add(library, class_id)
    assert index.status(library, class_id, None)["state"] == "unavailable"
    result, state = index.retrieve(library, class_id, "Cedar", None)
    assert result is None and state["mode"] == "lexical"
    assert library.retrieve(class_id, "Cedar")
