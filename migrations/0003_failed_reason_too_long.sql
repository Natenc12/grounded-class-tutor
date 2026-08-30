-- Slice 2 — `files.failed_reason` gains `too_long`: the terminal reason the ingest input
-- ceiling raises (issue #43, ADR 0020, terminal set extended per ADR 0029).
--
-- The column shipped as a CLOSED set of five values (0001_init.sql), so a new terminal reason
-- is a schema change, not a constant. Without this migration the pipeline can produce a reason
-- the worker's terminal path (`_bury(reason=exc.reason)`) cannot write: a CheckViolation raised
-- from inside the bury transaction, i.e. a job that fails while recording why it failed.
--
-- Widening happens by DROP + re-ADD because a CHECK is not alterable in place. `drop ... if
-- exists` then `add` is the idempotent shape here — re-running drops the widened constraint and
-- puts it back identically, matching 0001/0002's re-runnable-by-design contract (scripts/
-- migrate.py applies every file, every time). The constraint name is Postgres's own generated
-- name for the inline column check in 0001, so it is stated explicitly rather than guessed at.
--
-- Widening only: every value legal before is legal after, so nothing already stored can violate
-- it and no existing row needs touching.
alter table files drop constraint if exists files_failed_reason_check;

alter table files add constraint files_failed_reason_check
    check (failed_reason in
        ('unparseable','protected','unsupported','empty','too_long','transient_exhausted'));
