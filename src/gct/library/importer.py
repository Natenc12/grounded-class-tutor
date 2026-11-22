from __future__ import annotations

import errno
import math
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pptx import Presentation
from pypdf import PdfReader

from gct.ingest.parse import ParsedUnit, ParseError, parse_file
from gct.library.store import (
    Document,
    DocumentContentError,
    Library,
    LibraryError,
    UnknownClassError,
)

MAX_FILE_BYTES = 250_000_000
MAX_PDF_PAGES = 2_000
MAX_SLIDES = 500

_DISK_FULL = (errno.ENOSPC, errno.EDQUOT)


class RefusalReason(Enum):
    UNSUPPORTED = "unsupported"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    TOO_BIG = "too_big"
    TOO_MANY_PAGES = "too_many_pages"
    PROTECTED = "protected"
    UNPARSEABLE = "unparseable"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    DISK_FULL = "disk_full"
    NO_WORKING_COPY = "no_working_copy"
    CLASS_MISSING = "class_missing"
    NOT_SAVED = "not_saved"


@dataclass(frozen=True)
class Imported:
    document: Document
    already_in_class: bool

    @property
    def message(self) -> str:
        name = self.document.filename
        if self.already_in_class:
            return f"{name} is already in this class, so nothing was added."
        kind = _KINDS[Path(name).suffix.lower()]
        return f"Imported {name} ({_units(self.document.page_count, kind.unit)})."


@dataclass(frozen=True)
class Refused:
    reason: RefusalReason
    message: str


@dataclass(frozen=True)
class _Kind:
    noun: str
    unit: str
    max_units: int
    split: str
    too_long: str
    count: Callable[[Path], int]


_KINDS = {
    ".pdf": _Kind(
        noun="PDF",
        unit="page",
        max_units=MAX_PDF_PAGES,
        split="Split it into smaller PDFs and import each one.",
        too_long="{name} has a page too detailed for the tutor to read: it unpacks to more than "
        "16 MB. Make a fresh copy with File > Print > Save as PDF, or split off the pages you "
        "need, and import that.",
        count=lambda path: len(PdfReader(path).pages),
    ),
    ".pptx": _Kind(
        noun="PowerPoint file",
        unit="slide",
        max_units=MAX_SLIDES,
        split="Split it into smaller decks and import each one.",
        too_long="{name} is too big for the tutor once unpacked: the limits are 1 GB in all, "
        "512 MB for any one picture or video, and 10,000 slides, pictures and other pieces. "
        "Remove large videos or pictures, or split the deck, and import it again.",
        count=lambda path: len(Presentation(str(path)).slides),
    ),
}


class _Refusal(Exception):
    def __init__(self, reason: RefusalReason, message: str) -> None:
        super().__init__(message)
        self.refused = Refused(reason, message)


def import_file(library: Library, class_id: str, path: str | Path) -> Imported | Refused:
    try:
        return _import(library, class_id, Path(path))
    except _Refusal as refusal:
        return refusal.refused


def _import(library: Library, class_id: str, path: Path) -> Imported:
    name = path.name
    kind = _KINDS.get(path.suffix.lower())
    if kind is None:
        raise _Refusal(RefusalReason.UNSUPPORTED, _not_a_pdf_or_pptx(name))
    original = _read(path)
    with (
        _working_copy(name),
        tempfile.TemporaryDirectory(prefix="gct-import-", ignore_cleanup_errors=True) as folder,
    ):
        snapshot = Path(folder) / name
        snapshot.write_bytes(original)
        units = _parse(snapshot, kind)
        count = kind.count(snapshot)
    if count > kind.max_units:
        raise _Refusal(
            RefusalReason.TOO_MANY_PAGES,
            f"{name} has {_units(count, kind.unit)}, and the tutor takes up to "
            f"{_units(kind.max_units, kind.unit)} per file. {kind.split}",
        )
    try:
        saved = library.save_document(class_id, name, original, count, units)
    except UnknownClassError as exc:
        raise _Refusal(RefusalReason.CLASS_MISSING, str(exc)) from exc
    except DocumentContentError as exc:
        raise _Refusal(
            RefusalReason.UNPARSEABLE,
            f"{name} has text the tutor cannot store, which usually means the file is damaged. "
            "Open it and save a fresh copy, then import that.",
        ) from exc
    except LibraryError as exc:
        raise _Refusal(RefusalReason.NOT_SAVED, str(exc)) from exc
    return Imported(saved.document, saved.already_in_class)


def _read(path: Path) -> bytes:
    name = path.name
    original = b""
    try:
        with path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            if size <= MAX_FILE_BYTES:
                original = source.read(MAX_FILE_BYTES + 1)
                size = len(original)
    except FileNotFoundError as exc:
        raise _Refusal(
            RefusalReason.MISSING,
            f"{name} is not there any more. Check that it has not been moved, renamed or "
            "deleted, then try again.",
        ) from exc
    except OSError as exc:
        raise _Refusal(
            RefusalReason.UNREADABLE,
            f"{name} could not be opened. Check that it is downloaded to this Mac and that you "
            "have permission to open it, then try again.",
        ) from exc
    if size > MAX_FILE_BYTES:
        raise _Refusal(
            RefusalReason.TOO_BIG,
            f"{name} is {math.ceil(size / 1_000_000):,} MB, and the tutor takes files up to "
            f"{MAX_FILE_BYTES // 1_000_000:,} MB. Split it into smaller files, or save a copy "
            "with smaller pictures, and import those.",
        )
    return original


@contextmanager
def _working_copy(name: str) -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        if exc.errno in _DISK_FULL:
            raise _Refusal(
                RefusalReason.DISK_FULL,
                f"There is not enough disk space to import {name}. "
                "Free some space on this Mac, then try again.",
            ) from exc
        raise _Refusal(
            RefusalReason.NO_WORKING_COPY,
            f"The tutor could not make a working copy of {name} on this Mac. "
            "Try again; if it keeps happening, restart the Mac.",
        ) from exc


def _parse(snapshot: Path, kind: _Kind) -> list[ParsedUnit]:
    name = snapshot.name
    try:
        return parse_file(snapshot)
    except ParseError as exc:
        reason = RefusalReason(exc.reason)
        raise _Refusal(reason, _parse_message(reason, name, kind)) from exc


def _parse_message(reason: RefusalReason, name: str, kind: _Kind) -> str:
    if reason is RefusalReason.PROTECTED:
        return (
            f"{name} is password-protected. Open it, save a copy without the password, "
            "and import that copy."
        )
    if reason is RefusalReason.EMPTY:
        return (
            f"{name} has no text the tutor can search; it is probably a scan or made only of "
            "pictures. Use a version with text you can select, or run text recognition (OCR) "
            "on it first."
        )
    if reason is RefusalReason.TOO_LONG:
        return kind.too_long.format(name=name)
    if reason is RefusalReason.UNSUPPORTED:
        return _not_a_pdf_or_pptx(name)
    return (
        f"{name} could not be read. It may be damaged, only partly downloaded, or not really "
        f"a {kind.noun}. Download it again, or open it and save a fresh copy, then import that."
    )


def _not_a_pdf_or_pptx(name: str) -> str:
    return (
        f"{name} is not a PDF or PowerPoint (.pptx) file. "
        "Save or export it as a PDF, then import that."
    )


def _units(count: int, unit: str) -> str:
    return f"{count:,} {unit}{'' if count == 1 else 's'}"
