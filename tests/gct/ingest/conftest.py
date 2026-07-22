"""Fixture builders for ingest tests.

Real files are generated on the fly (not committed as binary blobs) so `pypdf` /
`python-pptx` / `reportlab` stay the single source of truth for what a "real" PDF or
PPTX looks like.

Also home to `fake_embedder` (issue #4) — a deterministic `Embeddings`-shaped stub so `compose`
can be tested with no network / no OpenAI (the same `client=`-injection discipline as the adapter).

The `db` fixture moved to the root `tests/conftest.py` when the retriever suite (#5) needed it too;
it is still available here by normal conftest inheritance.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Sequence

import pytest
from pptx import Presentation
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from gct.config import EMBEDDING_DIM
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


def make_pptx(path: Path, slide_texts: list[str | None]) -> Path:
    """Write a PPTX at `path` with one slide per entry in `slide_texts`.

    A `None` entry produces a slide with no text-bearing shape at all.
    """
    deck = Presentation()
    blank_layout = deck.slide_layouts[6]  # "Blank" in the default template
    for text in slide_texts:
        slide = deck.slides.add_slide(blank_layout)
        if text is not None:
            box = slide.shapes.add_textbox(0, 0, deck.slide_width, deck.slide_height)
            box.text_frame.text = text
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
    def _make(name: str, slide_texts: list[str | None]) -> Path:
        return make_pptx(tmp_path / name, slide_texts)

    return _make


@pytest.fixture
def ole_stub_factory(tmp_path):
    def _make(name: str) -> Path:
        return make_ole_stub(tmp_path / name)

    return _make


# --- Ingest-pipeline fixtures (issue #4) ------------------------------------------------------


class FakeEmbeddings:
    """Deterministic `Embeddings`-shaped stub — no network, no OpenAI.

    Satisfies the `providers.base.Embeddings` protocol (`model_id`, `dim`, `embed`). `dim`
    defaults to `EMBEDDING_DIM` (1536) so vectors it returns satisfy the `chunks.embedding
    vector(1536)` column in the real-DB index tests. Each text maps to a distinct vector (first
    component = a sha256 digest of the text, taken as a 48-bit big-endian int) so a misalignment
    between input order and stored vectors would show up. The digest is stable across processes
    and across time — no `hash()`, no modulo — so the same text always maps to the same vector,
    everywhere, forever (issue #23).

    NOT usable for retrieval ranking: every vector it returns is a positive scalar multiple of
    the same direction, and cosine ignores magnitude — so the cosine distance between any two of
    them is 0 and rank order would be arbitrary. Fine here (ingest never ranks); the retriever
    suite has its own ranking-capable stub in `tests/gct/retriever/conftest.py` (issue #5).
    """

    def __init__(self, model_id: str = "fake-embed-3", dim: int = EMBEDDING_DIM) -> None:
        self._model_id = model_id
        self._dim = dim

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            h = hashlib.sha256(text.encode()).digest()
            seed = float(int.from_bytes(h[:6], "big"))  # 48 bits, fits float64's mantissa; no modulo
            vectors.append([seed] + [0.0] * (self._dim - 1))
        return vectors


@pytest.fixture
def fake_embedder() -> FakeEmbeddings:
    return FakeEmbeddings()
