from __future__ import annotations

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


class DamagedLibraryError(LibraryError):
    pass


class UnknownClassError(LibraryError):
    pass


class ClassNameError(LibraryError):
    pass


@dataclass(frozen=True)
class LibraryClass:
    id: str
    name: str


_SCHEMA_QUERY = (
    "SELECT type, name, tbl_name, sql FROM sqlite_schema "
    "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
)


def _schema_of(connection: sqlite3.Connection) -> frozenset[tuple[str, str, str, str]]:
    return frozenset(connection.execute(_SCHEMA_QUERY))


@cache
def _expected_schema() -> frozenset[tuple[str, str, str, str]]:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in STATEMENTS:
            connection.execute(statement)
        return _schema_of(connection)
    finally:
        connection.close()


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
    return name


class Library:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        try:
            self._connection = sqlite3.connect(self.path, isolation_level=None)
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

    def _state(self) -> tuple[int, int, frozenset[tuple[str, str, str, str]]]:
        application_id = self._connection.execute("PRAGMA application_id").fetchone()[0]
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        return application_id, version, _schema_of(self._connection)

    def _prepare(self) -> None:
        try:
            state = self._state()
            if state == (0, 0, frozenset()):
                with self._write():
                    state = self._state()
                    if state == (0, 0, frozenset()):
                        for statement in STATEMENTS:
                            self._connection.execute(statement)
                        self._connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                        self._connection.execute(f"PRAGMA user_version = {VERSION}")
                        return
        except sqlite3.OperationalError as exc:
            raise LibraryError(
                f"Could not use {self.path}; check it is not read-only or open in another app."
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise NotALibraryError(f"{self.path} is not a class library; choose another.") from exc
        application_id, version, schema = state
        if application_id != APPLICATION_ID:
            raise NotALibraryError(f"{self.path} is not a class library; choose another.")
        if version > VERSION:
            raise NewerLibraryError(
                f"{self.path} was made by a newer version of the tutor; "
                "update the tutor to open it."
            )
        if version != VERSION or schema != _expected_schema():
            raise DamagedLibraryError(
                f"{self.path} does not have the expected tables; restore it from a backup."
            )

    @contextmanager
    def _write(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    @contextmanager
    def _saving(self) -> Iterator[None]:
        try:
            with self._write():
                yield
        except sqlite3.Error as exc:
            raise LibraryError(
                "The library could not save that change; check the disk has free space."
            ) from exc

    def create_class(self, name: str) -> LibraryClass:
        created = LibraryClass(id=str(uuid.uuid4()), name=_checked_name(name))
        with self._saving():
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
        with self._saving():
            cursor = self._connection.execute(
                "UPDATE classes SET name = ? WHERE id = ?", (renamed.name, renamed.id)
            )
            if cursor.rowcount == 0:
                raise UnknownClassError("That class is not in this library; refresh the list.")
        return renamed

    def delete_class(self, class_id: str) -> None:
        with self._saving():
            cursor = self._connection.execute("DELETE FROM classes WHERE id = ?", (class_id,))
            if cursor.rowcount == 0:
                raise UnknownClassError("That class is not in this library; refresh the list.")

    def check(self) -> list[str]:
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
        except sqlite3.DatabaseError:
            problems.append("The keyword-search index does not match the passages.")
        return problems
