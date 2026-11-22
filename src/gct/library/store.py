from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from gct.ingest.chunk import chunk_units
from gct.ingest.parse import ParsedUnit
from gct.library.schema import APPLICATION_ID, STATEMENTS, VERSION


class LibraryError(Exception):
    pass


class NotALibraryError(LibraryError):
    pass


class NewerLibraryError(LibraryError):
    pass


class OutdatedLibraryError(LibraryError):
    pass


class UnknownClassError(LibraryError):
    pass


class ClassNameError(LibraryError):
    pass


class UnknownDocumentError(LibraryError):
    pass


class UnknownPassageError(LibraryError):
    pass


class DocumentContentError(LibraryError):
    pass


@dataclass(frozen=True)
class LibraryClass:
    id: str
    name: str


@dataclass(frozen=True)
class Document:
    id: str
    class_id: str
    filename: str
    sha256: str
    page_count: int


@dataclass(frozen=True)
class SavedDocument:
    document: Document
    already_in_class: bool


@dataclass(frozen=True)
class Page:
    page_or_slide: int
    text: str


@dataclass(frozen=True)
class Passage:
    id: str
    document_id: str
    filename: str
    page_or_slide: int
    text: str


_OWNED_TABLES = ("classes", "documents", "pages", "passages", "passages_fts")

Shape = tuple[object, ...]


def _normalised(sql: str | None) -> str | None:
    if sql is None:
        return None
    return re.sub(r"\s*([(),=<>;'])\s*", r"\1", " ".join(sql.split()))


def _shape(connection: sqlite3.Connection) -> Shape:
    names = tuple(
        sorted(
            (kind, name, table, _normalised(sql))
            for kind, name, table, sql in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_schema "
                "WHERE type <> 'index' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
                "AND NOT (type = 'table' AND name LIKE 'passages\\_fts\\_%' ESCAPE '\\')"
            )
        )
    )
    tables = []
    for table in _OWNED_TABLES:
        unique_keys = sorted(
            (
                unique,
                origin,
                partial,
                tuple(row[2] for row in connection.execute(f"PRAGMA index_info('{index}')")),
            )
            for _, index, unique, origin, partial in connection.execute(
                f"PRAGMA index_list('{table}')"
            )
        )
        tables.append(
            (
                table,
                tuple(connection.execute(f"PRAGMA table_xinfo('{table}')")),
                tuple(connection.execute(f"PRAGMA foreign_key_list('{table}')")),
                tuple(unique_keys),
            )
        )
    return names, tuple(tables)


@cache
def _expected_shape() -> Shape:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in STATEMENTS:
            connection.execute(statement)
        return _shape(connection)
    finally:
        connection.close()


def _cause(exc: sqlite3.Error) -> str:
    text = str(exc).lower()
    if "locked" in text or "busy" in text:
        return "another window is using the library; close it and try again"
    if "readonly" in text or "read-only" in text:
        return "the library file is read-only; make it writable and try again"
    if "disk is full" in text:
        return "the disk is full; free some space and try again"
    return "something went wrong; try again, and run a check if it keeps happening"


def _checked_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ClassNameError("Give the class a name with at least one visible character.")
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise ClassNameError(
            "Retype the class name; it contains characters that cannot be saved."
        ) from None
    if "\x00" in name:
        raise ClassNameError("Remove the NUL character from the class name.")
    return name.strip()


def _page_texts(page_count: int, units: Sequence[ParsedUnit]) -> list[str]:
    if type(page_count) is not int or page_count < 1:
        raise DocumentContentError("A document needs at least one page.")
    texts = [""] * page_count
    seen: set[int] = set()
    for unit in units:
        page = unit.page_or_slide
        if type(page) is not int or not 1 <= page <= page_count or page in seen:
            raise DocumentContentError(
                f"Page {page!r} does not fit a document of {page_count} pages, or appears twice."
            )
        try:
            unit.text.encode("utf-8")
        except UnicodeEncodeError:
            raise DocumentContentError(
                f"Page {page} contains characters that cannot be saved."
            ) from None
        seen.add(page)
        texts[page - 1] = unit.text
    return texts


def _stable_id(class_id: str, sha256: str, page: int, ordinal: int, text: str) -> str:
    identity = json.dumps([class_id, sha256, page, ordinal, text], ensure_ascii=True)
    return hashlib.sha256(identity.encode("ascii")).hexdigest()


class Library:
    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = Path(path)
        try:
            self._connection = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        except sqlite3.Error as exc:
            raise LibraryError(f"Could not open {self.path}; check the folder exists.") from exc
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            if self._connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                raise LibraryError("This Python's SQLite cannot enforce foreign keys.")
            self._prepare()
        except BaseException:
            self._connection.close()
            raise

    def __enter__(self) -> Library:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def _stamp(self) -> tuple[int, int, bool]:
        application_id = self._connection.execute("PRAGMA application_id").fetchone()[0]
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        empty = self._connection.execute("SELECT count(*) FROM sqlite_schema").fetchone() == (0,)
        return application_id, version, empty

    def _prepare(self) -> None:
        try:
            stamp = self._stamp()
        except sqlite3.OperationalError as exc:
            raise LibraryError(f"Could not open {self.path}: {_cause(exc)}.") from exc
        except sqlite3.DatabaseError as exc:
            raise NotALibraryError(f"{self.path} is not a class library; choose another.") from exc
        if stamp == (0, 0, True):
            with self._transaction(f"Could not create a library at {self.path}"):
                stamp = self._stamp()
                if stamp == (0, 0, True):
                    for statement in STATEMENTS:
                        self._connection.execute(statement)
                    self._connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                    self._connection.execute(f"PRAGMA user_version = {VERSION}")
                    stamp = (APPLICATION_ID, VERSION, False)
        application_id, version, _ = stamp
        if application_id != APPLICATION_ID:
            raise NotALibraryError(f"{self.path} is not a class library; choose another.")
        if version > VERSION:
            raise NewerLibraryError(
                f"{self.path} was made by a newer version of the tutor; "
                "update the tutor to open it."
            )
        if version != VERSION or _shape(self._connection) != _expected_shape():
            raise OutdatedLibraryError(
                f"{self.path} was made by an earlier build of the tutor and cannot be opened; "
                "create a new library and import your course files again."
            )

    @contextmanager
    def _transaction(self, failure: str = "The change was not saved") -> Iterator[None]:
        try:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield
                self._connection.execute("COMMIT")
            finally:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
        except sqlite3.Error as exc:
            raise LibraryError(f"{failure}: {_cause(exc)}.") from exc

    def _refuse_taken_name(self, name: str, *, except_id: str | None = None) -> None:
        taken = self._connection.execute(
            "SELECT 1 FROM classes WHERE name = ? AND id IS NOT ?", (name, except_id)
        ).fetchone()
        if taken is not None:
            raise ClassNameError(f"A class called {name!r} already exists; choose another name.")

    def create_class(self, name: str) -> LibraryClass:
        created = LibraryClass(id=str(uuid.uuid4()), name=_checked_name(name))
        with self._transaction():
            self._refuse_taken_name(created.name)
            self._connection.execute(
                "INSERT INTO classes (id, name) VALUES (?, ?)", (created.id, created.name)
            )
        return created

    def list_classes(self) -> list[LibraryClass]:
        rows = self._connection.execute(
            "SELECT id, name FROM classes ORDER BY name COLLATE NOCASE, name, id"
        )
        return [LibraryClass(id=row[0], name=row[1]) for row in rows]

    def rename_class(self, class_id: str, name: str) -> LibraryClass:
        renamed = LibraryClass(id=class_id, name=_checked_name(name))
        with self._transaction():
            self._refuse_taken_name(renamed.name, except_id=renamed.id)
            cursor = self._connection.execute(
                "UPDATE classes SET name = ? WHERE id = ?", (renamed.name, renamed.id)
            )
            if cursor.rowcount == 0:
                raise UnknownClassError("That class is not in this library; refresh the list.")
        return renamed

    def delete_class(self, class_id: str) -> None:
        with self._transaction():
            cursor = self._connection.execute("DELETE FROM classes WHERE id = ?", (class_id,))
            if cursor.rowcount == 0:
                raise UnknownClassError("That class is not in this library; refresh the list.")

    @contextmanager
    def _reading(self, failure: str) -> Iterator[None]:
        try:
            yield
        except sqlite3.Error as exc:
            raise LibraryError(f"{failure}: {_cause(exc)}.") from exc

    def _refuse_unknown_class(self, class_id: str) -> None:
        if self._connection.execute("SELECT 1 FROM classes WHERE id = ?", (class_id,)).fetchone():
            return
        raise UnknownClassError("That class is not in this library; refresh the list.")

    def _document(self, class_id: str, document_id: str) -> Document:
        row = self._connection.execute(
            "SELECT id, class_id, filename, sha256, page_count FROM documents "
            "WHERE id = ? AND class_id = ?",
            (document_id, class_id),
        ).fetchone()
        if row is None:
            raise UnknownDocumentError("That document is not in this class; refresh the list.")
        return Document(*row)

    def save_document(
        self,
        class_id: str,
        filename: str,
        original: bytes,
        page_count: int,
        units: Sequence[ParsedUnit],
    ) -> SavedDocument:
        if not isinstance(filename, str) or not filename.strip():
            raise DocumentContentError("A document needs a filename.")
        if not isinstance(original, bytes) or not original:
            raise DocumentContentError("A document needs the original file's bytes.")
        texts = _page_texts(page_count, units)
        if not any(text.strip() for text in texts):
            raise DocumentContentError(f"{filename} has no text to search.")
        sha256 = hashlib.sha256(original).hexdigest()
        chunks = chunk_units(
            [
                ParsedUnit(text=text, file=filename, page_or_slide=page)
                for page, text in enumerate(texts, start=1)
            ]
        )
        saved = Document(
            id=str(uuid.uuid4()),
            class_id=class_id,
            filename=filename,
            sha256=sha256,
            page_count=page_count,
        )
        ordinals: dict[int, int] = {}
        passages = []
        for chunk in chunks:
            ordinal = ordinals.get(chunk.page_or_slide, 0)
            ordinals[chunk.page_or_slide] = ordinal + 1
            passages.append(
                (
                    _stable_id(class_id, sha256, chunk.page_or_slide, ordinal, chunk.text),
                    saved.id,
                    chunk.page_or_slide,
                    ordinal,
                    chunk.text,
                )
            )
        with self._transaction(f"{filename} was not saved"):
            self._refuse_unknown_class(class_id)
            existing = self._connection.execute(
                "SELECT id FROM documents WHERE class_id = ? AND sha256 = ?", (class_id, sha256)
            ).fetchone()
            if existing is not None:
                return SavedDocument(self._document(class_id, existing[0]), already_in_class=True)
            self._connection.execute(
                "INSERT INTO documents (id, class_id, filename, sha256, page_count, original) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (saved.id, class_id, filename, sha256, page_count, original),
            )
            self._connection.executemany(
                "INSERT INTO pages (document_id, page_count, page_or_slide, text) "
                "VALUES (?, ?, ?, ?)",
                [(saved.id, page_count, page, text) for page, text in enumerate(texts, start=1)],
            )
            self._connection.executemany(
                "INSERT INTO passages (stable_id, document_id, page_or_slide, ordinal, text) "
                "VALUES (?, ?, ?, ?, ?)",
                passages,
            )
        return SavedDocument(saved, already_in_class=False)

    def list_documents(self, class_id: str) -> list[Document]:
        with self._reading("The documents could not be listed"):
            rows = self._connection.execute(
                "SELECT documents.id, classes.id, documents.filename, documents.sha256, "
                "documents.page_count "
                "FROM classes LEFT JOIN documents ON documents.class_id = classes.id "
                "WHERE classes.id = ? "
                "ORDER BY documents.filename COLLATE NOCASE, documents.filename, documents.sha256",
                (class_id,),
            ).fetchall()
        if not rows:
            raise UnknownClassError("That class is not in this library; refresh the list.")
        return [Document(*row) for row in rows if row[0] is not None]

    def document_pages(self, class_id: str, document_id: str) -> list[Page]:
        with self._reading("The pages could not be read"):
            rows = self._connection.execute(
                "SELECT pages.page_or_slide, pages.text "
                "FROM documents JOIN pages ON pages.document_id = documents.id "
                "WHERE documents.id = ? AND documents.class_id = ? "
                "ORDER BY pages.page_or_slide",
                (document_id, class_id),
            ).fetchall()
        if not rows:
            raise UnknownDocumentError("That document is not in this class; refresh the list.")
        return [Page(*row) for row in rows]

    def delete_document(self, class_id: str, document_id: str) -> None:
        with self._transaction("The document was not deleted"):
            cursor = self._connection.execute(
                "DELETE FROM documents WHERE id = ? AND class_id = ?", (document_id, class_id)
            )
            if cursor.rowcount == 0:
                raise UnknownDocumentError("That document is not in this class; refresh the list.")

    def passage(self, class_id: str, passage_id: str) -> Passage:
        with self._reading("The passage could not be read"):
            row = self._connection.execute(
                "SELECT passages.stable_id, documents.id, documents.filename, "
                "passages.page_or_slide, passages.text "
                "FROM passages JOIN documents ON documents.id = passages.document_id "
                "WHERE passages.stable_id = ? AND documents.class_id = ?",
                (passage_id, class_id),
            ).fetchone()
        if row is None:
            raise UnknownPassageError(
                "That passage is not in this class; it may have been deleted."
            )
        return Passage(*row)

    def check(self) -> list[str]:
        try:
            problems = [
                f"SQLite integrity check: {row[0]}"
                for row in self._connection.execute("PRAGMA integrity_check")
                if row[0] != "ok"
            ]
            problems += [
                f"A row in {table} points at a {parent} row that does not exist."
                for table, _, parent, _ in self._connection.execute("PRAGMA foreign_key_check")
            ]
            try:
                self._connection.execute(
                    "INSERT INTO passages_fts (passages_fts, rank) VALUES ('integrity-check', 1)"
                )
            except sqlite3.OperationalError:
                raise
            except sqlite3.DatabaseError:
                problems.append("The keyword-search index does not match the passages.")
        except sqlite3.OperationalError as exc:
            raise LibraryError(f"The check could not run: {_cause(exc)}.") from exc
        return problems
