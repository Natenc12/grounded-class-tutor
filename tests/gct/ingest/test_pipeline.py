"""Tests for `compose` and the `ingest_file` entry point (issue #4, ADR 0018 stamp / 0019 never-span).

`compose` is the no-DB half of the pipeline: parse -> chunk -> embed -> build `PreparedChunk`s.
Its tests use a real generated PDF (`pdf_factory`) and the deterministic `fake_embedder` stub, so
no network / no Postgres.

`ingest_file` composes that half with the index transaction, so its tests DO take the `db` fixture
and hit real Postgres - they live here, with the entry point they exercise, rather than in
test_index.py. What lives there is `index_file` itself: the atomic-write invariants, tested
directly. Splitting on the function under test rather than on "does it touch the DB" is why both
files have DB-backed tests.
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
    """An `Embeddings`-shaped stub whose `embed` always raises - the failure injection for the
    `ingest_file` tests that assert nothing downstream of `compose` ran (issue #42).

    Used two ways: as an injected FAILURE (an embed error must leave the DB untouched) and as a
    TRIPWIRE (a guard that fails fast must return before `embed` is ever reached). Both rely on the
    same property, that reaching `embed` is loud rather than silent."""

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


def test_ingest_file_uses_a_caller_supplied_file_id(pdf_factory, fake_embedder, db, db_other):
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

    The final readback uses `db_other`, a SEPARATE connection, because everything asserted through
    `conn` is true whether or not anything was committed - a connection sees its own uncommitted
    work. This test shipped green while publishing nothing for exactly that reason. `db_other` is
    what makes `conn.commit()` below load-bearing in a way an assertion can feel.
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
    # LOAD-BEARING, not tidiness. This INSERT leaves `conn` INTRANS, and `index_file`'s
    # `conn.transaction()` then issues a SAVEPOINT rather than BEGIN - releasing which commits
    # NOTHING (ADR 0025, and `ingest_file`'s own docstring). Every assertion below reads back on
    # this same connection, so they all pass on rows no other connection can see and the fixture's
    # teardown rollback then discards. Without this line the test is green in exactly the world it
    # exists to rule out. The real caller has the same obligation: the Slice 2 worker MUST COMMIT
    # ITS CLAIM before ingesting on the same connection (`components/ingestion-worker.md`), so
    # committing here is also what makes this a faithful stand-in for `enqueue` (#70).
    conn.commit()

    path = pdf_factory("lecture.pdf", ["alpha beta gamma", "delta epsilon"])

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

    # PUBLISHED, not merely computed. Every assertion above would hold on a savepoint that commits
    # nothing; this one is read through a different connection, which can only see committed rows.
    published = db_other.execute(
        "select status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert published is not None, (
        "the files row is invisible to other connections - nothing committed"
    )
    assert published[0] == "ready"
    assert (
        db_other.execute(
            "select count(*) from chunks where file_id = %s::uuid", (file_id,)
        ).fetchone()[0]
        == n_chunks
    )


def test_ingest_file_still_mints_a_file_id_when_none_is_supplied(
    pdf_factory, fake_embedder, db, db_other
):
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

    Reads back through `db_other` for the same reason its sibling does. This path has no seeded row
    and so no `conn.commit()` to forget, which is exactly why the check belongs here too: the thing
    being pinned is that `ingest_file` PUBLISHES, and nothing about that should depend on which
    branch of the upsert ran.
    """
    conn, owner_id, class_id = db
    path = pdf_factory("lecture.pdf", ["alpha beta gamma", "delta epsilon"])

    before = conn.execute("select count(*) from files where owner_id = %s", (owner_id,)).fetchone()[
        0
    ]
    assert before == 0
    # Ends the transaction that SELECT opened - a READ leaves `conn` INTRANS just as a write does,
    # and `index_file` would then get a savepoint and publish nothing (ADR 0025). `rollback` rather
    # than `commit` because there is nothing to save: the point is to return the connection to
    # IDLE, not to keep anything. This test had no INSERT to make the hazard obvious, which is
    # exactly why it went unnoticed until `db_other` read it back on a second connection.
    conn.rollback()

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

    # PUBLISHED, on a different connection - see the sibling test.
    published = db_other.execute(
        "select status from files where file_id = %s::uuid", (returned,)
    ).fetchone()
    assert published is not None, (
        "the files row is invisible to other connections - nothing committed"
    )
    assert published[0] == "ready"
    assert (
        db_other.execute(
            "select count(*) from chunks where file_id = %s::uuid", (returned,)
        ).fetchone()[0]
        == n_chunks
    )


def test_ingest_file_rejects_a_malformed_file_id_before_spending_anything(pdf_factory):
    """A `file_id` that isn't a uuid fails INSTANTLY - no parse, no embed, no DB.

    Postgres rejects a malformed id on its own, at `index_file`'s `::uuid` cast. That is a real
    check in the wrong place: `compose` has already run by then, so a typo costs a full embedding
    run before anyone finds out. The defect was ordering, not detection, so this test pins the
    ORDER rather than the message.

    It proves that by making the later steps fatal instead of merely expensive. `conn=None` cannot
    be executed against and `_ExplodingEmbedder` raises on use, so this test can only pass if the
    guard returns before either is touched - a `ValueError` here means nothing downstream ran. Move
    the guard below `compose` and the `RuntimeError` escapes instead, which is why this asserts on
    the exception type and not merely that something raised.
    """
    path = pdf_factory("lecture.pdf", ["alpha beta gamma"])

    with pytest.raises(ValueError, match="well-formed uuid"):
        ingest_file(
            path,
            "owner",
            "class",
            embedder=_ExplodingEmbedder(),
            conn=None,
            file_id="not-a-uuid",
        )


def test_ingest_file_refuses_a_file_id_belonging_to_another_owner(pdf_factory, fake_embedder, db):
    """Supplying an id that names SOMEONE ELSE'S row raises, and writes nothing.

    The third row of the supplied-`file_id` table, and the only one where the upsert's indifference
    is observable: it sets `status`/`updated_at` only, so the existing row keeps its `owner_id`
    while step 3 writes chunks under the caller's. That leaves a `files` row scoped to one tenant
    owning chunks scoped to another, and retrieval filters on `owner_id AND class_id` (F6/F12) -
    so the chunks would answer for a student who never uploaded the file.

    `test_ingest_file_uses_a_caller_supplied_file_id` covers the same UPDATE branch at the one spot
    where this cannot show: it passes the same owner it seeded, and keeping the old value and
    writing the new one are indistinguishable when they are equal.

    Seeded under the fixture's `owner_id` (so teardown reclaims it) and ingested as a DIFFERENT
    owner - the direction a Slice 2 worker would get it wrong, carrying scope from somewhere other
    than the row it claimed. The status assertion is the load-bearing one: `queued` still `queued`
    proves the guard fired before step 1's upsert, where a raise from any later step would have
    left it too.
    """
    conn, owner_id, class_id = db

    file_id = str(uuid4())
    conn.execute(
        """
        insert into files (file_id, owner_id, class_id, filename, status)
        values (%s::uuid, %s, %s::uuid, %s, 'queued')
        """,
        (file_id, owner_id, class_id, "lecture.pdf"),
    )
    conn.commit()  # ADR 0025 - see the sibling test; without it nothing below is published.

    path = pdf_factory("lecture.pdf", ["alpha beta gamma", "delta epsilon"])

    with pytest.raises(ValueError, match="different owner/class"):
        ingest_file(
            path,
            f"{owner_id}-someone-else",
            class_id,
            embedder=fake_embedder,
            conn=conn,
            file_id=file_id,
        )

    # Untouched: still `queued`, never advanced to `ready` by the upsert.
    status = conn.execute(
        "select status from files where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert status == "queued"

    # And no chunks were written under it - the whole transaction rolled back.
    n_chunks = conn.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()[0]
    assert n_chunks == 0
