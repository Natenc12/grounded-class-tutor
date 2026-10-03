"""A device-local, transactional document library.

Original bytes live in the same SQLite transaction as their extracted pages,
chunks, and search index. No external blob path can become orphaned or point at
the user's original. Authentication is deliberately outside this module.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from gct.contracts import RetrievedChunk
from gct.ingest.chunk import chunk_units
from gct.ingest.parse import ParsedUnit
from gct.local import search

SCHEMA_VERSION = 1
APPLICATION_ID = 0x4743544C
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PAGES = 500
MAX_EXTRACTED_CHARS = 1_000_000
MAX_CHUNKS = 5_000
MAX_CHUNK_CHARS = 30_000
MAX_CLASS_NAME_CHARS = 200
MAX_FILENAME_CHARS = 255
_DOCUMENT_COLUMNS = "id, class_id, filename, sha256, byte_count, page_count, created_at"
_SCHEMA = (
    "CREATE TABLE classes (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE documents (id TEXT PRIMARY KEY, class_id TEXT NOT NULL REFERENCES classes(id) "
    "ON DELETE CASCADE, filename TEXT NOT NULL, sha256 TEXT NOT NULL, original BLOB NOT NULL, "
    "byte_count INTEGER NOT NULL, page_count INTEGER NOT NULL, created_at TEXT NOT NULL, "
    "UNIQUE(class_id, sha256))",
    "CREATE TABLE pages (document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, "
    "page_or_slide INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY(document_id, page_or_slide))",
    "CREATE TABLE chunks (rowid INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, "
    "document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, "
    "page_or_slide INTEGER NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL, "
    "UNIQUE(document_id, page_or_slide, ordinal))",
    "CREATE INDEX documents_class ON documents(class_id)",
    "CREATE INDEX chunks_document ON chunks(document_id)",
    "CREATE VIRTUAL TABLE chunks_fts USING fts5(text, file, tokenize='porter unicode61')",
)


class LibraryError(Exception):
    """Closed messages safe for the desktop boundary; underlying errors stay private."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _identifier(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise LibraryError("invalid_id", "Choose a valid class or document.") from exc
    return value


def _display_text(value: str, *, limit: int, filename: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or (
            filename
            and (
                "/" in value or "\\" in value or Path(value).suffix.lower() not in {".pdf", ".pptx"}
            )
        )
    ):
        raise LibraryError("invalid_name", "Use a short, readable class or PDF/PPTX filename.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise LibraryError("invalid_name", "Use a readable class or PDF/PPTX filename.") from exc
    return value.strip()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _private_file(path: Path, *, exclusive: bool = False) -> None:
    """Create a private regular file; never follow a selected symlink or FIFO."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if exclusive:
        flags |= os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode) or information.st_nlink != 1:
            raise LibraryError("storage_unsafe", "Choose a private, regular library file.")
        if hasattr(os, "getuid") and information.st_uid != os.getuid():
            raise LibraryError("storage_unsafe", "The library belongs to a different OS user.")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


class Library:
    """Open one local library; each mutation commits before returning to its caller."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).absolute()
        self._connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _private_file(self.path)
            connection = sqlite3.connect(self.path, timeout=3, isolation_level=None)
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            self._initialize()
            connection.execute("PRAGMA synchronous = FULL")
        except LibraryError:
            self.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            self.close()
            raise LibraryError(
                "storage_unavailable",
                "The local library could not be opened. Preserve it for recovery.",
            ) from exc

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *_arguments) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LibraryError("library_closed", "Reopen the local library before continuing.")
        return self._connection

    @contextlib.contextmanager
    def _errors(self) -> Iterator[None]:
        try:
            yield
        except sqlite3.Error as exc:
            raise LibraryError(
                "storage_error",
                "The local library action could not finish. Existing data is preserved.",
            ) from exc

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._errors():
            connection = self.connection
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _initialize(self) -> None:
        connection = self.connection
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        if version == 0 and application == 0 and not tables:
            with self._transaction():
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        if application != APPLICATION_ID:
            raise LibraryError(
                "invalid_library", "This file is not a Grounded Class Tutor library."
            )
        if version > SCHEMA_VERSION:
            raise LibraryError("newer_library", "This library needs a newer version of GCT.")
        if version != SCHEMA_VERSION or not {
            "classes",
            "documents",
            "pages",
            "chunks",
            "chunks_fts",
        }.issubset(tables):
            raise LibraryError("invalid_library", "The local library needs recovery before use.")
        # Initialization is version 1 only. Future upgrades must back up first and
        # migrate transactionally; never reset an unrecognized existing schema.
        if connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise LibraryError("invalid_library", "The local library needs recovery before use.")
        # SQLite page integrity does not establish that the separately maintained
        # search index still corresponds to our canonical saved passages. A lost
        # index must be a recovery error, never an unsupported-answer refusal.
        broken_index = connection.execute(
            "SELECT 1 FROM chunks c JOIN documents d ON d.id = c.document_id "
            "LEFT JOIN chunks_fts f ON f.rowid = c.rowid "
            "WHERE f.rowid IS NULL OR f.text IS NOT c.text OR f.file IS NOT d.filename "
            "UNION ALL SELECT 1 FROM chunks_fts f LEFT JOIN chunks c ON c.rowid = f.rowid "
            "WHERE c.rowid IS NULL LIMIT 1"
        ).fetchone()
        if broken_index is not None:
            raise LibraryError(
                "invalid_library", "The local search index needs recovery before use."
            )
        connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")

    def _class(self, class_id: str) -> dict:
        class_id = _identifier(class_id)
        with self._errors():
            row = self.connection.execute(
                "SELECT * FROM classes WHERE id = ?", (class_id,)
            ).fetchone()
        if row is None:
            raise LibraryError("class_missing", "This class is no longer in the local library.")
        return dict(row)

    def list_classes(self) -> list[dict]:
        with self._errors():
            return [
                dict(row)
                for row in self.connection.execute("SELECT * FROM classes ORDER BY created_at, id")
            ]

    def create_class(self, name: str) -> dict:
        name = _display_text(name, limit=MAX_CLASS_NAME_CHARS)
        class_id = str(uuid4())
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO classes(id, name, created_at) VALUES (?, ?, ?)",
                (class_id, name, _timestamp()),
            )
        return self._class(class_id)

    def list_documents(self, class_id: str) -> list[dict]:
        self._class(class_id)
        with self._errors():
            return [
                {**dict(row), "status": "ready"}
                for row in self.connection.execute(
                    f"SELECT {_DOCUMENT_COLUMNS} FROM documents WHERE class_id = ? "
                    "ORDER BY created_at, id",
                    (class_id,),
                )
            ]

    def document(self, class_id: str, document_id: str) -> dict:
        _identifier(class_id)
        _identifier(document_id)
        with self._errors():
            row = self.connection.execute(
                f"SELECT {_DOCUMENT_COLUMNS} FROM documents WHERE class_id = ? AND id = ?",
                (class_id, document_id),
            ).fetchone()
        if row is None:
            raise LibraryError(
                "document_missing", "This document is no longer in the selected class."
            )
        return {**dict(row), "status": "ready"}

    def import_document(
        self,
        class_id: str,
        filename: str,
        original: bytes,
        units: list[ParsedUnit],
        page_count: int,
    ) -> dict:
        self._class(class_id)
        filename = _display_text(filename, limit=MAX_FILENAME_CHARS, filename=True)
        if not isinstance(original, bytes) or not 0 < len(original) <= MAX_FILE_BYTES:
            raise LibraryError("invalid_file", "Choose a nonempty PDF/PPTX of at most 10 MiB.")
        if type(page_count) is not int or not 1 <= page_count <= MAX_PAGES:
            raise LibraryError("invalid_pages", "A document must have between one and 500 pages.")
        if not isinstance(units, list) or len(units) > page_count:
            raise LibraryError("invalid_pages", "The extracted document pages are invalid.")
        pages = {}
        text_chars = 0
        normalized_units = []
        for unit in units:
            if (
                not isinstance(unit, ParsedUnit)
                or type(unit.page_or_slide) is not int
                or not 1 <= unit.page_or_slide <= page_count
                or unit.page_or_slide in pages
                or not isinstance(unit.text, str)
                or "\x00" in unit.text
            ):
                raise LibraryError("invalid_pages", "The extracted document pages are invalid.")
            text_chars += len(unit.text)
            if text_chars > MAX_EXTRACTED_CHARS:
                raise LibraryError(
                    "document_too_large", "The extracted document exceeds local limits."
                )
            try:
                unit.text.encode("utf-8")
            except UnicodeError as exc:
                raise LibraryError(
                    "invalid_pages", "The extracted document text is invalid."
                ) from exc
            pages[unit.page_or_slide] = unit.text
            # Caller-supplied parser filenames never override the admitted source.
            normalized_units.append(ParsedUnit(unit.text, filename, unit.page_or_slide))
        if not any(text.strip() for text in pages.values()):
            raise LibraryError("empty_document", "This document has no extractable text to search.")
        chunks = chunk_units(sorted(normalized_units, key=lambda unit: unit.page_or_slide))
        if len(chunks) > MAX_CHUNKS or any(len(chunk.text) > MAX_CHUNK_CHARS for chunk in chunks):
            raise LibraryError("document_too_large", "The extracted document exceeds local limits.")
        digest = hashlib.sha256(original).hexdigest()
        document_id = str(uuid4())
        # Parsing/chunking has finished before opening the write transaction.
        with self._transaction() as connection:
            # Recheck scope under the write lock: a class may have been deleted
            # by another process while this document was being prepared.
            self._class(class_id)
            previous = connection.execute(
                "SELECT id FROM documents WHERE class_id = ? AND sha256 = ?", (class_id, digest)
            ).fetchone()
            if previous is not None:
                document_id = previous[0]
            else:
                connection.execute(
                    "INSERT INTO documents(id, class_id, filename, sha256, original, byte_count, "
                    "page_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        document_id,
                        class_id,
                        filename,
                        digest,
                        original,
                        len(original),
                        page_count,
                        _timestamp(),
                    ),
                )
                connection.executemany(
                    "INSERT INTO pages(document_id, page_or_slide, text) VALUES (?, ?, ?)",
                    [(document_id, page, pages.get(page, "")) for page in range(1, page_count + 1)],
                )
                ordinals: dict[int, int] = {}
                for chunk in chunks:
                    ordinal = ordinals.get(chunk.page_or_slide, 0)
                    ordinals[chunk.page_or_slide] = ordinal + 1
                    identity = json.dumps(
                        [document_id, digest, chunk.page_or_slide, ordinal, chunk.text],
                        ensure_ascii=True,
                    ).encode("utf-8")
                    chunk_id = hashlib.sha256(identity).hexdigest()
                    cursor = connection.execute(
                        "INSERT INTO chunks(id, document_id, page_or_slide, ordinal, text) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (chunk_id, document_id, chunk.page_or_slide, ordinal, chunk.text),
                    )
                    connection.execute(
                        "INSERT INTO chunks_fts(rowid, text, file) VALUES (?, ?, ?)",
                        (cursor.lastrowid, chunk.text, filename),
                    )
        return self.document(class_id, document_id)

    def pages(self, class_id: str, document_id: str) -> list[dict]:
        self.document(class_id, document_id)
        with self._errors():
            return [
                dict(row)
                for row in self.connection.execute(
                    "SELECT page_or_slide, text FROM pages WHERE document_id = ? "
                    "ORDER BY page_or_slide",
                    (document_id,),
                )
            ]

    def original(self, class_id: str, document_id: str) -> tuple[str, bytes]:
        self.document(class_id, document_id)
        with self._errors():
            row = self.connection.execute(
                "SELECT filename, CASE WHEN typeof(original) = 'blob' AND length(original) <= ? "
                "THEN original ELSE NULL END, sha256, byte_count "
                "FROM documents WHERE class_id = ? AND id = ?",
                (MAX_FILE_BYTES, class_id, document_id),
            ).fetchone()
        if row is None:
            raise LibraryError(
                "document_missing", "This document is no longer in the selected class."
            )
        original = row[1]
        if (
            not isinstance(original, bytes)
            or not 0 < len(original) == row[3] <= MAX_FILE_BYTES
            or hashlib.sha256(original).hexdigest() != row[2]
        ):
            raise LibraryError("invalid_library", "The saved original needs recovery before use.")
        return row[0], original

    def citation(self, class_id: str, chunk_id: str) -> dict:
        """Resolve a citation by saved identity, never by an ambiguous filename."""
        _identifier(class_id)
        if not isinstance(chunk_id, str) or re.fullmatch(r"[0-9a-f]{64}", chunk_id) is None:
            raise LibraryError("invalid_citation", "Choose a citation from the current answer.")
        with self._errors():
            row = self.connection.execute(
                "SELECT d.id AS document_id, d.filename, c.page_or_slide, c.text "
                "FROM chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE d.class_id = ? AND c.id = ?",
                (class_id, chunk_id),
            ).fetchone()
        if row is None:
            raise LibraryError("citation_missing", "This cited source is no longer in the class.")
        return dict(row)

    def delete_document(self, class_id: str, document_id: str) -> None:
        with self._transaction() as connection:
            self.document(class_id, document_id)
            connection.execute(
                "DELETE FROM chunks_fts WHERE rowid IN "
                "(SELECT rowid FROM chunks WHERE document_id = ?)",
                (document_id,),
            )
            connection.execute(
                "DELETE FROM documents WHERE id = ? AND class_id = ?", (document_id, class_id)
            )

    def delete_class(self, class_id: str) -> None:
        with self._transaction() as connection:
            self._class(class_id)
            connection.execute(
                "DELETE FROM chunks_fts WHERE rowid IN (SELECT c.rowid FROM chunks c "
                "JOIN documents d ON d.id = c.document_id WHERE d.class_id = ?)",
                (class_id,),
            )
            connection.execute("DELETE FROM classes WHERE id = ?", (class_id,))

    def backup(self, destination: str | Path) -> None:
        """Create a consistent, private backup; refuse to overwrite any existing file."""
        connection = self.connection
        target = Path(destination).absolute()
        if target == self.path:
            raise LibraryError("invalid_backup", "Choose a new file for the library backup.")
        created = False
        try:
            _private_file(target, exclusive=True)
            created = True
            # Connection's own context manager commits but does not close.
            with contextlib.closing(sqlite3.connect(target)) as backup_connection:
                connection.backup(backup_connection)
        except (OSError, sqlite3.Error) as exc:
            if created:
                target.unlink(missing_ok=True)
            raise LibraryError(
                "backup_failed", "The backup could not finish. Choose a new file."
            ) from exc

    def retrieve(
        self,
        class_id: str,
        question: str,
        document_id: str | None = None,
        pages: list[int] | None = None,
        k: int = search.MAX_RETRIEVED_CHUNKS,
    ) -> list[RetrievedChunk]:
        self._class(class_id)
        if document_id is not None:
            document = self.document(class_id, document_id)
            if pages is not None and (
                not isinstance(pages, list)
                or any(
                    type(page) is not int or not 1 <= page <= document["page_count"]
                    for page in pages
                )
            ):
                raise LibraryError(
                    "invalid_pages", "Choose existing pages in the selected document."
                )
        try:
            with self._errors():
                return search.retrieve(
                    self.connection, class_id, question, document_id=document_id, pages=pages, k=k
                )
        except ValueError as exc:
            raise LibraryError("invalid_search", str(exc)) from exc
