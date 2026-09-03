"""File staging (ADR 0010) - the durable landing for an upload's bytes, pure of HTTP.

Async ingestion (ADR 0006/0011) puts the producer and the consumer in different processes at
different times, so the bytes a request carries MUST be on disk before that request returns;
this module is the one writer of that directory. It knows nothing about multipart, routers or
FastAPI: `stage` takes any object with a `read(n) -> bytes` method and hands back the string a
caller then passes to `gct.jobs.queue.enqueue` as `path` - `staging_ref` IS that absolute path
(decided 2026-08-03, confirmed here), which is why neither `enqueue` nor `worker.process_one`
changed when the stager arrived. It stops being a local path only at ADR 0010's V2 move to
Object Storage.

Two refusals live here, and both are REQUEST-time: they are raised at the caller with no
`files` row in existence, so there is nothing for a `failed_reason` to hang on. That is the
whole reason `StagingError` is its own class with its own reason set rather than a widening of
`gct.ingest.parse.TERMINAL_REASONS` - that taxonomy mirrors the `files.failed_reason` CHECK
constraint and describes a file the student can poll; these describe a request that never
became one. The files router (#110) maps `reason` to an HTTP error envelope and `remedy` to
the text the student reads.

What this module does NOT judge: content. A zero-byte upload, a `.txt`, a corrupt PDF are all
staged faithfully and fail TERMINALLY at parse (`unparseable` / `unsupported` / `empty`, ADR
0020) - that is the designed route the `enqueue` docstring names, and it keeps one writer for
"is this file usable" (`parse_file`). A refusal here would be a second, weaker copy of that
judgement (three bytes are exactly as unparseable as zero).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Protocol

from gct.config import MAX_STAGE_BYTES, STAGING_DIR

# The closed request-time refusal set. Deliberately DISJOINT from `parse.TERMINAL_REASONS`
# (see the module docstring); a router maps these, never `files.failed_reason`.
STAGING_REASONS = ("bad_filename", "too_large")

# The most bytes one `read` asks the upload for. The stager holds at most this much in memory
# regardless of the upload's size - the point of streaming. 1 MiB is a page-cache-friendly
# size with no measured argument behind it; it is not a tunable anyone should need.
CHUNK_BYTES = 1024 * 1024

# Every common filesystem (APFS, ext4, NTFS) caps a single name at 255 BYTES; past it the
# `open` below raises `OSError(ENAMETOOLONG)` from deep inside the write, which a router would
# render as a 500 for what is a bad request.
_MAX_FILENAME_BYTES = 255

_FORBIDDEN = ("/", "\\", "\x00")


class Readable(Protocol):
    """The whole contract `stage` needs from an upload: `read(n)` returning at most `n` bytes,
    and `b""` at the end. `starlette.UploadFile.file`, an open binary file and `io.BytesIO`
    all satisfy it; nothing here seeks, tells or closes."""

    def read(self, n: int, /) -> bytes: ...


class StagingError(Exception):
    """A request-time refusal - the upload never became a `files` row.

    `reason` is a token from `STAGING_REASONS` for a router to switch on; `remedy` is what the
    student can DO about it, in words, and `str(exc)` carries both the detail and the remedy
    so a caller that only logs the exception still names the fix (the refuse-don't-convert
    rule at boundaries: an error that does not say what to change is half an error).
    """

    def __init__(self, reason: str, detail: str, *, remedy: str) -> None:
        assert reason in STAGING_REASONS, f"unknown staging reason: {reason!r}"
        super().__init__(f"{detail} - {remedy}")
        self.reason = reason
        self.detail = detail
        self.remedy = remedy


def validate_filename(filename: str) -> str:
    """Return `filename` UNCHANGED if it may become a citation label; raise `bad_filename` if not.

    The accepted name is stored verbatim: `enqueue` writes `Path(path).name` to `files.filename`,
    which is denormalized onto every chunk and rendered in every citation the student sees
    (honor-point 1 of the citation spine), so the on-disk basename must be exactly the name
    they uploaded - not a slug, not a stripped or lower-cased copy. Hence REJECT, never
    normalize: a normalized name would be a citation label the student never typed.

    Rejected, with the remedy each error names:
      - empty or whitespace-only (`""`, `"  "`) - there is nothing to cite;
      - a path separator (`/`, `\\`) or NUL anywhere - `../evil.pdf`, `sub/dir.pdf`, and
        `a\\x00b.pdf` are all traversal or truncation attempts, and a basename never needs
        either; the remedy is to send the basename alone;
      - dot-only (`.`, `..`, `...`) - directory references, not file names;
      - longer than 255 bytes in UTF-8 - the filesystem's per-name cap (see `_MAX_FILENAME_BYTES`).

    Kept as-is: everything else, including leading/trailing whitespace, leading dots
    (`..hidden.pdf` is a legal name), unicode, and an extension `parse_file` will refuse -
    content type is the parser's call, terminally (`unsupported`), not this validator's.
    """
    if filename.strip() == "":
        raise StagingError(
            "bad_filename",
            "the upload has no filename",
            remedy="name the file (for example lecture-3.pdf) and upload it again",
        )
    if any(ch in filename for ch in _FORBIDDEN):
        raise StagingError(
            "bad_filename",
            f"filename {filename!r} contains a path separator or NUL",
            remedy="send the file's basename only (lecture-3.pdf, not ../lecture-3.pdf)",
        )
    if filename.strip(".") == "":
        raise StagingError(
            "bad_filename",
            f"filename {filename!r} is a directory reference, not a file name",
            remedy="name the file (for example lecture-3.pdf) and upload it again",
        )
    if len(filename.encode("utf-8")) > _MAX_FILENAME_BYTES:
        raise StagingError(
            "bad_filename",
            f"filename is {len(filename.encode('utf-8'))} bytes; the cap is {_MAX_FILENAME_BYTES}",
            remedy=f"shorten the filename to {_MAX_FILENAME_BYTES} bytes or fewer",
        )
    return filename


def stage(
    fileobj: Readable,
    *,
    filename: str,
    max_bytes: int = MAX_STAGE_BYTES,
    staging_dir: str | Path = STAGING_DIR,
) -> str:
    """Stream an upload to `<staging_dir>/<uuid4>/<filename>` durably; return that absolute path.

    The return value is `staging_ref`: pass it to `enqueue(conn, path=...)` unchanged. The
    per-upload directory is what makes two uploads both called `lecture.pdf` two distinct refs
    while `Path(ref).name` stays exactly the accepted filename - the alternative, a prefix or
    suffix on the basename, would mangle the citation label `enqueue` derives from it.

    Bound: refused with `too_large` the moment the byte count EXCEEDS `max_bytes` - `max_bytes`
    itself is admitted - by asking the upload for at most `max_bytes - written + 1` bytes per
    read, so the refusal reads one byte past the bound and not a chunk past it, and never the
    rest of the upload. Nothing is truncated: a refused upload leaves NO file behind, because a
    truncated file that later parsed would index a lecture with its last pages missing and cite
    it with a straight face.

    Durability, and why this order:
      1. bytes stream in `CHUNK_BYTES` reads to `<slot>/.part` - a temp name in the SAME
         directory, so the final step is a same-filesystem rename, which is the only rename
         POSIX makes atomic;
      2. `flush` then `os.fsync` the file - the write is in the page cache until then, and a
         crash between `close` and the sync loses bytes behind a path that already exists;
      3. rename `.part` to `filename` - so a path that exists is, by construction, complete: the
         worker (or a spike re-running chunking, ADR 0010) can never open a half-written file;
      4. `fsync` the directory - the rename itself is directory metadata, and without this a
         crash can roll it back after `enqueue` has committed the ref.
    Any failure before step 3 - the bound, a `read` that raises, a full disk - removes the
    partial AND the slot directory before re-raising, so the staging dir only ever holds
    complete uploads.

    Raises `StagingError` (`bad_filename`, `too_large`) and `ValueError` for a negative
    `max_bytes` - a negative read size means "read everything" to every Python file object,
    which is precisely the slurp this function exists to prevent.
    """
    name = validate_filename(filename)
    if max_bytes < 0:
        raise ValueError(f"max_bytes must be >= 0, got {max_bytes}")

    root = Path(staging_dir).resolve()
    slot = root / uuid.uuid4().hex
    slot.mkdir(parents=True, exist_ok=False)
    part = slot / ".part"
    final = slot / name

    written = 0
    try:
        with open(part, "wb") as out:
            while True:
                chunk = fileobj.read(min(CHUNK_BYTES, max_bytes - written + 1))
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise StagingError(
                        "too_large",
                        f"{name!r} exceeds the {max_bytes:,}-byte upload limit",
                        remedy="split the file or shrink it (compress images), then upload again",
                    )
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        part.unlink(missing_ok=True)
        slot.rmdir()
        raise

    os.replace(part, final)
    _fsync_dir(slot)
    return str(final)


def _fsync_dir(directory: Path) -> None:
    """Persist a directory's entries (a rename is metadata of the PARENT, not of the file)."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
