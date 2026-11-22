"""A document is saved whole or not at all (#227).

The realistic ways a save is cut short: the app is killed partway, the disk fills, or another
window holds the library. Each test stops the save as late as it can, after the document row and
every page are written and while passages are still going in, so a save split into two
transactions (document and pages first, passages after) leaves a document behind and fails here.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from gct.ingest.parse import ParsedUnit
from gct.library import Library, LibraryError

PAGE_COUNT = 120
DISK_FULL = "the disk is full"
LOCKED = "another window is using the library"

KILLED_SAVE = """
import sys, time
from pathlib import Path
from gct.ingest.chunk import chunk_units
from gct.ingest.parse import ParsedUnit
from gct.library import Library

path, class_id, ready = sys.argv[1], sys.argv[2], Path(sys.argv[3])
page_count = int(sys.argv[4])
units = [
    ParsedUnit(text=f"page {n} " + "aquifer recharge " * 300, file="x.pdf", page_or_slide=n)
    for n in range(1, page_count + 1)
    if n % 10
]
last = len(chunk_units(units))
library = Library(path)
library._connection.execute("PRAGMA cache_size = 8")
inserted = 0

def pause_on_the_last_passage(statement):
    global inserted
    if statement.startswith("INSERT INTO passages "):
        inserted += 1
        if inserted == last:
            ready.write_text("ready")
            time.sleep(600)

library._connection.set_trace_callback(pause_on_the_last_passage)
library.save_document(class_id, "big.pdf", b"%PDF big" * 50_000, page_count, units)
"""


def _units() -> list[ParsedUnit]:
    return [
        ParsedUnit(text=f"page {n} " + "aquifer recharge " * 300, file="x.pdf", page_or_slide=n)
        for n in range(1, PAGE_COUNT + 1)
        if n % 10
    ]


def _save(library: Library, class_id: str, original: bytes = b"%PDF big" * 2_000):
    return library.save_document(class_id, "big.pdf", original, PAGE_COUNT, _units())


def _rows(connection: sqlite3.Connection) -> dict[str, int]:
    tables = ("documents", "pages", "passages", "passages_fts_docsize")
    return {t: connection.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


@pytest.fixture
def hydrology(library: Library) -> str:
    class_id = library.create_class("Hydrology").id
    library.save_document(
        class_id,
        "small.pdf",
        b"%PDF small",
        1,
        [ParsedUnit(text="Wetlands filter runoff.", file="x.pdf", page_or_slide=1)],
    )
    return class_id


@pytest.fixture
def impatient(library_path: Path, hydrology: str) -> Iterator[Library]:
    with Library(library_path, timeout=0.05) as opened:
        yield opened


@pytest.fixture
def other_window(library_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(library_path, timeout=0.05, isolation_level=None)
    yield connection
    if connection.in_transaction:
        connection.execute("ROLLBACK")
    connection.close()


def test_a_save_killed_partway_leaves_the_library_exactly_as_before(
    library: Library, hydrology: str, library_path: Path, tmp_path: Path
):
    library.close()
    before = library_path.read_bytes()
    ready = tmp_path / "ready"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            KILLED_SAVE,
            str(library_path),
            hydrology,
            str(ready),
            str(PAGE_COUNT),
        ]
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert child.poll() is None, "the save finished or failed before it could be killed"
            assert time.monotonic() < deadline, "the save never reached its last passage"
            time.sleep(0.02)
        assert Path(f"{library_path}-journal").exists()
        assert library_path.read_bytes() != before, "the save never reached the file itself"
    finally:
        os.kill(child.pid, signal.SIGKILL)
        child.wait()

    with Library(library_path) as reopened:
        assert [d.filename for d in reopened.list_documents(hydrology)] == ["small.pdf"]
        assert reopened.check() == []
        assert _rows(reopened._connection) == {
            "documents": 1,
            "pages": 1,
            "passages": 1,
            "passages_fts_docsize": 1,
        }
    assert library_path.read_bytes() == before


def test_a_save_on_a_full_disk_leaves_the_library_as_before(
    library: Library, hydrology: str, tmp_path: Path
):
    with Library(tmp_path / "measure.sqlite3") as measure:
        empty = measure._connection.execute("PRAGMA page_count").fetchone()[0]
        _save(measure, measure.create_class("Hydrology").id)
        needed = measure._connection.execute("PRAGMA page_count").fetchone()[0] - empty
    before = _rows(library._connection)
    pages = library._connection.execute("PRAGMA page_count").fetchone()[0]
    library._connection.execute(f"PRAGMA max_page_count = {pages + needed * 2 // 3}")

    with pytest.raises(LibraryError, match=f"big.pdf was not saved: {DISK_FULL}") as raised:
        _save(library, hydrology)

    assert isinstance(raised.value.__cause__, sqlite3.Error)
    assert not library._connection.in_transaction
    assert _rows(library._connection) == before
    assert library.check() == []
    library._connection.execute(f"PRAGMA max_page_count = {pages + needed * 2}")
    assert _save(library, hydrology).document.filename == "big.pdf"
    assert library.check() == []


def test_a_save_while_another_window_is_writing_says_so_and_saves_nothing(
    impatient: Library, hydrology: str, other_window: sqlite3.Connection
):
    before = _rows(impatient._connection)
    other_window.execute("BEGIN IMMEDIATE")

    with pytest.raises(LibraryError, match=LOCKED):
        _save(impatient, hydrology)

    other_window.execute("ROLLBACK")
    assert not impatient._connection.in_transaction
    assert _rows(impatient._connection) == before
    _save(impatient, hydrology)
    assert impatient.check() == []


def test_a_save_blocked_by_another_window_reading_saves_nothing(
    impatient: Library, hydrology: str, other_window: sqlite3.Connection
):
    before = _rows(impatient._connection)
    other_window.execute("BEGIN")
    other_window.execute("SELECT count(*) FROM passages").fetchone()

    with pytest.raises(LibraryError, match=LOCKED):
        _save(impatient, hydrology)

    assert not impatient._connection.in_transaction
    assert _rows(impatient._connection) == before
    other_window.execute("COMMIT")
    _save(impatient, hydrology)
    assert impatient.check() == []
