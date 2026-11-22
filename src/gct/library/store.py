from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path

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


@dataclass(frozen=True)
class LibraryClass:
    id: str
    name: str


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
