"""Importing a PDF or PowerPoint file into a class (#229).

Every file here is generated in the test. A refusal must leave the library exactly as it was, so
each refusal test starts from a library that already holds one document and checks it again after.
"""

from __future__ import annotations

import errno
import io
import os
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from pptx import Presentation
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import gct.ingest.parse as parse
from gct.library import Imported, Library, RefusalReason, Refused, import_file
from gct.library.importer import MAX_FILE_BYTES, MAX_PDF_PAGES, MAX_SLIDES

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def write_pdf(path: Path, pages: list[str | None]) -> Path:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(612, 792))
    for text in pages:
        if text is not None:
            pdf.drawString(72, 700, text)
        pdf.showPage()
    pdf.save()
    path.write_bytes(buffer.getvalue())
    return path


def write_scanned_pdf(path: Path, page_count: int) -> Path:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(612, 792))
    scan = ImageReader(Image.new("RGB", (200, 260), (235, 230, 220)))
    for _ in range(page_count):
        pdf.drawImage(scan, 36, 36, width=540, height=720)
        pdf.showPage()
    pdf.save()
    path.write_bytes(buffer.getvalue())
    return path


def write_encrypted_pdf(path: Path) -> Path:
    from pypdf import PdfReader, PdfWriter

    plain = write_pdf(path, ["Exam answers"])
    writer = PdfWriter()
    for page in PdfReader(plain).pages:
        writer.add_page(page)
    writer.encrypt(user_password="secret")
    with open(path, "wb") as out:
        writer.write(out)
    return path


def write_restricted_pdf(path: Path, pages: list[str | None], algorithm: str) -> Path:
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(write_pdf(path, pages)))
    writer.encrypt(user_password="", owner_password="prof", algorithm=algorithm)
    with open(path, "wb") as out:
        writer.write(out)
    return path


def write_pptx(path: Path, slides: list[str | None], notes: dict[int, str] | None = None) -> Path:
    deck = Presentation()
    for number, text in enumerate(slides, start=1):
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        if text is not None:
            box = slide.shapes.add_textbox(0, 0, deck.slide_width, deck.slide_height)
            box.text_frame.text = text
        if notes and number in notes:
            slide.notes_slide.notes_text_frame.text = notes[number]
    deck.save(str(path))
    return path


def cut_in_half(path: Path) -> Path:
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    return path


def search(raw_connection: sqlite3.Connection, word: str) -> list[str]:
    rows = raw_connection.execute(
        "SELECT passages.stable_id FROM passages_fts "
        "JOIN passages ON passages.id = passages_fts.rowid WHERE passages_fts MATCH ?",
        (word,),
    )
    return [row[0] for row in rows]


@pytest.fixture
def biology(library: Library) -> str:
    return library.create_class("Biology 101").id


@pytest.fixture
def files(tmp_path: Path) -> Path:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    return folder


def test_a_pdf_keeps_its_real_page_numbers_across_blank_pages(
    library, biology, files, raw_connection
):
    path = write_pdf(
        files / "Cells.pdf",
        [None, "Photosynthesis happens in chloroplasts", None, "Mitochondria make ATP", None],
    )

    result = import_file(library, biology, path)

    assert isinstance(result, Imported)
    assert not result.already_in_class
    assert result.message == "Imported Cells.pdf (5 pages)."
    document = result.document
    assert (document.filename, document.page_count) == ("Cells.pdf", 5)
    assert library.list_documents(biology) == [document]
    pages = library.document_pages(biology, document.id)
    assert [page.page_or_slide for page in pages] == [1, 2, 3, 4, 5]
    assert [bool(page.text.strip()) for page in pages] == [False, True, False, True, False]
    (hit,) = search(raw_connection, "mitochondria")
    cited = library.passage(biology, hit)
    assert (cited.filename, cited.page_or_slide) == ("Cells.pdf", 4)
    (hit,) = search(raw_connection, "chloroplasts")
    assert library.passage(biology, hit).page_or_slide == 2
    stored = raw_connection.execute(
        "SELECT original FROM documents WHERE id = ?", (document.id,)
    ).fetchone()[0]
    assert stored == path.read_bytes()
    assert library.check() == []


def test_a_pptx_keeps_its_real_slide_numbers_across_blank_slides(
    library, biology, files, raw_connection
):
    path = write_pptx(
        files / "Week 3.pptx",
        ["Osmosis moves water", None, None, "Diffusion moves solutes", None, None],
        notes={3: "Ask about turgor pressure"},
    )

    result = import_file(library, biology, path)

    assert isinstance(result, Imported)
    assert result.message == "Imported Week 3.pptx (6 slides)."
    assert result.document.page_count == 6
    for word, slide in (("osmosis", 1), ("turgor", 3), ("solutes", 4)):
        (hit,) = search(raw_connection, word)
        cited = library.passage(biology, hit)
        assert (cited.filename, cited.page_or_slide) == ("Week 3.pptx", slide)
    assert len(library.document_pages(biology, result.document.id)) == 6
    assert library.check() == []


@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-128", "AES-256"])
def test_a_reading_with_copy_or_print_restrictions_imports(
    library, biology, files, raw_connection, algorithm
):
    path = write_restricted_pdf(
        files / "Reading.pdf", ["Stomata open in light", None, "Guard cells swell"], algorithm
    )
    assert parse.PdfReader(path).is_encrypted

    result = import_file(library, biology, path)

    assert isinstance(result, Imported)
    assert result.document.page_count == 3
    for word, page in (("stomata", 1), ("guard", 3)):
        (hit,) = search(raw_connection, word)
        assert library.passage(biology, hit).page_or_slide == page
    assert [unit.page_or_slide for unit in parse.parse_file(path)] == [1, 3]
    assert library.check() == []


def test_importing_the_same_file_twice_gives_one_document(library, biology, files):
    path = write_pdf(files / "Cells.pdf", ["Mitochondria make ATP"])
    first = import_file(library, biology, path)
    renamed = files / "Cells (1).pdf"
    renamed.write_bytes(path.read_bytes())

    again = import_file(library, biology, path)
    copy = import_file(library, biology, renamed)

    assert isinstance(first, Imported) and isinstance(again, Imported)
    assert isinstance(copy, Imported)
    assert again.already_in_class and copy.already_in_class
    assert again.document == copy.document == first.document
    assert again.message == "Cells.pdf is already in this class, so nothing was added."
    assert library.list_documents(biology) == [first.document]
    assert library.check() == []


def test_the_same_file_can_go_into_two_classes(library, biology, files):
    chemistry = library.create_class("Chemistry").id
    path = write_pdf(files / "Cells.pdf", ["Mitochondria make ATP"])

    into_biology = import_file(library, biology, path)
    into_chemistry = import_file(library, chemistry, path)

    assert isinstance(into_biology, Imported) and isinstance(into_chemistry, Imported)
    assert not into_chemistry.already_in_class
    assert into_biology.document.id != into_chemistry.document.id


def test_the_working_copy_is_removed(library, biology, files, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    write_pdf(files / "Cells.pdf", ["Mitochondria make ATP"])
    write_pdf(files / "Broken.pdf", ["Mitochondria make ATP", "More"])
    cut_in_half(files / "Broken.pdf")

    assert isinstance(import_file(library, biology, files / "Cells.pdf"), Imported)
    assert isinstance(import_file(library, biology, files / "Broken.pdf"), Refused)

    assert list(scratch.iterdir()) == []


def _over_size(path: Path) -> Path:
    with open(path, "wb") as out:
        out.truncate(MAX_FILE_BYTES + 1)
    return path


def _html_saved_as_pdf(path: Path) -> Path:
    path.write_text("<!doctype html><html><body>Sign in to continue</body></html>")
    return path


def _word_file_renamed(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    return path


def _folder(path: Path) -> Path:
    path.mkdir()
    return path


REFUSALS = {
    "password-protected PDF": (
        "Exam.pdf",
        write_encrypted_pdf,
        RefusalReason.PROTECTED,
        "Exam.pdf is password-protected. Open it, save a copy without the password, "
        "and import that copy.",
    ),
    "password-protected PowerPoint": (
        "Exam.pptx",
        lambda path: path.write_bytes(OLE_SIGNATURE + b"\x00" * 512) and path,
        RefusalReason.PROTECTED,
        "Exam.pptx is password-protected. Open it, save a copy without the password, "
        "and import that copy.",
    ),
    "scanned PDF with no text": (
        "Scan.pdf",
        lambda path: write_scanned_pdf(path, 3),
        RefusalReason.EMPTY,
        "Scan.pdf has no text the tutor can search; it is probably a scan or made only of "
        "pictures. Use a version with text you can select, or run text recognition (OCR) on it "
        "first.",
    ),
    "PowerPoint of pictures only": (
        "Photos.pptx",
        lambda path: write_pptx(path, [None, None]),
        RefusalReason.EMPTY,
        "Photos.pptx has no text the tutor can search; it is probably a scan or made only of "
        "pictures. Use a version with text you can select, or run text recognition (OCR) on it "
        "first.",
    ),
    "PDF cut off halfway": (
        "Half.pdf",
        lambda path: cut_in_half(write_pdf(path, [f"Page {n} text" for n in range(20)])),
        RefusalReason.UNPARSEABLE,
        "Half.pdf could not be read. It may be damaged, only partly downloaded, or not really "
        "a PDF. Download it again, or open it and save a fresh copy, then import that.",
    ),
    "PowerPoint cut off halfway": (
        "Half.pptx",
        lambda path: cut_in_half(write_pptx(path, [f"Slide {n}" for n in range(20)])),
        RefusalReason.UNPARSEABLE,
        "Half.pptx could not be read. It may be damaged, only partly downloaded, or not really "
        "a PowerPoint file. Download it again, or open it and save a fresh copy, then import "
        "that.",
    ),
    "web page saved as a PDF": (
        "Lecture.pdf",
        _html_saved_as_pdf,
        RefusalReason.UNPARSEABLE,
        "Lecture.pdf could not be read. It may be damaged, only partly downloaded, or not "
        "really a PDF. Download it again, or open it and save a fresh copy, then import that.",
    ),
    "Word file renamed to PDF": (
        "Essay.pdf",
        _word_file_renamed,
        RefusalReason.UNPARSEABLE,
        "Essay.pdf could not be read. It may be damaged, only partly downloaded, or not "
        "really a PDF. Download it again, or open it and save a fresh copy, then import that.",
    ),
    "PDF renamed to PowerPoint": (
        "Deck.pptx",
        lambda path: write_pdf(path, ["Not a deck"]),
        RefusalReason.UNPARSEABLE,
        "Deck.pptx could not be read. It may be damaged, only partly downloaded, or not really "
        "a PowerPoint file. Download it again, or open it and save a fresh copy, then import "
        "that.",
    ),
    "empty file": (
        "Empty.pdf",
        lambda path: path.write_bytes(b"") or path,
        RefusalReason.UNPARSEABLE,
        "Empty.pdf could not be read. It may be damaged, only partly downloaded, or not "
        "really a PDF. Download it again, or open it and save a fresh copy, then import that.",
    ),
    "file over the size limit": (
        "Textbook.pdf",
        _over_size,
        RefusalReason.TOO_BIG,
        "Textbook.pdf is 251 MB, and the tutor takes files up to 250 MB. Split it into smaller "
        "files, or save a copy with smaller pictures, and import those.",
    ),
    "PDF over the page limit": (
        "Reader.pdf",
        lambda path: write_pdf(path, [f"Page {n}" for n in range(MAX_PDF_PAGES + 1)]),
        RefusalReason.TOO_MANY_PAGES,
        "Reader.pdf has 2,001 pages, and the tutor takes up to 2,000 pages per file. "
        "Split it into smaller PDFs and import each one.",
    ),
    "PowerPoint over the slide limit": (
        "Course.pptx",
        lambda path: write_pptx(path, ["Only the first slide has text"] + [None] * MAX_SLIDES),
        RefusalReason.TOO_MANY_PAGES,
        "Course.pptx has 501 slides, and the tutor takes up to 500 slides per file. "
        "Split it into smaller decks and import each one.",
    ),
    "Word document": (
        "Essay.docx",
        _word_file_renamed,
        RefusalReason.UNSUPPORTED,
        "Essay.docx is not a PDF or PowerPoint (.pptx) file. "
        "Save or export it as a PDF, then import that.",
    ),
    "old PowerPoint format": (
        "Old.ppt",
        lambda path: path.write_bytes(OLE_SIGNATURE + b"\x00" * 512) and path,
        RefusalReason.UNSUPPORTED,
        "Old.ppt is not a PDF or PowerPoint (.pptx) file. "
        "Save or export it as a PDF, then import that.",
    ),
    "PDF with no ending on its name": (
        "Reading",
        lambda path: write_pdf(path, ["A real PDF"]),
        RefusalReason.UNSUPPORTED,
        "Reading has no .pdf or .pptx at the end of its name, so the tutor cannot tell what "
        "kind of file it is. Add the right ending to the name and import it again.",
    ),
    "file that has gone": (
        "Moved.pdf",
        lambda path: path,
        RefusalReason.MISSING,
        "Moved.pdf is not there any more. Check that it has not been moved, renamed or "
        "deleted, then try again.",
    ),
    "folder named like a PDF": (
        "Notes.pdf",
        _folder,
        RefusalReason.UNREADABLE,
        "Notes.pdf could not be opened. Check that it is downloaded to this Mac and that you "
        "have permission to open it, then try again.",
    ),
}


@pytest.fixture
def library_with_a_document(library, biology, files):
    existing = import_file(library, biology, write_pdf(files / "Existing.pdf", ["Ribosomes"]))
    assert isinstance(existing, Imported)
    return library.list_documents(biology)


@pytest.mark.parametrize("case", REFUSALS, ids=list(REFUSALS))
def test_a_file_that_cannot_be_used_is_refused_and_changes_nothing(
    library, biology, files, library_with_a_document, case
):
    name, make, reason, message = REFUSALS[case]
    path = make(files / name)

    result = import_file(library, biology, path)

    assert result == Refused(reason, message)
    assert library.list_documents(biology) == library_with_a_document
    assert library.check() == []


def test_an_unreadable_file_is_refused(library, biology, files, library_with_a_document):
    if os.geteuid() == 0:
        pytest.skip("root can read any file")
    path = write_pdf(files / "Locked.pdf", ["Ribosomes"])
    path.chmod(0)
    try:
        result = import_file(library, biology, path)
    finally:
        path.chmod(0o600)

    assert isinstance(result, Refused)
    assert result.reason is RefusalReason.UNREADABLE
    assert library.list_documents(biology) == library_with_a_document


_real_temporary_file = tempfile.TemporaryFile


class _DiskFillsAfter:
    def __init__(self, room: int) -> None:
        self._file = _real_temporary_file()
        self._room = room

    def write(self, data: bytes) -> int:
        if len(data) > self._room:
            raise OSError(errno.ENOSPC, "No space left on device")
        self._room -= len(data)
        return self._file.write(data)

    def __getattr__(self, name: str):
        return getattr(self._file, name)

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self._file.close()


@pytest.mark.parametrize("room", [0, 2_000, 20_000])
def test_a_full_disk_during_the_powerpoint_copy_is_a_plain_refusal(
    library, biology, files, library_with_a_document, monkeypatch, room
):
    path = write_pptx(files / "Week 3.pptx", [f"Slide {n} " + "osmosis " * 200 for n in range(30)])
    monkeypatch.setattr(parse.tempfile, "TemporaryFile", lambda: _DiskFillsAfter(room))

    result = import_file(library, biology, path)

    assert result == Refused(
        RefusalReason.DISK_FULL,
        "There is not enough disk space to import Week 3.pptx. "
        "Free some space on this Mac, then try again.",
    )
    assert library.list_documents(biology) == library_with_a_document
    assert library.check() == []


def test_a_full_disk_while_making_the_working_copy_is_a_plain_refusal(
    library, biology, files, library_with_a_document, monkeypatch
):
    path = write_pdf(files / "Cells.pdf", ["Mitochondria make ATP"])

    def full(self, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", full)
    result = import_file(library, biology, path)

    assert isinstance(result, Refused)
    assert result.reason is RefusalReason.DISK_FULL
    assert library.list_documents(biology) == library_with_a_document


def test_a_full_disk_before_the_working_folder_exists_is_a_plain_refusal(
    library, biology, files, monkeypatch
):
    path = write_pdf(files / "Cells.pdf", ["Mitochondria make ATP"])

    def full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(tempfile, "mkdtemp", full)
    result = import_file(library, biology, path)

    assert isinstance(result, Refused)
    assert result.reason is RefusalReason.DISK_FULL


def test_text_that_cannot_be_stored_is_a_plain_refusal(library, biology, files, monkeypatch):
    import gct.library.importer as importer

    path = write_pdf(files / "Garbled.pdf", ["Mitochondria make ATP"])
    garbled = [parse.ParsedUnit(text="broken \ud800 font", file="Garbled.pdf", page_or_slide=1)]
    monkeypatch.setattr(importer, "parse_file", lambda _path: garbled)

    result = import_file(library, biology, path)

    assert result == Refused(
        RefusalReason.UNPARSEABLE,
        "Garbled.pdf has text the tutor cannot store, which usually means the file is damaged. "
        "Open it and save a fresh copy, then import that.",
    )
    assert library.list_documents(biology) == []


def test_another_disk_failure_during_the_copy_is_a_plain_refusal(
    library, biology, files, monkeypatch
):
    path = write_pptx(files / "Week 3.pptx", ["Osmosis"])

    def broken():
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(parse.tempfile, "TemporaryFile", broken)
    result = import_file(library, biology, path)

    assert result == Refused(
        RefusalReason.NO_WORKING_COPY,
        "The tutor could not make a working copy of Week 3.pptx on this Mac. "
        "Try again; if it keeps happening, restart the Mac.",
    )


def test_too_long_names_the_parser_limits_and_never_mentions_words(
    library, biology, files, monkeypatch
):
    assert parse._PDF_STREAM_MAX_BYTES == 16 * 1024 * 1024
    assert parse._PPTX_MAX_TOTAL_BYTES == 1024 * 1024 * 1024
    assert parse._PPTX_MAX_PART_BYTES == 512 * 1024 * 1024
    assert parse._PPTX_MAX_PARTS == 10_000
    pdf = write_pdf(files / "Map.pdf", ["A detailed map " * 40])
    deck = write_pptx(files / "Video.pptx", ["A lecture with a video " * 40])
    monkeypatch.setattr(parse, "_PDF_STREAM_MAX_BYTES", 64)
    monkeypatch.setattr(parse, "_PPTX_MAX_TOTAL_BYTES", 1024)

    from_pdf = import_file(library, biology, pdf)
    from_deck = import_file(library, biology, deck)

    assert from_pdf == Refused(
        RefusalReason.TOO_LONG,
        "Map.pdf has a page too detailed for the tutor to read: it unpacks to more than 16 MB. "
        "Make a fresh copy with File > Print > Save as PDF, or split off the pages you need, "
        "and import that.",
    )
    assert from_deck == Refused(
        RefusalReason.TOO_LONG,
        "Video.pptx is too big for the tutor once unpacked: the limits are 1 GB in all, "
        "512 MB for any one picture or video, and 10,000 slides, pictures and other pieces. "
        "Remove large videos or pictures, or split the deck, and import it again.",
    )
    assert "word" not in (from_pdf.message + from_deck.message).lower()
    assert library.list_documents(biology) == []


def test_every_parser_reason_has_a_refusal():
    assert {reason.value for reason in RefusalReason} >= set(parse.TERMINAL_REASONS)


def test_a_class_that_has_gone_is_a_plain_refusal(library, biology, files):
    path = write_pdf(files / "Cells.pdf", ["Mitochondria make ATP"])
    library.delete_class(biology)

    result = import_file(library, biology, path)

    assert result == Refused(
        RefusalReason.CLASS_MISSING, "That class is not in this library; refresh the list."
    )
    assert library.check() == []


def test_a_library_held_by_another_window_is_a_plain_refusal(
    library_path, library, biology, files, library_with_a_document
):
    path = write_pdf(files / "Cells.pdf", ["Mitochondria make ATP"])
    other_window = sqlite3.connect(library_path, isolation_level=None)
    other_window.execute("BEGIN IMMEDIATE")
    try:
        with Library(library_path, timeout=0.1) as impatient:
            result = import_file(impatient, biology, path)
    finally:
        other_window.execute("ROLLBACK")
        other_window.close()

    assert result == Refused(
        RefusalReason.NOT_SAVED,
        "Cells.pdf was not saved: another window is using the library; close it and try again.",
    )
    assert library.list_documents(biology) == library_with_a_document
    assert library.check() == []


def test_a_refusal_has_no_document_to_use_by_mistake(library, biology, files):
    result = import_file(library, biology, files / "Missing.pdf")

    assert isinstance(result, Refused)
    with pytest.raises(AttributeError):
        result.document  # noqa: B018
