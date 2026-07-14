"""Parse — turn a staged file into text + structural units, birthing the source
metadata that the citation spine trusts from here on: citation-spine honor-point ①
(design/decisions/0019-chunking-contract-never-span.md, F2).

Pure function: no DB, no job/status/lease state (the PM-4 seam — Slice 2 wraps this
in worker/queue machinery without rewriting it). Terminal failures (unparseable /
password-protected / zero-text) are signalled by raising `ParseError` with a `reason`
drawn from the same terminal taxonomy as `files.failed_reason`
(design/decisions/0020-ingestion-failure-idempotency-model.md); the caller decides
retry policy, this module never does.

Tooling (pypdf / python-pptx) is a spike choice (design/components/ingestion-worker.md
§"spec/spike line") — swappable without changing the `ParsedUnit` contract.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

import pypdf.errors
import pptx.exc
from pptx import Presentation
from pypdf import PdfReader

TERMINAL_REASONS = ("unparseable", "protected", "unsupported", "empty")

# MS-CFB (OLE2) container signature. Password-protected OOXML files (.pptx/.docx/.xlsx)
# are wrapped in this container instead of being a plain zip, so python-pptx's zip-based
# reader never gets far enough to raise anything more specific — check for it up front.
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class ParseError(Exception):
    """A terminal (no-retry) parse failure.

    `reason` matches `files.failed_reason`'s terminal taxonomy (ADR 0020) so a future
    caller can pass it straight through with no translation.
    """

    def __init__(self, reason: str, message: str) -> None:
        assert reason in TERMINAL_REASONS, f"unknown terminal reason: {reason!r}"
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ParsedUnit:
    """One page (PDF) or slide (PPTX) of extracted text.

    Carries the `(file, page_or_slide)` provenance the whole citation spine is born
    from — honor-point ①. `page_or_slide` is 1-indexed to match how a human would cite
    the source ("slide 3"), and is never a range (ADR 0019 never-span).
    """

    text: str
    file: str
    page_or_slide: int


def parse_file(path: str | Path) -> list[ParsedUnit]:
    """Parse a staged PDF or PPTX file into provenance-stamped text units.

    Raises `ParseError` (terminal, no retry) for an unsupported extension, an
    unparseable/corrupt file, a password-protected file, or a file with zero
    extractable text across every page/slide.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        units = _parse_pdf(path)
    elif suffix == ".pptx":
        units = _parse_pptx(path)
    else:
        raise ParseError("unsupported", f"unsupported file type: {suffix or '(none)'}")

    if not units:
        raise ParseError("empty", f"no extractable text in {path.name}")

    return units


def _parse_pdf(path: Path) -> list[ParsedUnit]:
    try:
        reader = PdfReader(path)
    except pypdf.errors.PyPdfError as exc:
        raise ParseError("unparseable", f"could not read PDF {path.name}: {exc}") from exc

    if reader.is_encrypted:
        raise ParseError("protected", f"{path.name} is password-protected")

    units: list[ParsedUnit] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise ParseError(
                "unparseable", f"could not extract text from {path.name} page {i}: {exc}"
            ) from exc
        if text.strip():
            units.append(ParsedUnit(text=text, file=path.name, page_or_slide=i))
    return units


def _parse_pptx(path: Path) -> list[ParsedUnit]:
    with open(path, "rb") as f:
        header = f.read(len(_OLE_SIGNATURE))
    if header == _OLE_SIGNATURE:
        raise ParseError("protected", f"{path.name} is password-protected")

    try:
        deck = Presentation(str(path))
    except (pptx.exc.PackageNotFoundError, zipfile.BadZipFile, KeyError) as exc:
        raise ParseError("unparseable", f"could not read PPTX {path.name}: {exc}") from exc

    units: list[ParsedUnit] = []
    for i, slide in enumerate(deck.slides, start=1):
        lines = []
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for paragraph in shape.text_frame.paragraphs:
                line = "".join(run.text for run in paragraph.runs)
                if line:
                    lines.append(line)
        text = "\n".join(lines)
        if text.strip():
            units.append(ParsedUnit(text=text, file=path.name, page_or_slide=i))
    return units
