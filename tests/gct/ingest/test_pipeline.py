"""Unit tests for the pure compose stage (issue #4, ADR 0018 stamp / 0019 never-span).

`compose` is the no-DB half of the pipeline: parse -> chunk -> embed -> build `PreparedChunk`s.
Tested with a real generated PDF (`pdf_factory`) and the deterministic `fake_embedder` stub, so no
network / no Postgres. The DB-backed `index_file`/`ingest_file` tests live elsewhere
(test_index.py).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS, chunk_units
from gct.ingest.parse import ParseError, parse_file
from gct.ingest.pipeline import PreparedChunk, compose, ingest_file

OWNER_ID = "test-owner"
CLASS_ID = "test-class"


def test_compose_stamps_provenance_model_and_embeds_all(pdf_factory, fake_embedder):
    """One `PreparedChunk` per `TextChunk`, in order, with provenance + scope + stamp + embedding.

    Two short pages -> one chunk each (text under `CHUNK_SIZE_WORDS`). Compares compose's output
    directly against the real `chunk_units(parse_file(...))` so the alignment (order + provenance)
    is proven against the actual chunker, not a hand-guessed count.
    """
    path = pdf_factory("lecture.pdf", ["alpha beta gamma", "delta epsilon"])
    expected_chunks = chunk_units(parse_file(path))

    prepared = compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder)

    assert len(prepared) == len(expected_chunks)
    for p, c in zip(prepared, expected_chunks, strict=True):
        assert isinstance(p, PreparedChunk)
        # Provenance carried through unchanged (citation spine, honor-point ①).
        assert p.text == c.text
        assert p.file == c.file
        assert p.page_or_slide == c.page_or_slide
        # Scope stamped on every row (F6/F12 isolation).
        assert p.owner_id == OWNER_ID
        assert p.class_id == CLASS_ID
        # Model id sourced from the embedder that produced the vectors (ADR 0018), not hardcoded.
        assert p.embedding_model_id == fake_embedder.model_id
        # Right dimension, and the vector is exactly the one the embedder returns for THIS text
        # (proves per-chunk order alignment, not just count).
        assert len(p.embedding) == fake_embedder.dim
        assert p.embedding == fake_embedder.embed([p.text])[0]


def test_compose_forwards_the_chunk_window(pdf_factory, fake_embedder):
    """`chunk_size`/`chunk_overlap` reach `chunk_units` — the pass-through is real, not decorative.

    The failure this exists for has no other symptom: a `compose` that accepts the window and then
    calls `chunk_units(...)` with the defaults still returns well-formed `PreparedChunk`s, still
    embeds and stamps them correctly, and passes every other test in this file. The spike (ADR
    0019 / 0021) would then retune the window, observe nothing change, and read that as evidence
    about chunking rather than about the wiring.

    Counts are compared against the real `chunk_units` at the SAME window (the convention
    `test_compose_stamps_provenance_model_and_embeds_all` sets) rather than hand-guessed, and both
    windows are chosen to differ from the defaults so a silent fallback cannot pass.
    """
    path = pdf_factory("lecture.pdf", [" ".join(f"w{i}" for i in range(600))])
    units = parse_file(path)

    wide = compose(
        path, OWNER_ID, CLASS_ID, embedder=fake_embedder, chunk_size=400, chunk_overlap=50
    )
    narrow = compose(
        path, OWNER_ID, CLASS_ID, embedder=fake_embedder, chunk_size=100, chunk_overlap=20
    )
    default = compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder)

    assert len(wide) == len(chunk_units(units, size=400, overlap=50))
    assert len(narrow) == len(chunk_units(units, size=100, overlap=20))
    # Three distinct counts: neither override collapsed onto the other or onto the default.
    assert len(narrow) > len(wide)
    assert len({len(wide), len(narrow), len(default)}) == 3
    # The chunks are genuinely re-cut, not merely re-counted — a narrow window's first chunk holds
    # fewer words than a wide one's, and every row is still a fully-stamped PreparedChunk.
    assert len(narrow[0].text.split()) < len(wide[0].text.split())
    assert all(p.embedding_model_id == fake_embedder.model_id for p in narrow)


def test_compose_defaults_are_the_module_constants(pdf_factory, fake_embedder):
    """An omitted window must equal an explicit one at the constants — pins `compose`'s OWN
    defaults, which the forwarding test above cannot: it compares compose against `chunk_units`
    at the same window, so a default that drifted off its constant would cancel out on both
    sides. `PreparedChunk` is frozen and the fake embedder is deterministic, so whole-row
    equality is exact."""
    path = pdf_factory("lecture.pdf", [" ".join(f"w{i}" for i in range(600))])

    assert compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder) == compose(
        path,
        OWNER_ID,
        CLASS_ID,
        embedder=fake_embedder,
        chunk_size=CHUNK_SIZE_WORDS,
        chunk_overlap=CHUNK_OVERLAP_WORDS,
    )


def test_compose_propagates_parse_error_on_empty_file(pdf_factory, fake_embedder):
    """A zero-text file raises `ParseError` from `parse_file`, and compose lets it fly untouched
    (terminal-failure *handling* is Slice 2's job, ADR 0020)."""
    path = pdf_factory("blank.pdf", [None])  # genuinely blank page, no text drawn

    with pytest.raises(ParseError):
        compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder)


class _ExplodingEmbedder:
    """An `Embeddings`-shaped stub whose `embed` always raises - the failure injection for
    `test_ingest_file_embed_failure_leaves_db_untouched` below (issue #42)."""

    model_id = "exploding-embedder"
    dim = 1

    def embed(self, texts):
        raise RuntimeError("embedder exploded")


def test_ingest_file_embed_failure_leaves_db_untouched(pdf_factory, db):
    """ADR 0020's property ("an embed failure leaves the DB untouched") holds only by STATEMENT
    ORDER inside `ingest_file`: `compose` (embed included) is pure and fully precedes the only
    transaction (`index_file`, in `index.py`) - nothing currently goes red if that ordering ever
    breaks. It's ONE OF TWO shapes a Slice-2 worker will be tempted to break, e.g. pre-creating the
    `files` row as `processing` before `compose` runs. An embedder that raises must leave zero
    `files` rows AND zero `chunks` rows for this owner - if a future worker pre-writes a row before
    `compose`, this goes red.

    SCOPE - this pins the WRITE-ORDER half only. The other half is the caller's connection state
    (ADR 0020, publication claim amended per ADR 0025): a worker that leases a job and then ingests
    on the SAME connection gets a savepoint rather than a transaction, so `ingest_file` returns
    reporting success while having published nothing. ADR 0025 records why no single-connection test
    can catch that - this one included. A green here is not cover for it."""
    conn, owner_id, class_id = db
    path = pdf_factory("lecture.pdf", ["alpha beta gamma"])

    with pytest.raises(RuntimeError):
        ingest_file(path, owner_id, class_id, embedder=_ExplodingEmbedder(), conn=conn)

    files_count = conn.execute(
        "select count(*) from files where owner_id = %s", (owner_id,)
    ).fetchone()[0]
    chunks_count = conn.execute(
        "select count(*) from chunks where owner_id = %s", (owner_id,)
    ).fetchone()[0]
    assert files_count == 0
    assert chunks_count == 0


def test_ingest_file_end_to_end(pdf_factory, fake_embedder, db):
    """The top-level Slice-1 entry: a real file goes through parse→chunk→embed→index and lands as a
    queryable, `ready` file. Returns the minted `file_id`; chunk rows are present under it."""
    conn, owner_id, class_id = db
    path = pdf_factory("lecture.pdf", ["alpha beta gamma", "delta epsilon"])

    file_id = ingest_file(path, owner_id, class_id, embedder=fake_embedder, conn=conn)

    assert isinstance(file_id, str)
    status = conn.execute(
        "select status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert status == "ready"

    n_chunks = conn.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    # One chunk per short page (text under CHUNK_SIZE_WORDS) — matches the real chunker.
    assert n_chunks == len(chunk_units(parse_file(path)))


def test_ingest_file_forwards_the_chunk_window(pdf_factory, fake_embedder, db):
    """The window survives the SECOND hop too — `ingest_file` -> `compose` -> `chunk_units` — and
    the retuned chunk set is what actually lands in the DB.

    `test_compose_forwards_the_chunk_window` proves the inner hop; this one is here because
    `ingest_file` is the seam Slice 2's worker wraps (ADR 0020), and a worker that passes a tuned
    window down through it would otherwise index default-sized chunks while believing otherwise —
    invisible until someone counted rows.
    """
    conn, owner_id, class_id = db
    path = pdf_factory("lecture.pdf", [" ".join(f"w{i}" for i in range(600))])
    units = parse_file(path)

    file_id = ingest_file(
        path,
        owner_id,
        class_id,
        embedder=fake_embedder,
        conn=conn,
        chunk_size=100,
        chunk_overlap=20,
    )

    n_chunks = conn.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert n_chunks == len(chunk_units(units, size=100, overlap=20))
    # Differs from what the defaults would have written, so a dropped parameter goes red here.
    assert n_chunks != len(chunk_units(units))


# --- Fixture reliability (issue #23) ----------------------------------------------------------


def test_fake_embedder_is_stable_across_processes(fake_embedder):
    """`FakeEmbeddings` maps a text to the SAME vector in every process, forever.

    It previously seeded from `hash(text) % 1000`. `hash()` is randomized per process, and the
    modulo collapsed the space to 1000 buckets — so two distinct chunk texts collided on roughly
    0.1% of pairs, and when they did, the alignment assertion in
    `test_compose_stamps_provenance_model_and_embeds_all` passed VACUOUSLY. That flake is worst
    exactly while the suite is being leaned on daily, which is why #23 replaced it with sha256.

    Assert against a hard-coded expected value (not just self-consistency within one run): a future
    "improvement" that reintroduces per-process randomization must fail here, loudly.
    """
    # sha256(b"hello world")[:6] as a big-endian int — computed once, hardcoded here so a future
    # regression to a randomized/process-local hash fails loudly instead of just staying
    # "consistent".
    vector = fake_embedder.embed(["hello world"])[0]
    assert vector[0] == 203741030093645.0
    assert vector[1:] == [0.0] * (fake_embedder.dim - 1)


def test_ingest_file_uses_a_caller_supplied_file_id(pdf_factory, fake_embedder, db):
    """A supplied `file_id` ingests INTO the row that already exists - one file stays one row.

    The Slice 2 worker claims a job whose `files` row was created `queued` before the claim, so it
    passes that row's id down. Without the seam `ingest_file` mints its own, and the `queued` row
    the student polls is stranded while the chunks land under a second id - no error, no log, an
    upload that appears to hang forever.

    The hand-written INSERT stands in for `enqueue` (#70), which does not exist yet; it is the only
    thing here pretending. This test drives `index_file`'s upsert onto its UPDATE branch, the
    counterpart to the INSERT branch every no-`file_id` caller takes.

    The two load-bearing assertions are the returned id and the row COUNT. Asserting only that a
    `ready` row exists would pass in the broken world too - there is a `ready` row there, it is just
    the wrong one.
    """
    conn, owner_id, class_id = db

    # Stand-in for enqueue: the row the worker's job points at, already `queued` before ingest runs.
    file_id = str(uuid4())
    conn.execute(
        """
        insert into files (file_id, owner_id, class_id, filename, status)
        values (%s::uuid, %s, %s::uuid, %s, 'queued')
        """,
        (file_id, owner_id, class_id, "lecture.pdf"),
    )

    path = pdf_factory("lecture.pdf", ["alpha beta gamme", "delta epsilon"])

    returned = ingest_file(
        path, owner_id, class_id, embedder=fake_embedder, conn=conn, file_id=file_id
    )
    # Return contract: the id supplied comes back, never a freshly minted one.
    assert returned == file_id

    # The row was ADVANCED, not duplicated. `owner_id` is unique per test (the `db` fixture), so
    # this counts only this test's rows - a second, minted row would make it 2.
    n_files = conn.execute(
        "select count(*) from files where owner_id = %s", (owner_id,)
    ).fetchone()[0]
    assert n_files == 1

    # `queued` -> `ready` on that same row: the upsert's UPDATE branch fired.
    status = conn.execute(
        "select status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert status == "ready"

    # And the content hangs off the id the caller supplied, not off one nobody is watching.
    n_chunks = conn.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert n_chunks > 0


def test_ingest_file_still_mints_a_file_id_when_none_is_supplied(pdf_factory, fake_embedder, db):
    """Omitting `file_id` mints one and CREATES the row - Slice 1's path, provably unchanged.

    Deliberately overlaps `test_ingest_file_end_to_end`, which has always exercised this path
    incidentally. What that test does not say is that the default is load-bearing: make `file_id`
    required and every Slice 1 caller breaks, but its failure reads as an incidental break to fix
    by passing an id. Named for the guarantee, this one cannot be read that way.

    It is also the other half of the pair. `test_ingest_file_uses_a_caller_supplied_file_id` drives
    the upsert's UPDATE branch; this drives INSERT. Which branch runs is decided entirely by whether
    the caller owns the id, so the two together pin both sides of that fork.

    The before/after count is what makes "created" a real claim rather than "a row exists" - the
    row must not have been there beforehand.
    """
    conn, owner_id, class_id = db
    path = pdf_factory("lecture.pdf", ["alpha beta gamma", "delta epsilon"])

    before = conn.execute("select count(*) from files where owner_id = %s", (owner_id,)).fetchone()[
        0
    ]
    assert before == 0

    returned = ingest_file(path, owner_id, class_id, embedder=fake_embedder, conn=conn)

    # Minted, not echoed: a well-formed uuid the caller never saw. UUID() raises on anything else,
    # which matters because the `files.file_id` column would reject it downstream anyway.
    assert UUID(returned)

    # Exactly one row, and it is the one just minted - the INSERT branch, not an UPDATE of something
    # that was already there.
    n_files = conn.execute(
        "select count(*) from files where owner_id = %s", (owner_id,)
    ).fetchone()[0]
    assert n_files == 1

    status = conn.execute(
        "select status from files where file_id = %s::uuid", (returned,)
    ).fetchone()[0]
    assert status == "ready"

    n_chunks = conn.execute(
        "select count(*) from chunks where file_id = %s::uuid", (returned,)
    ).fetchone()[0]
    assert n_chunks > 0
