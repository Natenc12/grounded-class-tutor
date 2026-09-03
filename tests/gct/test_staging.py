"""Tests for `gct.staging` (issue #105, ADR 0010).

Every pin here is TWO-SIDED on purpose: the bound test shows N bytes admitted AND N+1 refused,
the filename test shows each bad name refused AND a plain/unicode name stored under exactly its
own basename, the streaming test shows reads are chunk-sized AND that more than one happened.
A one-sided pin ("the error mentions X") survives a mutation that refuses everything, which is
worse than no test.

Every test stages into `tmp_path`; nothing here touches the real `STAGING_DIR`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gct.jobs.queue import enqueue
from gct.staging import CHUNK_BYTES, StagingError, stage, validate_filename


def _slots(staging_dir: Path) -> list[Path]:
    """Everything under the staging dir, files and directories alike - the leftover census."""
    return sorted(p for p in staging_dir.rglob("*"))


class _RecordingReader:
    """A file object that records every `read(n)` it is asked for and what it handed back."""

    def __init__(self, payload: bytes) -> None:
        self._buf = payload
        self.requested: list[int] = []
        self.served = 0

    def read(self, n: int) -> bytes:
        self.requested.append(n)
        chunk, self._buf = self._buf[:n], self._buf[n:]
        self.served += len(chunk)
        return chunk


class _EndlessReader(_RecordingReader):
    """Never returns `b""` - an upload with no end. Only a stager that refuses the moment the
    count exceeds the bound can return from this at all."""

    def read(self, n: int) -> bytes:
        self.requested.append(n)
        self.served += n
        return b"x" * n


class _FailingReader:
    """Raises mid-stream, like a client that disconnected."""

    def __init__(self, before_failing: bytes) -> None:
        self._buf = before_failing

    def read(self, n: int) -> bytes:
        if not self._buf:
            raise OSError("client went away")
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk


# --- the byte bound ----------------------------------------------------------------------------


def test_bound_admits_max_bytes_and_refuses_one_more_leaving_nothing_behind(tmp_path):
    staging = tmp_path / "staging"
    n = 4096

    ref = stage(
        _RecordingReader(b"a" * n), filename="lecture.pdf", max_bytes=n, staging_dir=staging
    )
    assert Path(ref).read_bytes() == b"a" * n
    accepted = _slots(staging)
    assert Path(ref) in accepted

    with pytest.raises(StagingError) as info:
        stage(
            _RecordingReader(b"b" * (n + 1)),
            filename="lecture.pdf",
            max_bytes=n,
            staging_dir=staging,
        )
    assert info.value.reason == "too_large"
    assert info.value.remedy and info.value.remedy in str(info.value)
    # No partial, no empty slot directory: the census is exactly what the accepted upload left.
    assert _slots(staging) == accepted


def test_refusal_reads_one_byte_past_the_bound_and_not_the_rest(tmp_path):
    """An endless upload terminates ONLY if the stager refuses the moment the count exceeds
    `max_bytes` - the pin that it is not "read everything, then check the size"."""
    n = 3 * CHUNK_BYTES + 17
    reader = _EndlessReader(b"")
    with pytest.raises(StagingError) as info:
        stage(reader, filename="lecture.pdf", max_bytes=n, staging_dir=tmp_path / "s")
    assert info.value.reason == "too_large"
    assert reader.served == n + 1
    assert _slots(tmp_path / "s") == []


def test_zero_max_bytes_admits_an_empty_upload_and_refuses_one_byte(tmp_path):
    """The bound is `>`, not `>=`, all the way down to zero; and a zero-byte upload is STAGED,
    not refused - `parse_file` fails it terminally (module docstring), stage does not judge."""
    ref = stage(_RecordingReader(b""), filename="lecture.pdf", max_bytes=0, staging_dir=tmp_path)
    assert Path(ref).read_bytes() == b""
    with pytest.raises(StagingError) as info:
        stage(_RecordingReader(b"x"), filename="lecture.pdf", max_bytes=0, staging_dir=tmp_path)
    assert info.value.reason == "too_large"


def test_negative_max_bytes_is_refused_before_any_read(tmp_path):
    """`read(-1)` means read-everything to every Python file object - the exact slurp."""
    reader = _RecordingReader(b"abc")
    with pytest.raises(ValueError):
        stage(reader, filename="lecture.pdf", max_bytes=-1, staging_dir=tmp_path)
    assert reader.requested == []
    assert _slots(tmp_path) == []


# --- the untrusted filename -------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../evil.pdf",
        "sub/dir.pdf",
        "..\\evil.pdf",
        "..",
        ".",
        "...",
        "",
        "  ",
        "lec\x00ture.pdf",
        "x" * 256,
    ],
)
def test_bad_filenames_are_refused_and_nothing_is_written(tmp_path, bad):
    reader = _RecordingReader(b"%PDF-1.4")
    with pytest.raises(StagingError) as info:
        stage(reader, filename=bad, staging_dir=tmp_path)
    assert info.value.reason == "bad_filename"
    assert info.value.remedy and info.value.remedy in str(info.value)
    assert reader.requested == []  # refused before the first read
    assert _slots(tmp_path) == []


@pytest.mark.parametrize(
    "good",
    [
        "lecture.pdf",
        "Lecture 20 – Evolutionist–Creationist Debate.pptx",
        "..hidden.pdf",
        " padded .pdf",
        "x" * 255,
        "notes.txt",  # content type is parse_file's terminal call, not the stager's
    ],
)
def test_good_filenames_are_kept_verbatim_as_the_basename(tmp_path, good):
    assert validate_filename(good) is good
    ref = stage(_RecordingReader(b"%PDF-1.4 bytes"), filename=good, staging_dir=tmp_path)
    assert Path(ref).name == good
    assert Path(ref).is_absolute()
    assert Path(ref).read_bytes() == b"%PDF-1.4 bytes"


# --- collisions ---------------------------------------------------------------------------------


def test_two_uploads_named_the_same_get_two_refs_each_holding_its_own_bytes(tmp_path):
    ref_a = stage(_RecordingReader(b"first deck"), filename="lecture.pdf", staging_dir=tmp_path)
    ref_b = stage(_RecordingReader(b"second deck"), filename="lecture.pdf", staging_dir=tmp_path)
    assert ref_a != ref_b
    assert Path(ref_a).name == Path(ref_b).name == "lecture.pdf"
    assert Path(ref_a).read_bytes() == b"first deck"
    assert Path(ref_b).read_bytes() == b"second deck"
    # Both live under the staging dir the caller named - the slot is inside it, not beside it.
    assert Path(ref_a).parent.parent == Path(ref_b).parent.parent == tmp_path.resolve()


# --- streaming + durability ---------------------------------------------------------------------


def test_stager_reads_in_chunks_and_never_asks_for_more_than_chunk_bytes(tmp_path):
    payload = os.urandom(3 * CHUNK_BYTES + 5)
    reader = _RecordingReader(payload)
    ref = stage(reader, filename="lecture.pdf", staging_dir=tmp_path)
    assert max(reader.requested) <= CHUNK_BYTES
    assert len(reader.requested) >= 4  # it really streamed: several reads, not one big one
    assert Path(ref).read_bytes() == payload


def test_a_read_that_raises_mid_stream_leaves_no_partial_behind(tmp_path):
    with pytest.raises(OSError, match="client went away"):
        stage(_FailingReader(b"y" * 10), filename="lecture.pdf", staging_dir=tmp_path)
    assert _slots(tmp_path) == []


def test_durability_order_is_fsync_file_then_rename_then_fsync_dir(tmp_path, monkeypatch):
    """The one thing no read-back can observe: that the bytes were forced to disk BEFORE the
    path came into existence, and the rename itself was forced after. Recorded through the
    two calls that do it, in the order the docstring gives for them."""
    import stat

    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        mode = os.fstat(fd).st_mode
        events.append("fsync:dir" if stat.S_ISDIR(mode) else "fsync:file")
        real_fsync(fd)

    def replace(src, dst):
        events.append("rename")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    stage(_RecordingReader(b"q" * 10), filename="lecture.pdf", staging_dir=tmp_path)
    assert events == ["fsync:file", "rename", "fsync:dir"]


def test_no_part_file_remains_after_success(tmp_path):
    ref = stage(_RecordingReader(b"z" * 100), filename="lecture.pdf", staging_dir=tmp_path)
    assert [p.name for p in Path(ref).parent.iterdir()] == ["lecture.pdf"]


# --- the seam: stage -> enqueue -> what the worker will open ---------------------------------


def test_staged_ref_round_trips_through_enqueue_as_files_staging_ref(db, db_other, tmp_path):
    """`stage` returns exactly the string `enqueue` stores as `files.staging_ref`, and
    `files.filename` is the accepted upload name - read back on a SECOND connection, because a
    read on `db` would hold whether or not the row was ever published (ADR 0025)."""
    conn, owner_id, class_id = db
    payload = b"%PDF-1.4 the upload"
    ref = stage(_RecordingReader(payload), filename="lecture.pdf", staging_dir=tmp_path)

    file_id = enqueue(conn, path=ref, owner_id=owner_id, class_id=class_id)

    row = db_other.execute(
        "select filename, staging_ref from files where file_id = %s", (file_id,)
    ).fetchone()
    assert row is not None
    filename, staging_ref = row
    assert staging_ref == ref
    assert filename == "lecture.pdf"
    assert Path(staging_ref).read_bytes() == payload
