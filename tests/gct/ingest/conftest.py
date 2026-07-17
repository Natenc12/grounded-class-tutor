"""Fixture builders for ingest tests.

Real files are generated on the fly (not committed as binary blobs) so `pypdf` /
`python-pptx` / `reportlab` stay the single source of truth for what a "real" PDF or
PPTX looks like.

Also home to two ingest-pipeline fixtures (issue #4):
  - `fake_embedder` — a deterministic `Embeddings`-shaped stub so `compose` can be tested
    with no network / no OpenAI (the same `client=`-injection discipline as the adapter).
  - `db` — a real local-Postgres connection with a seeded `classes` row, for the atomic
    index-transaction tests (ADR 0020). Skips when the DB is unreachable so non-DB envs
    don't hard-fail.
"""
from __future__ import annotations

import io
import uuid
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
    vector(1536)` column in the real-DB index tests. Each text maps to a distinct vector
    (first component = a per-run hash of the text — `hash()` is randomized per process, so it's
    stable within a test run, not across runs) so a misalignment between input order and stored
    vectors would show up.
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
            seed = float(hash(text) % 1000)
            vectors.append([seed] + [0.0] * (self._dim - 1))
        return vectors


@pytest.fixture
def fake_embedder() -> FakeEmbeddings:
    return FakeEmbeddings()


@pytest.fixture
def db():
    """Yield `(conn, owner_id, class_id)` on the real local Postgres with a seeded class row.

    `owner_id` is unique per test so teardown can delete exactly this test's rows
    (chunks → files → classes, respecting FKs). Skips the test if the DB is unreachable, so a
    machine without Postgres doesn't hard-fail the suite.
    """
    try:
        from gct.db import connect

        conn = connect()
    except Exception as exc:  # noqa: BLE001 — any connect failure means "no DB here", skip
        pytest.skip(f"local Postgres unavailable: {exc}")

    owner_id = f"test-owner-{uuid.uuid4()}"
    class_id = str(uuid.uuid4())
    try:
        conn.execute(
            "insert into classes (class_id, owner_id, name) values (%s::uuid, %s, %s)",
            (class_id, owner_id, "test class"),
        )
        conn.commit()
        yield conn, owner_id, class_id
    finally:
        conn.execute("delete from chunks where owner_id = %s", (owner_id,))
        conn.execute("delete from files where owner_id = %s", (owner_id,))
        conn.execute("delete from classes where owner_id = %s", (owner_id,))
        conn.commit()
        conn.close()
