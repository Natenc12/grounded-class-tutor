"""Persistence, scope, failure, and recovery checks for the primary local library."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from gct.ingest.parse import ParsedUnit
from gct.local.store import (
    APPLICATION_ID,
    MAX_EXTRACTED_CHARS,
    MAX_FILE_BYTES,
    SCHEMA_VERSION,
    Library,
    LibraryError,
)


def imported(library, class_id, *, filename="Lecture.pdf", original=b"original PDF bytes"):
    return library.import_document(
        class_id,
        filename,
        original,
        [
            ParsedUnit("Active recall retrieves a remembered fact.", filename, 1),
            ParsedUnit("Spaced practice spreads sessions over time.", filename, 3),
        ],
        3,
    )


def test_library_survives_close_and_original_file_disappearance(tmp_path):
    source = tmp_path / "original.pdf"
    source.write_bytes(b"original PDF bytes")
    path = tmp_path / "private" / "library.sqlite3"
    with Library(path) as library:
        course = library.create_class("  History  ")
        document = imported(library, course["id"], original=source.read_bytes())
        chunks = library.retrieve(course["id"], "recall")
        assert chunks[0].page_or_slide == 1
    source.unlink()
    with Library(path) as reopened:
        assert reopened.list_classes() == [course]
        assert course["name"] == "History"
        assert reopened.list_documents(course["id"]) == [document]
        assert reopened.document(course["id"], document["id"]) == document
        assert reopened.original(course["id"], document["id"]) == (
            "Lecture.pdf",
            b"original PDF bytes",
        )
        assert reopened.pages(course["id"], document["id"])[1] == {"page_or_slide": 2, "text": ""}
        assert reopened.retrieve(course["id"], "recall") == chunks
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_import_is_committed_visible_from_an_independent_connection(tmp_path):
    path = tmp_path / "library.db"
    with Library(path) as first, Library(path) as second:
        course = first.create_class("Biology")
        document = imported(first, course["id"])
        assert second.list_documents(course["id"]) == [document]
        assert second.retrieve(course["id"], "recall")


def test_duplicate_bytes_are_idempotent_within_class_but_classes_are_independent(tmp_path):
    with Library(tmp_path / "library.db") as library:
        first = library.create_class("A")["id"]
        second = library.create_class("B")["id"]
        document = imported(library, first)
        original_chunks = library.retrieve(first, "recall")
        renamed_duplicate = imported(library, first, filename="Renamed.pdf")
        other_class = imported(library, second)
        assert renamed_duplicate == document
        assert other_class["id"] != document["id"]
        assert len(library.list_documents(first)) == 1
        assert library.retrieve(first, "recall") == original_chunks
        assert library.retrieve(second, "recall")[0].chunk_id != original_chunks[0].chunk_id


def test_same_name_different_bytes_get_distinct_identities_and_grounded_provenance(tmp_path):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        first = imported(library, class_id, original=b"first original")
        second = imported(library, class_id, original=b"second original")
        assert first["id"] != second["id"]
        assert len({chunk.chunk_id for chunk in library.retrieve(class_id, "recall")}) == 2
        assert library.original(class_id, first["id"])[1] == b"first original"
        assert library.original(class_id, second["id"])[1] == b"second original"


def test_citation_resolves_same_named_documents_by_identity_with_class_scope(tmp_path):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        other = library.create_class("B")["id"]
        first = imported(library, class_id, original=b"first original")
        second = imported(library, class_id, original=b"second original")
        citations = {
            chunk.chunk_id: library.citation(class_id, chunk.chunk_id)
            for chunk in library.retrieve(class_id, "recall")
        }
        assert {citation["document_id"] for citation in citations.values()} == {
            first["id"],
            second["id"],
        }
        for chunk_id, citation in citations.items():
            assert citation == {
                "document_id": citation["document_id"],
                "filename": "Lecture.pdf",
                "page_or_slide": 1,
                "text": "Active recall retrieves a remembered fact.",
            }
            with pytest.raises(LibraryError) as error:
                library.citation(other, chunk_id)
            assert error.value.code == "citation_missing"
        first_chunk = next(
            chunk_id
            for chunk_id, citation in citations.items()
            if citation["document_id"] == first["id"]
        )
        library.delete_document(class_id, first["id"])
        with pytest.raises(LibraryError) as error:
            library.citation(class_id, first_chunk)
        assert error.value.code == "citation_missing"


@pytest.mark.parametrize("chunk_id", [None, "", "x" * 64, "0" * 63, "A" * 64, "../file", 7])
def test_invalid_citation_ids_are_closed_errors(tmp_path, chunk_id):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        with pytest.raises(LibraryError) as error:
            library.citation(class_id, chunk_id)
        assert error.value.code == "invalid_citation"


def test_parser_filename_cannot_change_admitted_source_identity(tmp_path):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        library.import_document(
            class_id,
            "Admitted.pdf",
            b"bytes",
            [ParsedUnit("Secret recall.", "/private/wrong.pdf", 1)],
            1,
        )
        assert library.retrieve(class_id, "recall")[0].file == "Admitted.pdf"


@pytest.mark.parametrize("operation", ["document", "pages", "original", "delete_document"])
def test_document_actions_refuse_cross_class_ids(tmp_path, operation):
    with Library(tmp_path / "library.db") as library:
        first = library.create_class("A")["id"]
        second = library.create_class("B")["id"]
        document = imported(library, first)
        with pytest.raises(LibraryError) as error:
            getattr(library, operation)(second, document["id"])
        assert error.value.code == "document_missing"
        assert library.original(first, document["id"])[1] == b"original PDF bytes"


def test_deleting_document_removes_original_pages_chunks_and_search_without_other_class_loss(
    tmp_path,
):
    path = tmp_path / "library.db"
    with Library(path) as library:
        first = library.create_class("A")["id"]
        second = library.create_class("B")["id"]
        document = imported(library, first)
        kept = imported(library, second)
        library.delete_document(first, document["id"])
        assert library.list_documents(first) == []
        assert library.retrieve(first, "recall") == []
        for method in (library.document, library.original, library.pages):
            with pytest.raises(LibraryError):
                method(first, document["id"])
        assert library.document(second, kept["id"]) == kept
        assert library.retrieve(second, "recall")
        assert library.connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 2
    with Library(path) as reopened:
        assert reopened.list_documents(first) == []
        assert reopened.retrieve(first, "recall") == []


def test_deleting_class_cascades_whole_library_subset(tmp_path):
    with Library(tmp_path / "library.db") as library:
        first = library.create_class("A")["id"]
        second = library.create_class("B")["id"]
        imported(library, first)
        imported(library, first, original=b"second file")
        kept = imported(library, second)
        library.delete_class(first)
        assert [course["id"] for course in library.list_classes()] == [second]
        assert library.document(second, kept["id"])
        assert library.connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        assert library.connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 2
        assert library.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_mid_import_failure_rolls_back_bytes_pages_chunks_and_fts(tmp_path):
    path = tmp_path / "library.db"
    with Library(path) as library:
        class_id = library.create_class("A")["id"]
        kept = imported(library, class_id)
        library.connection.execute(
            "CREATE TEMP TRIGGER interrupt_import BEFORE INSERT ON chunks "
            "WHEN NEW.page_or_slide = 3 BEGIN SELECT RAISE(ABORT, 'simulated full disk'); END"
        )
        with pytest.raises(LibraryError) as error:
            imported(library, class_id, original=b"new source bytes")
        assert error.value.code == "storage_error"
        assert "simulated" not in error.value.message
        assert library.list_documents(class_id) == [kept]
        assert library.connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0] == 2
    with Library(path) as reopened:
        assert reopened.list_documents(class_id) == [kept]
        assert len(reopened.retrieve(class_id, "recall")) == 1


def test_mid_delete_failure_restores_search_index_and_original(tmp_path):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        document = imported(library, class_id)
        library.connection.execute(
            "CREATE TEMP TRIGGER interrupt_delete BEFORE DELETE ON documents "
            "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
        )
        with pytest.raises(LibraryError):
            library.delete_document(class_id, document["id"])
        assert library.original(class_id, document["id"])[1] == b"original PDF bytes"
        assert library.retrieve(class_id, "recall")


def test_process_death_during_import_leaves_no_partial_document(tmp_path):
    path = tmp_path / "library.db"
    with Library(path) as library:
        class_id = library.create_class("Preserved class")["id"]
        kept = imported(library, class_id)
    # Die from an SQLite trigger after the document and pages were inserted,
    # before chunks/FTS commit. This leaves a real hot journal to recover.
    script = """
import os, sys
from gct.local.store import Library
from gct.ingest.parse import ParsedUnit
library = Library(sys.argv[1])
library.connection.create_function('crash_now', 0, lambda: os._exit(71))
library.connection.execute('CREATE TEMP TRIGGER die BEFORE INSERT ON chunks '
                          'BEGIN SELECT crash_now(); END')
library.import_document(sys.argv[2], 'Interrupted.pdf', b'interrupted original',
                        [ParsedUnit('Unique interrupted passage.', 'Interrupted.pdf', 1)], 1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), class_id],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
        timeout=10,
        capture_output=True,
    )
    assert result.returncode == 71, result.stderr.decode()
    with Library(path) as recovered:
        assert recovered.list_documents(class_id) == [kept]
        assert recovered.retrieve(class_id, "interrupted") == []
        assert recovered.original(class_id, kept["id"])[1] == b"original PDF bytes"


def test_backup_failure_removes_partial_copy_and_keeps_library(tmp_path, monkeypatch):
    from gct.local import store

    path = tmp_path / "library.db"
    target = tmp_path / "backup.db"
    with Library(path) as library:
        class_id = library.create_class("Preserve me")["id"]
        document = imported(library, class_id)

        def fail_connection(_path):
            raise sqlite3.OperationalError("simulated disk full")

        monkeypatch.setattr(store.sqlite3, "connect", fail_connection)
        with pytest.raises(LibraryError) as error:
            library.backup(target)
        assert error.value.code == "backup_failed"
        assert not target.exists()
        assert library.original(class_id, document["id"])[1] == b"original PDF bytes"


def test_backup_is_reopenable_consistent_and_never_overwrites(tmp_path):
    backup = tmp_path / "backup.db"
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        document = imported(library, class_id)
        library.backup(backup)
        before = backup.read_bytes()
        library.delete_class(class_id)
        with pytest.raises(LibraryError):
            library.backup(backup)
        assert backup.read_bytes() == before
        with pytest.raises(LibraryError):
            library.backup(library.path)
    with Library(backup) as restored:
        assert restored.original(class_id, document["id"])[1] == b"original PDF bytes"
        assert restored.retrieve(class_id, "spaced")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_newer_schema_is_refused_without_changing_existing_bytes(tmp_path):
    path = tmp_path / "library.db"
    with Library(path) as library:
        library.create_class("Preserve me")
        library.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    before = path.read_bytes()
    with pytest.raises(LibraryError) as error:
        Library(path)
    assert error.value.code == "newer_library"
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["garbage", "foreign", "missing_schema"])
def test_corrupt_or_foreign_database_is_preserved(tmp_path, kind):
    path = tmp_path / "library.db"
    if kind == "garbage":
        path.write_bytes(b"not a SQLite database, do not delete")
    else:
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            if kind == "missing_schema":
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    before = path.read_bytes()
    with pytest.raises(LibraryError):
        Library(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "extra", "changed", "null_text", "null_file"])
def test_broken_search_index_is_recovery_error_instead_of_false_refusal(tmp_path, kind):
    path = tmp_path / "library.db"
    with Library(path) as library:
        class_id = library.create_class("A")["id"]
        imported(library, class_id)
        if kind == "missing":
            library.connection.execute("DELETE FROM chunks_fts")
        elif kind == "extra":
            library.connection.execute(
                "INSERT INTO chunks_fts(rowid, text, file) VALUES (999, 'invented', 'false.pdf')"
            )
        elif kind == "changed":
            library.connection.execute("UPDATE chunks_fts SET text = 'altered' WHERE rowid = 1")
        elif kind == "null_text":
            library.connection.execute("UPDATE chunks_fts SET text = NULL WHERE rowid = 1")
        else:
            library.connection.execute("UPDATE chunks_fts SET file = NULL WHERE rowid = 1")
        assert library.connection.execute("PRAGMA quick_check(1)").fetchone()[0] == "ok"
    before = path.read_bytes()
    with pytest.raises(LibraryError) as error:
        Library(path)
    assert error.value.code == "invalid_library"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "replacement", [b"forged PDF bytes!!!", b"short", "original PDF bytes", b""]
)
def test_saved_original_bytes_must_match_declared_identity(tmp_path, replacement):
    path = tmp_path / "library.db"
    with Library(path) as library:
        class_id = library.create_class("A")["id"]
        document = imported(library, class_id)
        library.connection.execute(
            "UPDATE documents SET original = ? WHERE id = ?", (replacement, document["id"])
        )
    with Library(path) as library:
        with pytest.raises(LibraryError) as error:
            library.original(class_id, document["id"])
        assert error.value.code == "invalid_library"


def test_failed_schema_initialization_rolls_back_all_tables(tmp_path, monkeypatch):
    from gct.local import store

    path = tmp_path / "library.db"
    monkeypatch.setattr(store, "_SCHEMA", (*store._SCHEMA[:2], "INVALID SQL"))
    with pytest.raises(LibraryError):
        Library(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


@pytest.mark.parametrize(
    "value", ["", " ", "a" * 201, "bad\x00name", "bad\nname", "bad\ud800", None]
)
def test_invalid_class_names_do_not_create_rows(tmp_path, value):
    with Library(tmp_path / "library.db") as library:
        with pytest.raises(LibraryError):
            library.create_class(value)
        assert library.list_classes() == []


@pytest.mark.parametrize("value", [None, "", "../class", "' OR 1=1 --", str(uuid4()).upper(), 4])
def test_invalid_class_identifiers_fail_closed(tmp_path, value):
    with Library(tmp_path / "library.db") as library:
        with pytest.raises(LibraryError):
            library.list_documents(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"filename": "../outside.pdf"},
        {"filename": "bad\\path.pptx"},
        {"filename": "x.exe"},
        {"filename": "bad\nname.pdf"},
        {"filename": "bad\ud800.pdf"},
        {"original": b""},
        {"original": b"x" * (MAX_FILE_BYTES + 1)},
        {"original": "not bytes"},
        {"page_count": 0},
        {"page_count": 501},
        {"page_count": True},
        {"units": [ParsedUnit("a", "x.pdf", 2)]},
        {"units": [ParsedUnit("a\x00b", "x.pdf", 1)]},
        {"units": [ParsedUnit("a\ud800b", "x.pdf", 1)]},
        {"units": [ParsedUnit("a", "x.pdf", True)]},
        {"units": [ParsedUnit("a", "x.pdf", 1), ParsedUnit("b", "x.pdf", 1)], "page_count": 2},
        {"units": [ParsedUnit("x" * (MAX_EXTRACTED_CHARS + 1), "x.pdf", 1)]},
        {"units": [ParsedUnit("x" * 30001, "x.pdf", 1)]},
        {"units": []},
        {"units": [ParsedUnit("   ", "x.pdf", 1)]},
    ],
)
def test_bad_import_never_mutates_library(tmp_path, changes):
    with Library(tmp_path / "library.db") as library:
        class_id = library.create_class("A")["id"]
        arguments = {
            "filename": "x.pdf",
            "original": b"original",
            "page_count": 1,
            "units": [ParsedUnit("A useful passage", "x.pdf", 1)],
        }
        with pytest.raises(LibraryError):
            library.import_document(class_id, **(arguments | changes))
        assert library.list_documents(class_id) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem admission")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_library_admission_never_opens_unsafe_file(tmp_path, kind):
    original = tmp_path / "original"
    original.write_bytes(b"preserve me")
    target = tmp_path / "library.db"
    if kind == "symlink":
        target.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, target)
    else:
        os.mkfifo(target)
    with pytest.raises(LibraryError):
        Library(target)
    assert original.read_bytes() == b"preserve me"


def test_closed_library_rejects_actions(tmp_path):
    library = Library(tmp_path / "library.db")
    library.close()
    library.close()
    with pytest.raises(LibraryError) as error:
        library.list_classes()
    assert error.value.code == "library_closed"
    backup = tmp_path / "should-not-exist.db"
    with pytest.raises(LibraryError):
        library.backup(backup)
    assert not backup.exists()
