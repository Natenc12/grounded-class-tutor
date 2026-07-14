"""Unit tests for `gct.ingest.parse` — pure, no DB / API key needed.

Covers the honor-point ① contract: PDF/PPTX happy paths, per-page/slide unit
boundaries (never-span, ADR 0019), provenance stamping, and each terminal-failure
case (ADR 0020).
"""
from __future__ import annotations

import pytest

from gct.ingest.parse import ParsedUnit, ParseError, parse_file


class TestPdfHappyPath:
    def test_one_unit_per_page_with_text(self, pdf_factory):
        path = pdf_factory("lecture.pdf", ["Page one content", "Page two content"])

        units = parse_file(path)

        assert [u.page_or_slide for u in units] == [1, 2]
        assert "Page one content" in units[0].text
        assert "Page two content" in units[1].text

    def test_units_are_frozen_parsed_units(self, pdf_factory):
        path = pdf_factory("lecture.pdf", ["Some text"])

        [unit] = parse_file(path)

        assert isinstance(unit, ParsedUnit)
        with pytest.raises(AttributeError):
            unit.text = "mutated"  # frozen dataclass — provenance can't drift post-parse

    def test_provenance_stamped_on_every_unit(self, pdf_factory):
        path = pdf_factory("lecture-3.pdf", ["Alpha", "Beta"])

        units = parse_file(path)

        assert all(u.file == "lecture-3.pdf" for u in units)

    def test_blank_page_skipped_without_renumbering_survivors(self, pdf_factory):
        path = pdf_factory("deck.pdf", ["First", None, "Third"])

        units = parse_file(path)

        assert [u.page_or_slide for u in units] == [1, 3]

    def test_unit_never_spans_pages(self, pdf_factory):
        path = pdf_factory("deck.pdf", ["Alpha content", "Beta content"])

        units = parse_file(path)

        assert "Beta content" not in units[0].text
        assert "Alpha content" not in units[1].text


class TestPptxHappyPath:
    def test_one_unit_per_slide_with_text(self, pptx_factory):
        path = pptx_factory("lecture.pptx", ["Slide one content", "Slide two content"])

        units = parse_file(path)

        assert [u.page_or_slide for u in units] == [1, 2]
        assert "Slide one content" in units[0].text
        assert "Slide two content" in units[1].text

    def test_provenance_stamped_on_every_unit(self, pptx_factory):
        path = pptx_factory("lecture-3.pptx", ["Alpha", "Beta"])

        units = parse_file(path)

        assert all(u.file == "lecture-3.pptx" for u in units)

    def test_blank_slide_skipped_without_renumbering_survivors(self, pptx_factory):
        path = pptx_factory("deck.pptx", ["First", None, "Third"])

        units = parse_file(path)

        assert [u.page_or_slide for u in units] == [1, 3]

    def test_unit_never_spans_slides(self, pptx_factory):
        path = pptx_factory("deck.pptx", ["Alpha content", "Beta content"])

        units = parse_file(path)

        assert "Beta content" not in units[0].text
        assert "Alpha content" not in units[1].text


class TestTerminalFailures:
    def test_unsupported_extension(self, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_text("plain text file")

        with pytest.raises(ParseError) as exc_info:
            parse_file(path)

        assert exc_info.value.reason == "unsupported"

    def test_unparseable_pdf(self, tmp_path):
        path = tmp_path / "corrupt.pdf"
        path.write_bytes(b"this is not a real pdf file")

        with pytest.raises(ParseError) as exc_info:
            parse_file(path)

        assert exc_info.value.reason == "unparseable"

    def test_unparseable_pptx(self, tmp_path):
        path = tmp_path / "corrupt.pptx"
        path.write_bytes(b"this is not a real pptx file")

        with pytest.raises(ParseError) as exc_info:
            parse_file(path)

        assert exc_info.value.reason == "unparseable"

    def test_password_protected_pdf(self, pdf_factory):
        path = pdf_factory("secret.pdf", ["Confidential content"], encrypt_password="hunter2")

        with pytest.raises(ParseError) as exc_info:
            parse_file(path)

        assert exc_info.value.reason == "protected"

    def test_password_protected_pptx(self, ole_stub_factory):
        path = ole_stub_factory("secret.pptx")

        with pytest.raises(ParseError) as exc_info:
            parse_file(path)

        assert exc_info.value.reason == "protected"

    def test_zero_text_pdf(self, pdf_factory):
        path = pdf_factory("blank.pdf", [None, None])

        with pytest.raises(ParseError) as exc_info:
            parse_file(path)

        assert exc_info.value.reason == "empty"

    def test_zero_text_pptx(self, pptx_factory):
        path = pptx_factory("blank.pptx", [None, None])

        with pytest.raises(ParseError) as exc_info:
            parse_file(path)

        assert exc_info.value.reason == "empty"
