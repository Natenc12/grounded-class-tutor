"""Optional, derivable semantic indexes; the canonical library remains authoritative.

A complete class is one atomic build. No partial, stale, wrong-model or corrupt
index can rank evidence. Sidecar failures fall back to lexical retrieval; canonical
library failures retain their normal error instead of becoming empty evidence.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import sqlite3
import stat
import struct
import time
from collections.abc import Iterator
from pathlib import Path

from gct.contracts import RetrievedChunk
from gct.local import embeddings, search
from gct.local.embeddings import EmbeddingError, EmbeddingLimit
from gct.local.store import Library, LibraryError

MAX_WINDOWS = 50_000
APPLICATION_ID = 0x47435445
VERSION = 1
_MESSAGES = {
    "unavailable": "Local semantic search is unavailable. Keyword search is available.",
    "missing": "Prepare this class for semantic search. Keyword search is available.",
    "stale": "This class changed. Prepare it again. Keyword search is available.",
    "ready": "Semantic and keyword search are ready for this class.",
    "limited": "This class exceeded the preparation limit. Keyword search is available.",
    "failed": "Semantic search could not finish. Keyword search is available.",
}
_CACHE_ERRORS = (EmbeddingError, OSError, sqlite3.Error, ValueError, TypeError, ImportError)


def _state(state: str) -> dict:
    return {
        "state": state,
        "mode": "hybrid" if state == "ready" else "lexical",
        "message": _MESSAGES[state],
    }


def _encoder(assets):
    try:
        return embeddings.Encoder(assets)
    except Exception as exc:
        raise EmbeddingError("Optional encoder could not load") from exc


def _encode(encoder, chunks, check):
    try:
        yield from encoder.stream(chunks, check=check)
    except (LibraryError, EmbeddingError):
        raise
    except Exception as exc:
        raise EmbeddingError("Optional encoder could not finish") from exc


def _check_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
        ):
            raise EmbeddingError("Unsafe semantic cache file")


@contextlib.contextmanager
def _cache(library: Library, *, create: bool = False) -> Iterator[sqlite3.Connection]:
    path = library.path.with_suffix(".embeddings.sqlite3")
    created = False
    if create:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(descriptor)
            created = True
        except FileExistsError:
            pass
    _check_file(path)
    for suffix in ("-journal", "-wal", "-shm"):
        auxiliary = Path(str(path) + suffix)
        if auxiliary.exists() or auxiliary.is_symlink():
            _check_file(auxiliary)
    connection = sqlite3.connect(
        path.as_uri() + ("?mode=rw" if create else "?mode=ro"),
        uri=True,
        isolation_level=None,
        timeout=2,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA trusted_schema=OFF")
        if created:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE indexes(class_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,"
                "snapshot TEXT NOT NULL,chunk_count INTEGER NOT NULL,"
                "window_count INTEGER NOT NULL,digest TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE vectors(class_id TEXT NOT NULL,position INTEGER NOT NULL,"
                "chunk_id TEXT NOT NULL,window INTEGER NOT NULL,embedding BLOB NOT NULL,"
                "PRIMARY KEY(class_id,position,window))"
            )
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={VERSION}")
            connection.commit()
        if (
            connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != VERSION
        ):
            raise EmbeddingError("Unrecognized semantic cache preserved")
        if connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise EmbeddingError("Semantic cache integrity failure")
        if create:
            connection.execute("PRAGMA synchronous=FULL")
        yield connection
    finally:
        connection.close()


def _chunks(library: Library, class_id: str):
    with library._errors():
        cursor = library.connection.execute(
            "SELECT c.id,c.document_id,c.page_or_slide,c.ordinal,d.filename,d.sha256,"
            "CASE WHEN typeof(c.text)='text' AND length(c.text)<=30000 "
            "THEN c.text ELSE NULL END AS text "
            "FROM chunks c JOIN documents d ON d.id=c.document_id WHERE d.class_id=? "
            "ORDER BY d.filename,d.sha256,c.page_or_slide,c.ordinal",
            (class_id,),
        )
        for row in cursor:
            text = row["text"]
            if not isinstance(text, str) or not text.strip():
                raise LibraryError(
                    "invalid_library", "The local library needs recovery before use."
                )
            identity = json.dumps(
                [row["document_id"], row["sha256"], row["page_or_slide"], row["ordinal"], text],
                ensure_ascii=True,
            ).encode()
            if hashlib.sha256(identity).hexdigest() != row["id"]:
                raise LibraryError(
                    "invalid_library", "The local library needs recovery before use."
                )
            yield row


def _snapshot(library: Library, class_id: str, check=lambda: None) -> tuple[str, int]:
    library._class(class_id)
    digest = hashlib.sha256(class_id.encode())
    with library._errors():
        documents = library.connection.execute(
            "SELECT id,filename,sha256,byte_count,page_count FROM documents "
            "WHERE class_id=? ORDER BY filename,sha256",
            (class_id,),
        )
        for document in documents:
            check()
            # Source bytes are canonical, bounded and hash-checked by the library.
            library.original(class_id, document["id"])
            digest.update(json.dumps(tuple(document), ensure_ascii=True).encode())
    count = 0
    for row in _chunks(library, class_id):
        check()
        digest.update(json.dumps(tuple(row), ensure_ascii=True).encode())
        count += 1
        if count > MAX_WINDOWS:
            raise EmbeddingLimit("Class exceeds complete-index limit")
    return digest.hexdigest(), count


def _vector(raw: bytes) -> tuple[float, ...]:
    if not isinstance(raw, bytes) or len(raw) != 384 * 4:
        raise EmbeddingError("Invalid cached vector size")
    values = struct.unpack("<384f", raw)
    if any(not math.isfinite(value) for value in values):
        raise EmbeddingError("Invalid cached vector values")
    if abs(math.fsum(value * value for value in values) - 1) > 0.002:
        raise EmbeddingError("Invalid cached vector norm")
    return values


def _vector_digest(digest, position, chunk_id, window, raw):
    digest.update(json.dumps([position, chunk_id, window]).encode())
    digest.update(raw)


def _header(connection, class_id, fingerprint, snapshot):
    header = connection.execute("SELECT * FROM indexes WHERE class_id=?", (class_id,)).fetchone()
    if header is None:
        return None, "missing"
    if header["fingerprint"] != fingerprint or header["snapshot"] != snapshot:
        return None, "stale"
    if (
        type(header["window_count"]) is not int
        or type(header["chunk_count"]) is not int
        or not 0 < header["chunk_count"] <= header["window_count"] <= MAX_WINDOWS
    ):
        raise EmbeddingError("Invalid semantic index counts")
    return header, "ready"


def _validated(connection, library, class_id, header):
    """Stream and join every window to canonical identity before accepting ranking."""
    cursor = iter(
        connection.execute(
            "SELECT position,chunk_id,window,CASE WHEN typeof(embedding)='blob' "
            "AND length(embedding)=1536 THEN embedding ELSE NULL END AS embedding "
            "FROM vectors WHERE class_id=? ORDER BY position,window",
            (class_id,),
        )
    )
    cached = next(cursor, None)
    digest = hashlib.sha256()
    count = chunks = 0
    for position, source in enumerate(_chunks(library, class_id)):
        windows = 0
        while cached is not None and cached["position"] == position:
            if (
                cached["chunk_id"] != source["id"]
                or cached["window"] != windows
                or windows >= embeddings.MAX_WINDOWS_PER_CHUNK
            ):
                raise EmbeddingError("Semantic cache source mismatch")
            raw = cached["embedding"]
            vector = _vector(raw)
            _vector_digest(digest, position, source["id"], windows, raw)
            windows += 1
            count += 1
            if count > MAX_WINDOWS:
                raise EmbeddingLimit("Semantic cache exceeds window limit")
            yield position, source, vector, raw
            cached = next(cursor, None)
        if windows == 0:
            raise EmbeddingError("Semantic cache lacks a source passage")
        chunks += 1
    if (
        cached is not None
        or count != header["window_count"]
        or chunks != header["chunk_count"]
        or digest.hexdigest() != header["digest"]
    ):
        raise EmbeddingError("Incomplete semantic cache")


def status(library: Library, class_id: str, model_root: str | Path | None) -> dict:
    library._class(class_id)
    try:
        assets = embeddings.admit(model_root)
    except _CACHE_ERRORS:
        return _state("unavailable")
    try:
        snapshot, count = _snapshot(library, class_id)
        if not count:
            return _state("missing")
        with _cache(library) as connection:
            header, state = _header(connection, class_id, assets.fingerprint, snapshot)
            if header is not None:
                for _ in _validated(connection, library, class_id, header):
                    pass
            return _state(state)
    except FileNotFoundError:
        return _state("missing")
    except EmbeddingLimit:
        return _state("limited")
    except _CACHE_ERRORS:
        return _state("failed")


def prepare(
    library: Library, class_id: str, model_root: str | Path | None, *, deadline_seconds: float = 210
) -> dict:
    library._class(class_id)
    if not isinstance(deadline_seconds, (int, float)) or not 0 < deadline_seconds <= 210:
        raise ValueError("Preparation deadline must be at most210seconds")
    deadline = time.monotonic() + deadline_seconds

    def check():
        if time.monotonic() >= deadline:
            raise EmbeddingLimit("Preparation deadline exceeded")

    try:
        assets = embeddings.admit(model_root)
    except _CACHE_ERRORS:
        return _state("unavailable")
    try:
        before, count = _snapshot(library, class_id, check)
        if not count:
            return _state("missing")
        encoder = _encoder(assets)
        check()
        with _cache(library, create=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM vectors WHERE class_id=?", (class_id,))
                digest = hashlib.sha256()
                window_count = 0
                position = -1
                previous = None

                def source_chunks():
                    for source in _chunks(library, class_id):
                        yield source["id"], source["text"]

                for chunk_id, window, raw in _encode(encoder, source_chunks(), check):
                    check()
                    if chunk_id != previous:
                        position += 1
                        previous = chunk_id
                    window_count += 1
                    if window_count > MAX_WINDOWS:
                        raise EmbeddingLimit("Class exceeds window limit")
                    _vector(raw)
                    _vector_digest(digest, position, chunk_id, window, raw)
                    connection.execute(
                        "INSERT INTO vectors VALUES(?,?,?,?,?)",
                        (class_id, position, chunk_id, window, raw),
                    )
                check()
                # Hold a short canonical read transaction through publication so a
                # concurrent import cannot race the final snapshot comparison.
                with library._errors():
                    library.connection.execute("BEGIN")
                try:
                    after, after_count = _snapshot(library, class_id, check)
                    if before != after or count != after_count or position + 1 != count:
                        connection.rollback()
                        return _state("stale")
                    connection.execute(
                        "INSERT OR REPLACE INTO indexes VALUES(?,?,?,?,?,?)",
                        (
                            class_id,
                            assets.fingerprint,
                            before,
                            count,
                            window_count,
                            digest.hexdigest(),
                        ),
                    )
                    header, _ = _header(connection, class_id, assets.fingerprint, before)
                    for _ in _validated(connection, library, class_id, header):
                        check()
                    check()
                    connection.commit()
                finally:
                    library.connection.rollback()
            except BaseException:
                connection.rollback()
                raise
        return _state("ready")
    except EmbeddingLimit:
        return _state("limited")
    except _CACHE_ERRORS:
        return _state("failed")


def _lexical(connection, class_id, question, document_id):
    expression = search.query_expression(question)
    if not expression:
        return []
    condition = " AND d.id=?" if document_id is not None else ""
    parameters = [expression, class_id, *([document_id] if document_id is not None else [])]
    return [
        row[0]
        for row in connection.execute(
            "SELECT c.id FROM chunks_fts JOIN chunks c ON c.rowid=chunks_fts.rowid "
            "JOIN documents d ON d.id=c.document_id WHERE chunks_fts MATCH ? AND d.class_id=?"
            + condition
            + " ORDER BY bm25(chunks_fts,1.0,0.35),d.filename,d.sha256,"
            "c.page_or_slide,c.ordinal LIMIT 50",
            parameters,
        )
    ]


def _cosine(raw, query):
    # Keep cosine arithmetic in the same float32 dtype as the stored embeddings.
    import numpy as np

    return float(np.frombuffer(raw, dtype="<f4") @ query)


def retrieve(
    library: Library,
    class_id: str,
    question: str,
    model_root: str | Path | None,
    document_id: str | None = None,
) -> tuple[list[RetrievedChunk] | None, dict]:
    library._class(class_id)
    if document_id is not None:
        library.document(class_id, document_id)
    # Validate ordinary user input before optional capability fallback.
    try:
        search.query_expression(question)
    except ValueError as exc:
        raise LibraryError("invalid_search", str(exc)) from exc
    try:
        assets = embeddings.admit(model_root)
    except _CACHE_ERRORS:
        return None, _state("unavailable")
    try:
        before, count = _snapshot(library, class_id)
        if not count:
            return None, _state("missing")
        with _cache(library) as connection:
            header, state = _header(connection, class_id, assets.fingerprint, before)
            if header is None:
                return None, _state(state)
            encoder = _encoder(assets)
            try:
                query = encoder.query(question)
            except Exception as exc:
                raise EmbeddingError("Optional query encoder could not finish") from exc
            best = []
            current = None
            score = -math.inf
            current_source = None

            def retain():
                if current_source is not None and (
                    document_id is None or current_source["document_id"] == document_id
                ):
                    best.append((-score, current, current_source["id"]))
                    best.sort()
                    del best[50:]

            for position, source, _, raw in _validated(connection, library, class_id, header):
                if position != current:
                    retain()
                    current = position
                    current_source = source
                    score = -math.inf
                if document_id is None or source["document_id"] == document_id:
                    score = max(score, _cosine(raw, query))
            retain()
            with library._errors():
                lexical = _lexical(library.connection, class_id, question, document_id)
            dense = [row[2] for row in best]
            fused = {}
            for ranking in (lexical, dense):
                for rank, chunk_id in enumerate(ranking, 1):
                    fused[chunk_id] = fused.get(chunk_id, 0) + 1 / (60 + rank)
            # At most100 candidate bodies; each body retains canonical identity.
            candidates = []
            for position, source in enumerate(_chunks(library, class_id)):
                if source["id"] in fused:
                    candidates.append((-fused[source["id"]], position, dict(source)))
            candidates.sort(key=lambda item: item[:2])
            selected = []
            length = 0
            for negative_score, _, source in candidates:
                block = (
                    f"[S{len(selected) + 1}] ({source['filename']}, "
                    f"p.{source['page_or_slide']})\n{source['text']}"
                )
                increment = len(block) + (2 if selected else 0)
                if length + increment > search.MAX_CONTEXT_CHARS:
                    continue
                selected.append(
                    RetrievedChunk(
                        source["id"],
                        source["text"],
                        source["filename"],
                        source["page_or_slide"],
                        -negative_score,
                    )
                )
                length += increment
                if len(selected) == 8:
                    break
            if _snapshot(library, class_id)[0] != before:
                return None, _state("stale")
            return selected, _state("ready")
    except FileNotFoundError:
        return None, _state("missing")
    except EmbeddingLimit:
        return None, _state("limited")
    except _CACHE_ERRORS:
        return None, _state("failed")
