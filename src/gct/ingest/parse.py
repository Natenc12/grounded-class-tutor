"""Parse - turn a staged file into text + structural units, birthing the source
metadata that the citation spine trusts from here on: citation-spine honor-point ①
(design/decisions/0019-chunking-contract-never-span.md, F2).

Pure function: no database, model connection or job state. Terminal failures raise
`ParseError` with a closed reason; the local service owns user-facing messages.
PDF/PowerPoint tooling remains replaceable without changing `ParsedUnit` provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pypdf.errors
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pypdf import PdfReader

# Closed terminal reasons retained from ADR 0020/0029. The caller may also raise
# `too_long` when enforcing a bound after parsing.
TERMINAL_REASONS = ("unparseable", "protected", "unsupported", "empty", "too_long")

# MS-CFB (OLE2) container signature. Password-protected OOXML files (.pptx/.docx/.xlsx)
# are wrapped in this container instead of being a plain zip, so python-pptx's zip-based
# reader never gets far enough to raise anything more specific - check for it up front.
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class ParseError(Exception):
    """A terminal (no-retry) parse failure.

    `reason` belongs to the closed terminal taxonomy (ADR 0020/0029). Consumers
    translate it to a safe public message without exposing parser diagnostics.
    """

    def __init__(self, reason: str, message: str) -> None:
        assert reason in TERMINAL_REASONS, f"unknown terminal reason: {reason!r}"
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ParsedUnit:
    """One page (PDF) or slide (PPTX) of extracted text.

    Carries the `(file, page_or_slide)` provenance the whole citation spine is born
    from - honor-point ①. `page_or_slide` is 1-indexed to match how a human would cite
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

    units = _strip_nul(units)

    if not units:
        raise ParseError("empty", f"no extractable text in {path.name}")

    return units


def _strip_nul(units: list[ParsedUnit]) -> list[ParsedUnit]:
    """Drop NUL (0x00) bytes from every unit's text, and drop units with nothing else left.

    NUL is extraction debris, observed in the retained dogfood corpus. Normalize it
    before chunking so stored text, retrieved evidence and model context agree.

    Applied in `parse_file` AFTER format dispatch - one rule for both formats - though only PDFs
    can actually carry it: PPTX bodies are XML 1.0, which cannot represent NUL at all. A unit that
    is nothing but NULs (with whitespace) is dropped exactly like an empty page, and a whole file
    of them lands on the existing `empty` terminal (ADR 0020's taxonomy, no new failure kind).
    """
    cleaned: list[ParsedUnit] = []
    for unit in units:
        text = unit.text.replace("\x00", "")
        if text.strip():
            cleaned.append(
                ParsedUnit(text=text, file=unit.file, page_or_slide=unit.page_or_slide)
                if text != unit.text
                else unit
            )
    return cleaned


def _parse_pdf(path: Path) -> list[ParsedUnit]:
    try:
        reader = PdfReader(path)
    except pypdf.errors.LimitReachedError as exc:
        raise ParseError("too_long", "PDF parser expansion limit exceeded") from exc
    except pypdf.errors.PyPdfError as exc:
        raise ParseError("unparseable", f"could not read PDF {path.name}: {exc}") from exc

    if reader.is_encrypted:
        raise ParseError("protected", f"{path.name} is password-protected")

    units: list[ParsedUnit] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except pypdf.errors.LimitReachedError as exc:
            raise ParseError("too_long", "PDF parser expansion limit exceeded") from exc
        except Exception as exc:
            raise ParseError(
                "unparseable", f"could not extract text from {path.name} page {i}: {exc}"
            ) from exc
        if text.strip():
            units.append(ParsedUnit(text=text, file=path.name, page_or_slide=i))
    return units


# Separator between a slide's body text and its speaker notes within the same
# ParsedUnit. Deliberately NOT bracketed. The reason is a nudge, not a collision: the
# Grounder's validator only parses [S#] tokens out of the MODEL'S ANSWER and range-checks
# them (design/decisions/0015-grounder-citation-contract-validation.md §②/§③), so a
# "[Speaker notes]" token in context text would never enter that ladder at all. What it
# WOULD do is put bracketed tokens in front of the model in the very context where [S#] is
# the citation vocabulary — and bracket-shaped context invites bracket-shaped output. Cheap
# to avoid, so avoid it.
_NOTES_MARKER = "Speaker notes:"


def _iter_shapes(shapes):
    """Flatten grouped shapes so text nested inside a PowerPoint group (a common
    authoring pattern) isn't invisible to a top-level `shape.has_text_frame` scan."""
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_shapes(shape.shapes)
        else:
            yield shape


def _slide_notes(slide) -> str:
    """Return a slide's speaker-notes text, or "" if it has none.

    Guards on `has_notes_slide` FIRST: reading `slide.notes_slide` *creates* the notes
    part as a side effect (verified on python-pptx 1.0.2), so touching it before the
    check both mutates the in-memory deck and makes a "slide has no notes" test pass for
    the wrong reason. `notes_text_frame` can also be None on decks from other authoring
    tools, so it is guarded separately rather than assumed present.
    """
    if not slide.has_notes_slide:
        return ""
    notes_text_frame = slide.notes_slide.notes_text_frame
    if notes_text_frame is None:
        return ""
    return notes_text_frame.text


def _parse_pptx(path: Path) -> list[ParsedUnit]:
    with open(path, "rb") as f:
        header = f.read(len(_OLE_SIGNATURE))
    if header == _OLE_SIGNATURE:
        raise ParseError("protected", f"{path.name} is password-protected")

    try:
        deck = Presentation(str(path))
    except Exception as exc:
        raise ParseError("unparseable", f"could not read PPTX {path.name}: {exc}") from exc

    units: list[ParsedUnit] = []
    for i, slide in enumerate(deck.slides, start=1):
        try:
            lines = []
            for shape in _iter_shapes(slide.shapes):
                if shape.has_table:
                    # Preserve grid positions. Carry a vertical merge's label
                    # into each covered row; horizontal continuations stay empty.
                    table = shape.table
                    row_count, column_count = len(table.rows), len(table.columns)
                    carried: dict[tuple[int, int], str] = {}
                    for row_index, row in enumerate(table.rows):
                        cells = []
                        for column, cell in enumerate(row.cells):
                            if cell.is_merge_origin:
                                row_span, column_span = cell.span_height, cell.span_width
                                if not (
                                    1 <= row_span <= row_count - row_index
                                    and 1 <= column_span <= column_count - column
                                ):
                                    raise ValueError("table merge extends beyond its grid")
                                origin_text = cell.text
                                # Repeat small category labels for readability. A
                                # large merged value is emitted in full once; later
                                # rows reference that cell instead of multiplying
                                # arbitrary source text by the row count.
                                continuation = (
                                    origin_text
                                    if len(origin_text) <= 160
                                    else (
                                        f"(same merged cell as row {row_index + 1}, "
                                        f"column {column + 1})"
                                    )
                                )
                                for offset in range(1, row_span):
                                    carried[row_index + offset, column] = continuation
                            cells.append(
                                carried.get((row_index, column), "")
                                if cell.is_spanned
                                else cell.text
                            )
                        if any(cell.strip() for cell in cells):
                            lines.append(" | ".join(cells))
                    continue
                if not shape.has_text_frame:
                    continue
                for paragraph in shape.text_frame.paragraphs:
                    line = "".join(run.text for run in paragraph.runs)
                    if line:
                        lines.append(line)
            text = "\n".join(lines)
        except Exception as exc:
            # Body-text failure stays TERMINAL - unchanged by #12.
            raise ParseError(
                "unparseable", f"could not extract text from {path.name} slide {i}: {exc}"
            ) from exc

        try:
            notes = _slide_notes(slide)
        except Exception:
            # DEGRADE, don't fail (D2): a corrupt notes part on an otherwise readable
            # deck must not turn into a terminal `unparseable` the student sees as a
            # refusal. Swallowed deliberately and silently - no logging in V1; Slice 2
            # owns observability (D3).
            notes = ""

        if notes.strip():
            # Body, blank line, marker, notes (D1). Join only the non-empty parts so a
            # notes-only slide doesn't carry a pointless leading blank block.
            parts = [p for p in (text, f"{_NOTES_MARKER}\n{notes}") if p.strip()]
            text = "\n\n".join(parts)

        # Notes ride inside this slide's own unit - one unit per slide, so never-span
        # (ADR 0019) holds by construction. The emptiness check tests the COMBINED text,
        # so a notes-only slide still emits a unit (`empty` is a WHOLE-FILE terminal,
        # ADR 0020).
        if text.strip():
            units.append(ParsedUnit(text=text, file=path.name, page_or_slide=i))
    return units
