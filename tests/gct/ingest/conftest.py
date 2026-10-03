"""Fixture builders for ingest tests.

Real files are generated on the fly (not committed as binary blobs) so `pypdf` /
`python-pptx` / `reportlab` stay the single source of truth for what a "real" PDF or
PPTX looks like.

"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from pptx import Presentation
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from gct.ingest.parse import _OLE_SIGNATURE


def make_pdf(path: Path, page_texts: list[str | None], encrypt_password: str | None = None) -> Path:
    """Write a PDF at `path` with one page per entry in `page_texts`.

    A `None` entry produces a genuinely blank page (no text drawn at all), so the
    zero-text test case is a real absence of content, not just whitespace.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    for text in page_texts:
        if text is not None:
            c.drawString(72, 700, text)
        c.showPage()
    c.save()
    buf.seek(0)

    if encrypt_password is None:
        path.write_bytes(buf.getvalue())
        return path

    reader = PdfReader(buf)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=encrypt_password)
    with open(path, "wb") as f:
        writer.write(f)
    return path


def make_pptx(
    path: Path,
    slide_texts: list[str | None],
    slide_notes: list[str | None] | None = None,
) -> Path:
    """Write a PPTX at `path` with one slide per entry in `slide_texts`.

    A `None` entry produces a slide with no text-bearing shape at all.

    `slide_notes` (issue #12) optionally attaches speaker notes, positionally matched to
    `slide_texts`; a `None` entry leaves that slide with **no notes part at all** - the
    distinction matters, because `slide.notes_slide` creates the part on access, so a slide
    that was merely given empty notes is not the same fixture as one that never had them.
    Defaults to `None` (no slide gets notes), keeping every pre-#12 caller unchanged.
    """
    if slide_notes is not None and len(slide_notes) != len(slide_texts):
        raise ValueError(
            f"slide_notes has {len(slide_notes)} entries but slide_texts has {len(slide_texts)}"
        )

    deck = Presentation()
    blank_layout = deck.slide_layouts[6]  # "Blank" in the default template
    for i, text in enumerate(slide_texts):
        slide = deck.slides.add_slide(blank_layout)
        if text is not None:
            box = slide.shapes.add_textbox(0, 0, deck.slide_width, deck.slide_height)
            box.text_frame.text = text
        if slide_notes is not None and slide_notes[i] is not None:
            slide.notes_slide.notes_text_frame.text = slide_notes[i]
    deck.save(str(path))
    return path


def make_ole_stub(path: Path) -> Path:
    """A file stamped with the MS-CFB signature real password-protected Office
    files use, standing in for one without needing a real encryption library."""
    path.write_bytes(_OLE_SIGNATURE + b"\x00" * 32)
    return path


@pytest.fixture
def pdf_factory(tmp_path):
    def _make(name: str, page_texts: list[str | None], encrypt_password: str | None = None) -> Path:
        return make_pdf(tmp_path / name, page_texts, encrypt_password)

    return _make


@pytest.fixture
def pptx_factory(tmp_path):
    def _make(
        name: str,
        slide_texts: list[str | None],
        slide_notes: list[str | None] | None = None,
    ) -> Path:
        return make_pptx(tmp_path / name, slide_texts, slide_notes)

    return _make


@pytest.fixture
def ole_stub_factory(tmp_path):
    def _make(name: str) -> Path:
        return make_ole_stub(tmp_path / name)

    return _make
