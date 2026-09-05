"""Tests for `compose` and `ingest_file` (issue #4, ADR 0018 stamp / 0019 never-span).

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

import inspect
from uuid import UUID, uuid4

import pytest

from gct.config import MAX_INGEST_WORDS
from gct.ingest.chunk import CHUNK_OVERLAP_WORDS, CHUNK_SIZE_WORDS, chunk_units
from gct.ingest.parse import TERMINAL_REASONS, ParseError, parse_file
from gct.ingest.pipeline import PreparedChunk, compose, ingest_file
from gct.providers.base import TransientEmbeddingError
from gct.retriever.retrieve import retrieve

OWNER_ID = "test-owner"
# A REAL uuid, not a readable stand-in like the owner beside it. `compose` never looks at this
# value, but `ingest_file` canonicalises it before spending anything (#126), so a non-uuid here
# would make every ingest_file test in this module fail at the guard instead of where it is
# aimed. `owner_id` stays a plain string because it is a `text` column with no cast anywhere.
CLASS_ID = "6f1e1b4a-0f0e-4a3e-9a7d-2f0a1b2c3d4e"


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
    transaction (`index_file`, in `index.py`). THIS test is what pins that order: an embedder that
    raises must leave zero `files` rows AND zero `chunks` rows for this owner, so an `ingest_file`
    that ever wrote a row before `compose` goes red here.

    What it CANNOT see is the worker path. There the `files` row already exists before
    `ingest_file` runs - `enqueue` writes it `queued` and the worker stamps it `processing` - and
    an embed failure must leave that row in place for the retry. This test calls `ingest_file`
    directly with no prior row, so no change to the worker can reach it;
    `tests/gct/jobs/test_worker.py` owns that path
    (`test_a_transient_failure_backs_off_then_requeues_the_job`).

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

    The hand-written INSERT stands in for `enqueue` (`gct.jobs.queue`), which is what creates the
    `queued` row in production; it is the only thing here pretending. It is written by hand rather
    than calling `enqueue` so this test stays a test of `ingest_file`'s seam alone - calling the
    real writer would couple it to the queue's own contract and to the class row `enqueue`'s
    foreign key requires. This test drives `index_file`'s upsert onto its UPDATE branch, the
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
    # LOAD-BEARING, not tidiness. This INSERT leaves `conn` INTRANS - the state in which
    # `index_file` once degraded to a SAVEPOINT publishing nothing, and now refuses outright
    # (ADR 0025, guarded per ADR 0027): without this commit, `require_idle` reddens this test.
    # The real caller has the same obligation: the Slice 2 worker MUST COMMIT ITS CLAIM before
    # ingesting on the same connection (`components/ingestion-worker.md`), so committing here is
    # also what makes this a faithful stand-in for `enqueue` (#70).
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
    # and `index_file` refuses an INTRANS connection (ADR 0025, guarded per ADR 0027; it once
    # degraded to a savepoint publishing nothing, which is how this test shipped green while
    # writing nothing). `rollback` rather than `commit` because there is nothing to save: the
    # point is to return the connection to IDLE, not to keep anything.
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

    A malformed id is rejected downstream on its own - by `index_file`'s strict guard since #126,
    and by Postgres's `::uuid` cast before that. That is a real check in the wrong place: `compose`
    has already run by then, so a typo costs a full embedding run before anyone finds out. The
    defect was ordering, not detection, so this test pins the ORDER rather than the message.

    It proves that by making the later steps fatal instead of merely expensive. `conn=None` cannot
    be executed against and `_ExplodingEmbedder` raises on use, so this test can only pass if the
    guard returns before either is touched - a `ValueError` here means nothing downstream ran. Move
    the guard below `compose` and the `RuntimeError` escapes instead, which is why this asserts on
    the exception type and not merely that something raised.
    """
    path = pdf_factory("lecture.pdf", ["alpha beta gamma"])

    with pytest.raises(ValueError, match="canonical uuid"):
        ingest_file(
            path,
            "owner",
            "class",
            embedder=_ExplodingEmbedder(),
            conn=None,
            file_id="not-a-uuid",
        )

    # The other shape the guard must refuse WELL: psycopg returns uuid columns as `uuid.UUID`
    # instances, so this is the value a Slice 2 worker naturally holds after reading its job.
    # The guard owes it the same documented ValueError - whose message says the fix, pass
    # str(id) - not an incidental AttributeError from inside `UUID()`.
    with pytest.raises(ValueError, match="canonical uuid"):
        ingest_file(
            path,
            "owner",
            "class",
            embedder=_ExplodingEmbedder(),
            conn=None,
            file_id=uuid4(),
        )


# --- The ingest input ceiling (issue #43; ADR 0020, terminal set extended per ADR 0029) --------


def test_compose_rejects_input_past_the_word_ceiling_before_embedding(pdf_factory):
    """Past the ceiling, `compose` raises terminal `ParseError("too_long")` and NEVER embeds.

    Embedding is the only paid call in this pipeline, so a ceiling that fires after it bounds
    nothing - the guard's whole value is its POSITION. `_ExplodingEmbedder` is the tripwire:
    reaching `embed` raises `RuntimeError`, so a `ParseError` escaping here is proof the guard
    returned first. That is why this asserts the exception TYPE rather than merely that something
    raised - move the check below `embed` and the `RuntimeError` escapes instead.
    """
    path = pdf_factory("huge.pdf", [" ".join(f"w{i}" for i in range(300))])

    with pytest.raises(ParseError) as exc_info:
        compose(path, OWNER_ID, CLASS_ID, embedder=_ExplodingEmbedder(), max_words=100)

    assert exc_info.value.reason == "too_long"


def test_the_word_ceiling_is_configurable_and_inclusive_at_the_limit(pdf_factory, fake_embedder):
    """A file AT the ceiling ingests normally; the same file one word over is refused.

    Two facts in one fixture, because they share a measurement. First, the knob is real: the same
    input passes or fails purely on the value handed in. Second, the boundary - `>` and `>=` differ
    on exactly one input, and nothing else in this file distinguishes them. The word count is taken
    from the real `parse_file` output rather than from the string written into the PDF, so the test
    measures what the guard measures instead of assuming the two agree.
    """
    path = pdf_factory("lecture.pdf", [" ".join(f"w{i}" for i in range(120))])
    n_words = sum(len(u.text.split()) for u in parse_file(path))

    at_limit = compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder, max_words=n_words)
    assert at_limit == compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder)

    with pytest.raises(ParseError) as exc_info:
        compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder, max_words=n_words - 1)
    assert exc_info.value.reason == "too_long"


@pytest.mark.parametrize("func", [compose, ingest_file], ids=["compose", "ingest_file"])
def test_default_ceiling_is_the_config_constant(func):
    """Both entry points default `max_words` to `MAX_INGEST_WORDS` - not a second copy of it.

    BOTH, because the two defaults are not interchangeable and the one that matters in production
    is `ingest_file`'s. `ingest_file` always forwards `max_words=` explicitly, so `compose`'s
    default is dead on the production path; the worker (`jobs/worker.py`'s `process_one`) calls
    `ingest_file` WITHOUT `max_words`, so `ingest_file`'s default is the ceiling every real upload
    is actually measured against. Pinning only `compose` leaves the live number unguarded: raise
    `ingest_file`'s default to 999_999_999 and every other test in this suite still passes, because
    each one either passes `max_words=` explicitly or is far too small to reach any ceiling.

    Asserted on the signature rather than behaviorally on purpose - but not for the reason it is
    tempting to give. A 250,000-word fixture is cheap to BUILD (~0.5s of reportlab) and the round
    trip through `parse_file` + `compose` measures ~6s, which is more than the whole `not live`
    suite costs today. The real argument is the second one: a default that had drifted to some
    OTHER large number would still pass every ordinary-sized test in this file silently, so the
    identity is the fact worth pinning. Pin the identity, in both places.
    """
    default = inspect.signature(func).parameters["max_words"].default

    assert default == MAX_INGEST_WORDS


def test_ingest_file_rejects_past_the_ceiling_before_the_embedder_or_the_db(pdf_factory):
    """The ceiling holds at `ingest_file`, the seam Slice 2's worker wraps (PM-4, ADR 0020) - not
    only inside `compose`.

    Same tripwire discipline as `test_ingest_file_rejects_a_malformed_file_id_before_spending
    _anything`: `conn=None` cannot be executed against and `_ExplodingEmbedder` raises on use, so a
    `ParseError` escaping proves neither the paid call nor the DB was reached. The reason is
    asserted too, because it is the value `worker.py`'s `except ParseError` writes straight into
    `files.failed_reason` with no translation - a wrong one there is a CHECK violation at runtime.
    """
    path = pdf_factory("huge.pdf", [" ".join(f"w{i}" for i in range(300))])

    with pytest.raises(ParseError) as exc_info:
        ingest_file(
            path,
            OWNER_ID,
            CLASS_ID,
            embedder=_ExplodingEmbedder(),
            conn=None,
            max_words=100,
        )

    assert exc_info.value.reason == "too_long"


@pytest.mark.parametrize("reason", TERMINAL_REASONS)
def test_every_terminal_reason_is_a_legal_failed_reason(reason, db, db_other):
    """`TERMINAL_REASONS` and `files.failed_reason`'s CHECK are one fact stored in two places -
    this is the only thing holding them together.

    `too_long` is the case this test was added for (issue #43): a new terminal reason is a schema
    change, not a constant, and without migration 0003 it is a `CheckViolation`. But the mirror is
    the durable hazard - `worker.py` writes `exc.reason` into the column UNTRANSLATED, so any value
    Python can raise and Postgres will not store is a job that fails while recording why it failed,
    surfacing as a psycopg error from inside the bury transaction rather than as the actionable
    status the student is waiting on. Parametrized over the tuple so the next reason added to
    either side without the other goes red on arrival.

    Read back through `db_other`, a SEPARATE connection: a CHECK is evaluated per statement, so an
    uncommitted insert proves the value parsed, not that it survives a commit - and `db`'s own
    connection cannot tell those apart.
    """
    conn, owner_id, class_id = db
    file_id = str(uuid4())

    conn.execute(
        """
        insert into files (file_id, owner_id, class_id, filename, status, failed_reason)
        values (%s::uuid, %s, %s::uuid, %s, 'failed', %s)
        """,
        (file_id, owner_id, class_id, "huge.pdf", reason),
    )
    conn.commit()

    published = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert published == ("failed", reason)


# --- Ceiling hardening: boundary, blast radius, blind spot (issue #43 follow-up) ---------------
#
# The block above pins that the ceiling EXISTS and fires before `embed`. This block pins what it
# does at its edges, what it leaves behind when it fires, and - the entry that matters most - what
# it cannot see at all.


@pytest.mark.parametrize(
    ("offset", "refused"),
    [(1, False), (0, False), (-1, True)],
    ids=["one-under-the-count", "exactly-at-the-count", "one-over-the-count"],
)
def test_the_ceiling_is_inclusive_at_the_limit(offset, refused, pdf_factory, counting_embedder):
    """Three-point sweep across the boundary: `max_words = n+1`, `n`, `n-1` for an `n`-word file.

    The guard is `total_words > max_words`, so a file EXACTLY at the ceiling is accepted and the
    first refused input is one word past it. `>` and `>=` differ on exactly one of these three
    rows and agree on the other two, which is why all three are here rather than just the one
    that fails: a sweep that only shows the refusal cannot tell you which side of the boundary it
    is on. Parametrized on an OFFSET applied to the file's own measured word count, not on three
    literal numbers, so the fixture and the boundary can never drift apart.

    `n` is taken from `parse_file`'s output rather than from the string handed to `pdf_factory`,
    because the guard counts what the PARSER produced - a PDF round trip is free to add or drop
    whitespace, and a test that counted the input string would be asserting about reportlab.

    The embedder is counted on both sides: the accepted rows must have bought exactly one batch
    and the refused row exactly zero. An accept that quietly skipped `embed` would otherwise pass
    a test named for the boundary.
    """
    path = pdf_factory("lecture.pdf", [" ".join(f"w{i}" for i in range(120))])
    n_words = sum(len(u.text.split()) for u in parse_file(path))

    if refused:
        with pytest.raises(ParseError) as exc_info:
            compose(
                path, OWNER_ID, CLASS_ID, embedder=counting_embedder, max_words=n_words + offset
            )
        assert exc_info.value.reason == "too_long"
        assert counting_embedder.calls == [], "a refused file must buy no embedding at all"
    else:
        prepared = compose(
            path, OWNER_ID, CLASS_ID, embedder=counting_embedder, max_words=n_words + offset
        )
        assert prepared, "a file at or under the ceiling must produce chunks"
        assert len(counting_embedder.calls) == 1, "an accepted file embeds exactly once"


def test_the_too_long_message_names_the_real_count_the_ceiling_and_the_file(pdf_factory):
    """The refusal message carries the two numbers and the filename - the whole actionable payload.

    `worker.py` writes `f"{exc.reason}: {exc}"` into `jobs.last_error`, so this string is the only
    place the operator learns WHY the ceiling fired. `reason` alone says "too_long" and answers
    nothing an operator can act on: too long by one word or by ten times, and which of the files
    just uploaded. Both numbers are asserted as the ACTUAL values (the measured count, the ceiling
    that was in force) rather than "contains a digit" - a message interpolating the wrong variable
    is exactly the bug a laxer check would let through, and it is invisible until someone is
    debugging at 2am.
    """
    path = pdf_factory("Livingston Cosmogony.pdf", [" ".join(f"w{i}" for i in range(300))])
    n_words = sum(len(u.text.split()) for u in parse_file(path))
    ceiling = n_words - 7

    with pytest.raises(ParseError) as exc_info:
        compose(path, OWNER_ID, CLASS_ID, embedder=_ExplodingEmbedder(), max_words=ceiling)

    message = str(exc_info.value)
    assert str(n_words) in message, f"the real word count is missing from {message!r}"
    assert str(ceiling) in message, f"the ceiling in force is missing from {message!r}"
    assert "Livingston Cosmogony.pdf" in message, f"the file is not named in {message!r}"


def test_the_ceiling_knob_moves_in_both_directions(pdf_factory, counting_embedder):
    """A LOWER ceiling refuses what the default accepts; a HIGHER one accepts what the lower
    refused - same file, same embedder, only the knob moved.

    One direction alone is not the property. A `max_words` that was accepted and then ignored in
    favour of `MAX_INGEST_WORDS` passes any test that only checks the accept side; a guard
    hardcoded at some small constant passes any test that only checks the reject side. Running
    both against ONE fixture is what leaves no reading in which the parameter is decorative.

    Deliberately distinct from `test_the_ceiling_is_inclusive_at_the_limit`, which sweeps the
    boundary at a fixed fixture: that one asks WHERE the edge is, this one asks whether the edge
    moves when told to - including a run at the shipped default with no `max_words` at all, the
    only call shape production uses.
    """
    path = pdf_factory("lecture.pdf", [" ".join(f"w{i}" for i in range(400))])
    n_words = sum(len(u.text.split()) for u in parse_file(path))
    assert n_words < MAX_INGEST_WORDS, "the fixture must sit under the shipped default"

    at_the_default = compose(path, OWNER_ID, CLASS_ID, embedder=counting_embedder)
    assert at_the_default, "the shipped default must accept an ordinary file"

    with pytest.raises(ParseError) as exc_info:
        compose(path, OWNER_ID, CLASS_ID, embedder=counting_embedder, max_words=n_words // 2)
    assert exc_info.value.reason == "too_long"

    raised_again = compose(
        path, OWNER_ID, CLASS_ID, embedder=counting_embedder, max_words=n_words * 2
    )
    assert raised_again == at_the_default, (
        "raising the ceiling back over the file must produce the identical chunk set - "
        "the knob gates the run, it does not change it"
    )
    assert len(counting_embedder.calls) == 2, "only the two accepted runs may reach the provider"


def test_a_refusal_buys_no_embedding_counted_on_the_embedder(pdf_factory, counting_embedder):
    """Zero paid calls, counted on the provider stub - not inferred from which exception escaped.

    `test_compose_rejects_input_past_the_word_ceiling_before_embedding` proves the same ordering
    with `_ExplodingEmbedder`, and that proof has a shape worth naming: it is a proof by
    CONTRADICTION - reaching `embed` raises something else, so a `ParseError` escaping means the
    guard won. That is sound and it is also indirect, and it stops working the moment the pipeline
    grows a second `embed` call site or an `except` clause between the two. `calls == []` is the
    direct measurement of the thing the ADR actually claims ("nothing embedded", ADR 0029 §3), and
    it would still be a real assertion in a pipeline where reaching `embed` was harmless.
    """
    path = pdf_factory("huge.pdf", [" ".join(f"w{i}" for i in range(300))])

    with pytest.raises(ParseError):
        compose(path, OWNER_ID, CLASS_ID, embedder=counting_embedder, max_words=100)

    assert counting_embedder.calls == [], (
        "the ceiling must fire before the pipeline's only paid call (ADR 0029 §3)"
    )


def test_a_refused_file_leaves_zero_rows_behind(pdf_factory, counting_embedder, db, db_other):
    """The blast radius of a refusal is zero rows, read back through a SECOND connection.

    "Nothing was written" is precisely the claim a single-connection assertion cannot make: `db`'s
    own connection sees its own uncommitted work, so a count of 0 through it would also hold for
    rows that were written and rolled back later, and a count of N would not distinguish committed
    from pending (ADR 0025; `db_other`'s docstring). The tables are counted by OWNER rather than by
    file id because there is no file id to count by - the point is that `ingest_file` minted one
    and then wrote nothing under it.

    `files` and `chunks` are both counted, and neither is redundant. The chunk write and the
    `files` upsert are separate statements inside `index_file`'s transaction, and the upsert runs
    FIRST - a guard that fired late enough to reach the transaction would leave a `ready` file with
    no chunks, which is the exact state ADR 0020 §3 exists to make impossible.
    """
    conn, owner_id, class_id = db
    path = pdf_factory("huge.pdf", [" ".join(f"w{i}" for i in range(300))])

    with pytest.raises(ParseError) as exc_info:
        ingest_file(path, owner_id, class_id, embedder=counting_embedder, conn=conn, max_words=100)
    assert exc_info.value.reason == "too_long"

    (files_count,) = db_other.execute(
        "select count(*) from files where owner_id = %s", (owner_id,)
    ).fetchone()
    (chunks_count,) = db_other.execute(
        "select count(*) from chunks where owner_id = %s", (owner_id,)
    ).fetchone()
    assert (files_count, chunks_count) == (0, 0)
    assert counting_embedder.calls == []


def test_a_refusal_does_not_flip_a_files_row_that_already_exists(
    pdf_factory, counting_embedder, db, db_other
):
    """The row the STUDENT is watching is left exactly as it was - the case with something to lose.

    The sibling test above starts from an empty table, where "wrote nothing" and "wrote nothing
    VISIBLE" are the same observation. Production never starts there: `enqueue` commits a `queued`
    `files` row before any worker claims it (ADR 0011), so by the time the ceiling fires there is
    already a row, and `index_file`'s upsert is an `on conflict do update set status = 'ready'`
    aimed straight at it. A guard that fired one line later would flip a refused file to `ready` -
    a status the student reads as "your file is searchable" over a corpus containing none of it.

    The row is written directly rather than through `enqueue`, deliberately: `enqueue` is
    `gct.jobs`' contract and this file's subject is the pure pipeline (the PM-4 seam). What matters
    here is that a `queued` row EXISTS, not how it got there; the end-to-end version that does go
    through `enqueue` lives in `test_ceiling_through_worker.py`.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    file_id = str(uuid4())
    conn.execute(
        """
        insert into files (file_id, owner_id, class_id, filename, status)
        values (%s::uuid, %s, %s::uuid, %s, 'queued')
        """,
        (file_id, owner_id, class_id, "huge.pdf"),
    )
    path = pdf_factory("huge.pdf", [" ".join(f"w{i}" for i in range(300))])

    with pytest.raises(ParseError):
        ingest_file(
            path,
            owner_id,
            class_id,
            file_id=file_id,
            embedder=counting_embedder,
            conn=conn,
            max_words=100,
        )

    published = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert published == ("queued", None), (
        "the pure pipeline does not settle status - it raises and leaves the row untouched "
        "for the worker's terminal handler to write (ADR 0020 §1)"
    )
    (chunks_count,) = db_other.execute(
        "select count(*) from chunks where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert chunks_count == 0
    assert counting_embedder.calls == []


def test_a_file_at_the_ceiling_ingests_completely_and_is_queryable(
    pdf_factory, fake_embedder, db, db_other
):
    """At the ceiling the file is not merely "not refused" - it is READY, complete, and retrievable
    under its own scope and nobody else's.

    "Doesn't raise" is a weak reading of the accept side and it is the reading a broken guard
    passes: a ceiling that truncated the unit list instead of refusing raises nothing at all, lands
    a `ready` file, and answers questions from a corpus missing most of its own text. So this
    asserts COMPLETENESS - every chunk `compose` would build is present, compared against the real
    `chunk_units(parse_file(...))` rather than a hand-guessed count - and then asserts the rows are
    reachable through `retrieve`, the actual read path, rather than through a `select`.

    Scope is asserted in both directions (F6/F12): the right owner+class sees the chunks, and a
    stranger's owner id over the same class sees NOTHING. A one-sided scope assertion passes on a
    retriever that ignores `owner_id` entirely.

    `max_words` is set to the file's own measured count rather than to `MAX_INGEST_WORDS`, and the
    reason is cost, not convenience: a genuine 250,000-word file is one code path away from this
    one (`total_words > max_words` does not know where the number came from) and measures 5.2s
    through `ingest_file` - more than this entire suite. The identity of the shipped default is
    pinned separately, on the signature, by `test_default_ceiling_is_the_config_constant`; the
    refusal side at the REAL 250,000 is exercised end-to-end in `test_ceiling_through_worker.py`.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    path = pdf_factory("lecture.pdf", ["alpha beta gamma delta", "epsilon zeta", "eta theta"])
    expected = chunk_units(parse_file(path))
    n_words = sum(len(u.text.split()) for u in parse_file(path))

    file_id = ingest_file(
        path, owner_id, class_id, embedder=fake_embedder, conn=conn, max_words=n_words
    )

    published = db_other.execute(
        "select status, failed_reason from files where file_id = %s::uuid", (file_id,)
    ).fetchone()
    assert published == ("ready", None)
    stored = db_other.execute(
        "select text, file, page_or_slide from chunks where file_id = %s::uuid order by chunk_id",
        (file_id,),
    ).fetchall()
    assert sorted(stored) == sorted((c.text, c.file, str(c.page_or_slide)) for c in expected), (
        "a file at the ceiling must be indexed WHOLE, not partially"
    )

    retrieved = retrieve(conn, "alpha beta gamma", owner_id, class_id, embedder=fake_embedder, k=10)
    assert {r.text for r in retrieved} == {c.text for c in expected}
    assert {r.file for r in retrieved} == {"lecture.pdf"}

    assert retrieve(conn, "alpha", "someone-else", class_id, embedder=fake_embedder, k=10) == [], (
        "F6/F12: a file at the ceiling is still scoped to its owner"
    )
    assert retrieve(conn, "alpha", owner_id, str(uuid4()), embedder=fake_embedder, k=10) == [], (
        "F6/F12: and to its class"
    )


def test_the_ceiling_is_per_file_and_bounds_nothing_across_a_class(
    pdf_factory, fake_embedder, db, db_other
):
    """KNOWN LIMITATION, pinned rather than fixed: three files each AT the ceiling all ingest, and
    the class ends up holding three times the ceiling's worth of words.

    `compose` counts `parse_file`'s output for ONE path and compares it to `max_words`. Nothing in
    the pipeline, the queue, or the schema sums anything across files, so the ceiling bounds one
    upload's work and not a session's, a class's, or an owner's. Ten uploads of a 249,999-word file
    is 2.5 million words of embedding under a 250,000-word ceiling, and every one of them is
    accepted by design.

    That is a deliberate scope line, not an oversight: ADR 0029 sets out to make a SINGLE upload's
    work finite, and names V2's per-caller rate limit and billing ceiling (N15) as the separate,
    API-side story that closes the aggregate lane. It is worth a test anyway, because it is the
    obvious next hole and the difference between "we know" and "nobody checked" is not recoverable
    from the code. Whoever picks up the aggregate bound should expect this test to go red - and
    that red is the notification, which is the entire point of writing it down as an assertion
    rather than as a comment.
    """
    conn, owner_id, class_id = db
    conn.autocommit = True
    ceiling = 100
    per_file_words = []
    file_ids = []
    for i in range(3):
        path = pdf_factory(f"lecture{i}.pdf", [" ".join(f"w{i}x{j}" for j in range(ceiling))])
        n_words = sum(len(u.text.split()) for u in parse_file(path))
        assert n_words == ceiling, "each file must sit exactly AT the per-file ceiling"
        per_file_words.append(n_words)
        file_ids.append(
            ingest_file(
                path, owner_id, class_id, embedder=fake_embedder, conn=conn, max_words=ceiling
            )
        )

    assert sum(per_file_words) == 3 * ceiling > ceiling, (
        "the class now holds three ceilings' worth of words, admitted one file at a time"
    )
    statuses = db_other.execute(
        "select status from files where file_id = any(%s::uuid[])", (file_ids,)
    ).fetchall()
    assert statuses == [("ready",)] * 3, "every file was accepted; nothing aggregates"
    (chunks_count,) = db_other.execute(
        "select count(*) from chunks where owner_id = %s and class_id = %s::uuid",
        (owner_id, class_id),
    ).fetchone()
    assert chunks_count >= 3, "all three files' chunks are indexed side by side in one class"


# --- The blind spot: space-free scripts (issue #43, inherited from ADR 0019) -------------------


# 20,000 repetitions of a 13-character Chinese phrase - 260,000 characters, and not one space.
# Sized to be unambiguously past the ceiling in every unit EXCEPT the one the ceiling counts.
_CJK_TEXT = "宇宙论是关于宇宙起源的理论" * 20_000


def test_the_word_ceiling_does_not_see_space_free_scripts(pptx_factory, fake_embedder):
    """PINNED, NOT FIXED - the ceiling is blind to CJK/Thai/Japanese, and this records the size of
    the hole rather than papering over it.

    Measured 2026-08-30: 260,000 characters of Chinese is **one word** under `str.split()`, because
    the script has no inter-word spaces. Against a 250,000-WORD ceiling that input scores 1, so a
    file costing roughly 130,000 tokens to embed passes a guard whose whole job is to bound
    embedding work - by a factor of 250,000.

    WHERE IT COMES FROM: not from issue #43. The unit is `sum(len(unit.text.split()) ...)`, chosen
    to mirror `_chunk_one`'s own split exactly, and that split is the provisional whitespace-word
    strategy ADR 0019 marked as spike-tunable and ADR 0029 §1 chose to measure the ceiling in so
    that the guard counts the same stream the chunker consumes. The blind spot is INHERITED from
    that strategy and shared with it: at 260,000 characters the CHUNKER is equally blind, emitting
    one 260,000-character chunk where an English file of the same token weight would emit ~1,000.
    A ceiling counted in true tokens (`tiktoken`) closes both at once, which is exactly the upgrade
    ADR 0029 parked and `chunk.py` already parks for chunk sizing - one change, not two.

    WHAT IT COSTS: the corpus is English course material (ADR 0029 §1 measured it), so nothing
    ships today that this admits. The exposure is a student uploading non-Latin-script material,
    and what they get is the subject of the next test - which is where the honest answer lives.
    This one only fixes the measurement so a future `tiktoken` switch can be checked against it.
    """
    path = pptx_factory("cosmogony-zh.pptx", [_CJK_TEXT])
    units = parse_file(path)

    assert len(units) == 1
    assert len(units[0].text) == 260_000, "the parser round-trips the text intact"
    assert sum(len(u.text.split()) for u in units) == 1, (
        "260,000 characters of Chinese is ONE whitespace-word - this is the blind spot"
    )

    # No `max_words` override: this is the SHIPPED ceiling failing to fire.
    prepared = compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder)

    assert len(prepared) == 1, "one word is one chunk window (ADR 0019 never-span holds trivially)"
    assert len(prepared[0].text) == 260_000, (
        "and that single chunk carries the whole file - ~16x more text than one embedding "
        "request can hold, handed to the provider as a single input"
    )


def test_a_space_free_file_fails_loudly_at_the_provider_and_stores_nothing(
    pptx_factory, token_limit_embedder, db, db_other
):
    """The verdict on the blind spot: it is a LOUD failure, not a silent mis-ingest. Verified by
    execution, 2026-08-30.

    The question the previous test leaves open is the one that decides whether the hole is a
    nuisance or a trust bug: a 260,000-character chunk cannot be embedded, so does the pipeline
    say so, or does something quietly store a file whose vectors represent a fraction of its text?
    The second would be the trust failure this product exists to prevent - a `ready` file, cited
    with confidence, retrieved on an embedding of its first 3%.

    It is the first. The over-cap input raises at the provider, `compose` has no `except` of any
    kind, and `index_file` is never reached - so the transaction never opens and there is nothing
    to be partially written. Asserted through `db_other` for the same reason every write claim in
    this suite is: zero rows on one connection proves nothing (ADR 0025).

    TWO THINGS THIS DOES NOT CLAIM, both worth stating because a reader will otherwise assume them:
      - The exception is NOT `ParseError` and NOT `TransientEmbeddingError`, which is asserted
        directly here because it is the classification `worker.process_one` performs. So this file
        does not land on `failed_reason = 'too_long'`, or on any `failed_reason` at all on the
        first attempt - the worker's consequence is spelled out and pinned in
        `test_ceiling_through_worker.py`, which is where it can be measured instead of reasoned.
      - `PerInputTokenLimitEmbeddings` is a LOWER bound on the real cap, not a model of it (see its
        docstring). It proves the pipeline's handling of a provider refusal; it proves nothing
        about OpenAI's exact threshold, and no test here should be read as if it did.
    """
    conn, owner_id, class_id = db
    path = pptx_factory("cosmogony-zh.pptx", [_CJK_TEXT])

    with pytest.raises(token_limit_embedder.Error) as exc_info:
        ingest_file(path, owner_id, class_id, embedder=token_limit_embedder, conn=conn)

    assert not isinstance(exc_info.value, ParseError | TransientEmbeddingError), (
        "an over-cap input is neither bad input nor bad luck, so it is UNCLASSIFIED - the worker "
        "crashes on it rather than burying the file (ADR 0020 §1)"
    )
    assert len(token_limit_embedder.calls) == 1, (
        "the ceiling did not fire, so the provider WAS reached - the money is the symptom"
    )

    (files_count,) = db_other.execute(
        "select count(*) from files where owner_id = %s", (owner_id,)
    ).fetchone()
    (chunks_count,) = db_other.execute(
        "select count(*) from chunks where owner_id = %s", (owner_id,)
    ).fetchone()
    assert (files_count, chunks_count) == (0, 0), (
        "loud, not silent: nothing is indexed, so nothing can be mis-cited"
    )


# --- Degenerate text at the other end of the range (issue #43) ---------------------------------


@pytest.mark.parametrize(
    "text",
    ["   \t  \n  ", "\u00a0\u00a0\u00a0", "\u2007\u202f", "\u3000\u3000"],
    ids=["ascii-whitespace", "no-break-space", "figure-and-narrow-nbsp", "ideographic-space"],
)
def test_text_that_is_only_whitespace_lands_on_the_existing_empty_terminal(text, pptx_factory):
    """Every flavour of "whitespace" ends on `empty` - an existing terminal reason, not a crash and
    not an accept.

    The ceiling made `TERMINAL_REASONS` a six-value set, so it is worth knowing that the OTHER end
    of the size range still resolves inside it. `parse_file`'s `_strip_nul` drops any unit whose
    text does not `strip()` to something, and a file with no units left raises `ParseError("empty")`
    - so this never reaches `compose` and can never reach the ceiling at all.

    The unicode rows are the ones with any doubt in them, and they turn on a fact about Python
    rather than about this codebase: `str.strip()` and `str.split()` treat U+00A0, U+2007, U+202F
    and U+3000 as whitespace, so a "non-breaking" space is exactly as invisible to the pipeline as
    an ASCII one. That is the answer this test exists to fix in place - the plausible alternative
    (a file of non-breaking spaces parsing to a one-word unit and being embedded as a paid,
    `ready`, contentless file) is what would happen under any of several reasonable-looking
    reimplementations of `_strip_nul`.

    PPTX rather than PDF for all four: python-pptx round-trips arbitrary unicode into the XML body
    exactly, while a PDF's text layer depends on the font carrying the glyph, which would make a
    green here a fact about reportlab.
    """
    path = pptx_factory("blank.pptx", [text])

    with pytest.raises(ParseError) as exc_info:
        parse_file(path)

    assert exc_info.value.reason == "empty"


def test_text_that_is_entirely_punctuation_is_ACCEPTED_not_refused(pptx_factory, fake_embedder):
    """PINNED, and deliberately not fixed: a file of nothing but punctuation ingests normally.

    `... !!! ??? ---` strips to something, so `parse_file` keeps the unit, the chunker emits a
    chunk, and the pipeline embeds and indexes it. Named in the affirmative because "handled by an
    existing terminal reason" is the intuitive expectation here and it is WRONG: `empty` means no
    extractable text, and punctuation is extractable text.

    Correct, and in scope. #43 shipped a CEILING; a relevance FLOOR is a different guard, and V1
    has none on purpose - ADR 0008 declines a retrieval score threshold for exactly the same
    reason, that V1 refuses on grounding rather than on a similarity number. A contentless chunk
    costs one embedding and then loses every ranking on its merits.

    The word count is asserted too, to close the door on the reading that punctuation is somehow
    free: `.split()` counts `...` as a word, so a punctuation-only file consumes ceiling budget at
    the same rate as prose.
    """
    path = pptx_factory("noise.pptx", ["... !!! ??? --- ;;;"])
    units = parse_file(path)

    assert sum(len(u.text.split()) for u in units) == 5, "punctuation counts against the ceiling"

    prepared = compose(path, OWNER_ID, CLASS_ID, embedder=fake_embedder)

    assert len(prepared) == 1
    assert prepared[0].text == "... !!! ??? --- ;;;"


def test_zero_width_space_is_not_whitespace_to_python_and_survives_the_empty_check(pptx_factory):
    """The gap between the two degenerate cases above, pinned because it is genuinely surprising.

    U+200B ZERO WIDTH SPACE is named "space", renders as nothing, and is NOT whitespace to Python:
    `"\\u200b".strip()` returns it unchanged and `.split()` reports one word. So a file whose text
    is only zero-width spaces takes the punctuation path, not the whitespace path - it survives
    `parse_file`'s `empty` check, counts as a word against the ceiling, and would be embedded and
    indexed as a `ready` file with visually blank content.

    Recorded rather than fixed for the same reason as the punctuation case - #43 is a ceiling, not
    a content floor - but recorded SEPARATELY from it, because the two look identical from the
    outside and arrive by opposite routes: punctuation is real text that happens to be useless,
    while this is a character that every layer except Python's `str` treats as absent. A future
    "strip invisible characters" pass at the parse chokepoint is where this belongs, and it will
    flip this assertion; that red is the notification.
    """
    path = pptx_factory("invisible.pptx", ["\u200b\u200b\u200b"])

    units = parse_file(path)

    assert len(units) == 1, "U+200B survives the `empty` terminal - it is not Python whitespace"
    assert sum(len(u.text.split()) for u in units) == 1, "and it consumes ceiling budget"


# The three ways `uuid.UUID` reports a bad argument, plus a malformed string (#126).
_NOT_A_USABLE_CLASS_ID = {
    "malformed": "intro-to-religion",
    "none": None,
    "int": 12345,
    "already_parsed": uuid4(),
}


@pytest.mark.parametrize("kind", sorted(_NOT_A_USABLE_CLASS_ID))
def test_ingest_file_rejects_a_bad_class_id_before_spending_anything(pdf_factory, kind):
    """A `class_id` that isn't a uuid fails INSTANTLY - no parse, no embed, no DB (#126).

    Same tripwire discipline as the `file_id` test above, and the same defect: the id is rejected
    downstream on its own - by `index_file`'s guard now, by Postgres's `::uuid` cast before #126 -
    but not until AFTER `compose` has bought a full embedding run. `conn=None` cannot be executed
    against and `_ExplodingEmbedder` raises on use, so a `ValueError` here proves neither was
    reached - move this guard below `compose` and the `RuntimeError` escapes instead, which is why
    the exception
    TYPE is asserted and not merely that something raised.

    LENIENT, unlike `file_id`: this test is the refusing half only. The accepting half - that a
    urn-spelled class_id ingests and publishes under the canonical spelling - is
    `test_ingest_file_canonicalises_the_class_id_it_publishes_under`.
    """
    path = pdf_factory("lecture.pdf", ["alpha beta gamma"])

    with pytest.raises(ValueError, match=r"ingest_file\(\) requires a uuid class_id") as caught:
        ingest_file(
            path,
            OWNER_ID,
            _NOT_A_USABLE_CLASS_ID[kind],
            embedder=_ExplodingEmbedder(),
            conn=None,
        )

    assert "create_class" in str(caught.value), "the refusal does not name what to pass instead"


def test_ingest_file_canonicalises_the_class_id_it_publishes_under(
    pdf_factory, fake_embedder, db, db_other
):
    """The lenient half: `urn:uuid:<id>` ingests, and every row lands under the canonical spelling.

    `urn` is the spelling `uuid.UUID` accepts and Postgres's `::uuid` cast refuses, so before this
    it reached `index_file` intact, aborted the insert with an `InvalidTextRepresentation` naming a
    Postgres type - and did so only after the embedding run was already bought.

    The `files` row and the `chunks` rows are both checked, through `db_other`, because they are
    written from two different values: the argument and the `class_id` `compose` stamped onto every
    `PreparedChunk`. Canonicalising one and not the other would leave them describing the same
    class in two spellings, which the F6/F12 scope filter reads as two different classes.
    """
    conn, owner_id, class_id = db
    path = pdf_factory("lecture.pdf", ["alpha beta gamma", "delta epsilon"])

    urn = UUID(class_id).urn
    assert urn != class_id, "the spelling under test is the canonical one; nothing is proven"

    file_id = ingest_file(path, owner_id, urn, embedder=fake_embedder, conn=conn)

    assert db_other.execute(
        "select class_id::text, status from files where file_id = %s::uuid", (file_id,)
    ).fetchone() == (class_id, "ready")
    assert db_other.execute(
        "select distinct class_id::text from chunks where file_id = %s::uuid", (file_id,)
    ).fetchall() == [(class_id,)], "the chunks landed under a different spelling than the file"
