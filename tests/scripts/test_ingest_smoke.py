"""Tests for `scripts/ingest_smoke.py` — the parts of the ceremony that can go wrong SILENTLY.

WHY A CEREMONY SCRIPT HAS TESTS AT ALL, when ADR 0009 calls the scripts thin peer callers that
usually earn none: `ingest_smoke.py` is the thing that decides whether Slice 2 shipped, and almost
every way for it to be wrong is a way for it to go GREEN. A `partial_index_sightings` that returns
`[]` for the wrong reason, a `reaper_evidence` that says yes to a plain retry, an
`illegal_transitions` whose table quietly permits everything — each one still prints PASS, and the
run that would have caught it costs real money and cannot be run in CI. That is the same shape of
defect `test_ask_smoke.py` and `test_worker_script.py` were written for: invisible from reading,
visible only from running, and only if you already knew the answer.

Everything here is offline and free except the one `db`-marked test at the bottom: no provider
client is constructed anywhere in this file, deliberately, so nothing here takes a `live_*` fixture
and nothing here can spend money. The ceremony's PAID half — that a real worker really does move a
real file — is not simulated here; it is what running the script itself proves.

`parse_file` is driven for real over the ceremony's own corrupt bytes, because the reason phase 2
asserts (`unparseable`) is a fact about pypdf, not about this repo, and pinning it here is what
turns a pypdf upgrade that reclassified those bytes into a red CI run rather than a ceremony
asserting against a reason nothing produces any more.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from gct.config import EMBEDDING_DIM
from gct.ingest.parse import ParseError, parse_file

# Imported by PATH, the same way `test_worker_script.py` and `test_ask_smoke.py` do it and for the
# same reason: `scripts/` is deliberately not a package (ADR 0009 — the scripts are peers of the
# library, not part of it), so there is nothing to `import` and bending `sys.path` would blur the
# boundary.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ingest_smoke.py"
_spec = importlib.util.spec_from_file_location("ingest_smoke_under_test", _SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
# Registered in `sys.modules` BEFORE it is executed, which the other two script tests do not need
# to do. `@dataclass` resolves its own module out of `sys.modules` while processing the class body
# (to recognise `KW_ONLY`), so a module executed without being registered raises `AttributeError`
# on `Snapshot` — not a missing import, and nothing about the script is wrong. Registering first is
# the documented shape of the from-a-path import; the two-line version only works for a file with
# no dataclass in it.
sys.modules[_spec.name] = smoke
_spec.loader.exec_module(smoke)


def snapshot(status: str, chunks: int, **overrides) -> smoke.Snapshot:
    """A `Snapshot` with only the fields a given test cares about spelled out.

    The two axes almost every test here is about are `files.status` and the chunk count; the job
    columns default to a plausible in-flight reading so a test about the partial-index rule does
    not have to state an opinion about `attempts` to say what it means.
    """
    return smoke.Snapshot(
        file_status=status,
        failed_reason=overrides.get("failed_reason"),
        job_state=overrides.get("job_state", "processing"),
        attempts=overrides.get("attempts", 1),
        last_error=overrides.get("last_error"),
        chunks=chunks,
    )


# --- the terminal-failure input -----------------------------------------------------------------


def test_the_ceremonys_corrupt_bytes_really_parse_to_unparseable(tmp_path):
    """Phase 2 asserts `failed_reason == 'unparseable'`; this is what makes that assertion true.

    The reason is not this repo's decision — it is what pypdf does with a file that has a PDF
    header and no body, routed through `parse.py`'s taxonomy. Read off a real run rather than
    guessed, and pinned here so it stays read off one: a pypdf release that started rejecting these
    bytes at the EXTENSION check, or parsing them into zero pages (`empty`), would leave phase 2
    quietly asserting a reason nothing produces, and the ceremony would fail for a reason that has
    nothing to do with the write path.
    """
    corrupt = smoke.write_corrupt_pdf(tmp_path)
    with pytest.raises(ParseError) as caught:
        parse_file(corrupt)
    assert caught.value.reason == "unparseable"


def test_the_corrupt_file_is_written_with_a_pdf_suffix(tmp_path):
    """The suffix is what routes it to the PDF parser at all.

    Rename it `.txt` and `parse_file` raises `unsupported` without opening anything — a terminal
    failure too, but a much weaker demonstration: it proves the extension whitelist works, not that
    a parser met bad bytes and classified them. Phase 2 is written for the second claim.
    """
    assert smoke.write_corrupt_pdf(tmp_path).suffix == ".pdf"


# --- what a history is allowed to say -----------------------------------------------------------


def test_status_path_collapses_repeats_and_keeps_order():
    """The observer samples far faster than the worker moves, so most readings are duplicates."""
    history = [
        snapshot("queued", 0),
        snapshot("processing", 0),
        snapshot("processing", 0, attempts=2),
        snapshot("ready", 12, job_state="done"),
    ]
    assert smoke.status_path(history) == ["queued", "processing", "ready"]


def test_the_happy_path_has_no_illegal_transitions():
    assert smoke.illegal_transitions(["queued", "processing", "ready"]) == []


def test_a_bury_followed_by_a_later_claim_is_legal():
    """`failed -> processing` is PERMITTED, and deliberately so.

    `_bury` writes `files` before `jobs`, so a crash between the two leaves a `failed` file under a
    still-claimable job; the reaper requeues it and the next claim's `processing` write is what
    recovers the row. A table that forbade this transition would report the recovery path as a
    defect.
    """
    assert smoke.illegal_transitions(["failed", "processing", "ready"]) == []


@pytest.mark.parametrize("after", ["processing", "failed", "queued"])
def test_ready_is_absorbing(after):
    """Nothing may follow `ready` — the one status that is a promise already made to the student.

    Both writers that could leave it carry the same `status <> 'ready'` guard (`process_one`'s
    claim write and `_bury`'s files write), so a sighting of anything after `ready` means one of
    those guards stopped holding. This is the check that would notice.
    """
    assert smoke.illegal_transitions(["ready", after]) == [("ready", after)]


def test_a_status_outside_the_schemas_check_set_is_reported():
    """`files.status` is CHECK-constrained, so an unknown value means the schema moved."""
    assert smoke.illegal_transitions(["queued", "quarantined"]) == [("queued", "quarantined")]


# --- no partial index ever visible --------------------------------------------------------------


def test_a_clean_first_ingest_shows_no_partial_index():
    history = [
        snapshot("queued", 0),
        snapshot("processing", 0),
        snapshot("ready", 62, job_state="done"),
    ]
    assert smoke.partial_index_sightings(history) == []


def test_a_redelivery_that_replaced_the_set_shows_no_partial_index():
    """Two publishes of the same file under the same window are the same size — that is idempotence.

    This is phase 3's shape: the zombie publishes, then the winner republishes over it. Both
    sightings carry the full count, and the count in between never dips.
    """
    history = [
        snapshot("queued", 0),
        snapshot("processing", 0),
        snapshot("ready", 62),
        snapshot("ready", 62, job_state="done"),
    ]
    assert smoke.partial_index_sightings(history) == []


def test_ready_over_zero_chunks_is_a_violation():
    """`status = 'ready'` ⟺ a full chunk set is committed and queryable (ADR 0020).

    A `ready` row with nothing behind it is the trust failure in its purest form: the file is
    advertised as answerable and grounds nothing.
    """
    published = snapshot("ready", 0, job_state="done")
    assert smoke.partial_index_sightings([snapshot("processing", 0), published]) == [published]


def test_a_count_between_zero_and_full_is_a_violation():
    """A chunk set caught mid-write — literally partial.

    Not reachable through `index_file`, whose delete-and-insert is one transaction, which is the
    guarantee rather than a reason not to look: a publish that escaped that transaction would show
    up exactly here, on a real second connection.
    """
    caught = snapshot("processing", 30)
    history = [snapshot("processing", 0), caught, snapshot("ready", 62, job_state="done")]
    assert smoke.partial_index_sightings(history) == [caught]


def test_a_ready_sighting_that_disagrees_with_the_publishing_run_is_a_violation():
    """The issue's own wording: `ready` against a count that disagrees with the run that published.

    Here an earlier publish carried fewer chunks than the file ever ends up with — a shrunken set
    advertised as complete. Self-consistent, and still a lie.
    """
    stale = snapshot("ready", 40)
    history = [snapshot("processing", 0), stale, snapshot("ready", 62, job_state="done")]
    assert smoke.partial_index_sightings(history) == [stale]


def test_a_file_that_never_published_has_nothing_partial_about_it():
    """Phase 2's file: zero chunks throughout, `failed` at the end. Zero is not partial."""
    history = [snapshot("queued", 0), snapshot("failed", 0, job_state="failed", attempts=1)]
    assert smoke.partial_index_sightings(history) == []


# --- the reaper's fingerprint -------------------------------------------------------------------


def test_one_delivery_is_not_a_reclaim():
    reaped, why = smoke.reaper_evidence([snapshot("ready", 62, job_state="done")])
    assert not reaped
    assert "delivered once" in why


def test_a_redelivery_with_a_clean_error_column_is_the_reaper():
    """`attempts >= 2` says a redelivery happened; a null `last_error` says the reaper caused it.

    Every other route back to `queued` writes that column — `release` on a transient failure, `fail`
    on a bury — so the reaper is the only requeue that leaves it untouched. Together the two
    columns identify the writer without reading a single log line.
    """
    reaped, why = smoke.reaper_evidence([snapshot("ready", 62, job_state="done", attempts=2)])
    assert reaped
    assert "attempts=2" in why


def test_a_retry_after_a_transient_failure_is_not_the_reaper():
    """The test that stops phase 3 from passing on the wrong mechanism.

    A 429 mid-embed also produces `attempts == 2`, and a check that only counted deliveries would
    call that a reclaim and print PASS for a run in which no lease ever expired. `release` writes
    `last_error`, so the two are distinguishable — but only if something looks.
    """
    history = [snapshot("ready", 62, job_state="done", attempts=2, last_error="transient: 429")]
    reaped, why = smoke.reaper_evidence(history)
    assert not reaped
    assert "not from the reaper" in why


# --- the lifecycle gate itself ------------------------------------------------------------------


def test_a_file_that_reached_ready_without_being_seen_processing_fails():
    """THE assertion issue #72 is built around, tested against the history that would fake it.

    An end-state check reads this history as a clean pass: it starts `queued`, it ends `ready`,
    the chunks are there. Nothing about the worker was demonstrated — `processing` is the only
    status the worker itself writes, and it was never seen.
    """
    history = [snapshot("queued", 0), snapshot("ready", 62, job_state="done")]
    faults = smoke._lifecycle_faults(smoke.status_path(history), history)
    assert any("'processing' was never observed" in fault for fault in faults)


def test_a_fully_observed_lifecycle_has_no_faults():
    history = [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("processing", 0),
        snapshot("ready", 62, job_state="done"),
    ]
    assert smoke._lifecycle_faults(smoke.status_path(history), history) == []


def test_a_ready_file_whose_job_never_settled_is_a_fault():
    """`ready` on the file axis and `processing` on the job axis is a job nobody completed.

    It is a legal INTERMEDIATE reading under at-least-once — phase 3 sees it, while the winner is
    still working — but as a FINAL reading it means the lease was lost and nothing ever settled the
    job, which the happy path must not do.
    """
    history = [
        snapshot("queued", 0, job_state="queued", attempts=0),
        snapshot("processing", 0),
        snapshot("ready", 62, job_state="processing"),
    ]
    faults = smoke._lifecycle_faults(smoke.status_path(history), history)
    assert any("jobs.state='processing'" in fault for fault in faults)


# --- setup validity -----------------------------------------------------------------------------


def test_a_missing_corpus_directory_is_a_setup_error(tmp_path):
    with pytest.raises(smoke.SetupError, match="does not exist"):
        smoke._corpus_files(tmp_path / "nope", None)


def test_an_empty_corpus_directory_is_a_setup_error(tmp_path):
    """Exit 2, not exit 1: a run with nothing to ingest never validly started."""
    (tmp_path / "notes.txt").write_text("not a corpus file")
    with pytest.raises(smoke.SetupError, match="nothing to ingest"):
        smoke._corpus_files(tmp_path, None)


def test_max_files_narrows_a_sorted_corpus(tmp_path):
    """Sorted, so `--max-files 2` means the same two files on every run and two runs compare."""
    for name in ("c.pdf", "a.pptx", "b.pdf"):
        (tmp_path / name).write_bytes(b"%PDF-1.7\n")
    assert [p.name for p in smoke._corpus_files(tmp_path, None)] == ["a.pptx", "b.pdf", "c.pdf"]
    assert [p.name for p in smoke._corpus_files(tmp_path, 2)] == ["a.pptx", "b.pdf"]


def _pdf(path: Path, pages: list[str]) -> Path:
    page = canvas.Canvas(str(path), pagesize=(612, 792))
    for text in pages:
        if text:
            page.drawString(72, 700, text)
        page.showPage()
    page.save()
    return path


def test_phase_threes_target_is_picked_by_WORDS_and_not_by_BYTES(tmp_path):
    """The measured defect this picker exists to avoid, reproduced in miniature.

    On the real dogfood corpus the LARGEST file is a 7.1MB deck carrying 315 words and the SLOWEST
    to ingest is a 3.7MB PDF carrying 12,300 — so picking by size hands phase 3 a file that
    finishes in half a second, the lease never expires, and the reaper case silently stops being
    demonstrated. Embedding time tracks text; bytes track images. Here the same inversion is built
    from two PDFs: the big one is mostly blank pages.
    """
    big_and_empty = _pdf(tmp_path / "big.pdf", [""] * 120 + ["alpha"])
    small_and_wordy = _pdf(tmp_path / "small.pdf", [" ".join(f"word{n}" for n in range(60))])

    assert big_and_empty.stat().st_size > small_and_wordy.stat().st_size
    assert smoke._slowest_to_embed([big_and_empty, small_and_wordy]) == small_and_wordy


def test_an_unparseable_corpus_file_counts_as_zero_words_rather_than_raising(tmp_path):
    """Phase 1 is the right place to report an unparseable corpus file, with its reason and history.

    A picker that raised would pre-empt that with a `SetupError` naming neither, and a ceremony that
    exits 2 for a file phase 1 was built to fail on exit 1 reports the wrong thing about the run.
    """
    corrupt = smoke.write_corrupt_pdf(tmp_path)
    wordy = _pdf(tmp_path / "wordy.pdf", ["one two three four five"])
    assert smoke._slowest_to_embed([corrupt, wordy]) == wordy


# --- the observer's one statement ---------------------------------------------------------------


def test_the_snapshot_query_reads_both_axes_and_the_chunk_count(db, db_other):
    """The observer's whole view of the world, driven against real rows on a REAL second connection.

    Read back through `db_other` and not through `db`, which is the point rather than a formality:
    a connection sees its own uncommitted work, so this query run on the writing connection would
    return the same six columns whether or not a single row was ever published (ADR 0025). The
    ceremony's every claim — `ready` happened, the chunks are there, the job settled — is a claim
    about what SURVIVED, so the query behind it is verified the same way.

    One statement, six columns, both axes plus the count: `files.status` and `count(chunks)` fetched
    in two round trips could straddle the index transaction's commit and manufacture the very
    `ready`-with-a-disagreeing-count sighting the ceremony reports as a violation.
    """
    conn, owner_id, class_id = db
    file_id = str(uuid.uuid4())
    conn.execute(
        """
        insert into files (file_id, owner_id, class_id, filename, staging_ref, status,
                           failed_reason)
        values (%s::uuid, %s, %s::uuid, %s, %s, 'failed', 'unparseable')
        """,
        (file_id, owner_id, class_id, "corrupt-lecture.pdf", "/tmp/corrupt-lecture.pdf"),
    )
    conn.execute(
        """
        insert into jobs (file_id, owner_id, class_id, state, attempts, last_error)
        values (%s::uuid, %s, %s::uuid, 'failed', 1, 'unparseable: could not read PDF')
        """,
        (file_id, owner_id, class_id),
    )
    for n in range(3):
        conn.execute(
            """
            insert into chunks (file_id, owner_id, class_id, text, file, page_or_slide,
                                embedding, embedding_model_id)
            values (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s)
            """,
            (
                file_id,
                owner_id,
                class_id,
                f"chunk {n}",
                "corrupt-lecture.pdf",
                str(n + 1),
                [0.0] * EMBEDDING_DIM,
                "test-embedder",
            ),
        )
    conn.commit()

    assert smoke._read(db_other, [file_id]) == {
        file_id: smoke.Snapshot(
            file_status="failed",
            failed_reason="unparseable",
            job_state="failed",
            attempts=1,
            last_error="unparseable: could not read PDF",
            chunks=3,
        )
    }


def test_the_snapshot_query_is_not_asked_at_all_when_nothing_is_watched(db_other):
    """`any(%s::uuid[])` over an empty array is legal SQL, so this is about round trips, not safety.

    The observer samples every 40ms for the length of the run; a phase that has watched nothing yet
    should cost zero statements rather than a few thousand empty ones.
    """
    assert smoke._read(db_other, []) == {}
