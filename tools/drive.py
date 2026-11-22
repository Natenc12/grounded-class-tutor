"""Drive the whole tutor once, for free: `uv run python tools/drive.py`.

It always uses the stand-in model and a throwaway library, whatever the environment says, and ends
with DRIVE PASSED or DRIVE FAILED at: <step>.
"""

from __future__ import annotations

import io
import re
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from gct.library.cli import main as tutor
from gct.providers.standin import StandInGeneration

PDF = "hydrology-notes.pdf"
PPTX = "history-slides.pptx"
PDF_PAGES = [
    "Hydrology studies how water moves over and under the land.",
    "The base flow of a river is the part of its flow fed by groundwater.",
    "Clay above an aquifer slows the recharge of the water table.",
]
PPTX_SLIDES = [
    "The Ostren League was founded by four harbour towns in 1204.",
    "Its fleet guarded the amberwool trade on the northern coast.",
]
HYDROLOGY = "Hydrology"
HISTORY = "History"
COVERED = "What feeds the base flow of a river?"
LEAGUE = "Who founded the Ostren League?"

_SOURCE = re.compile(r"^  \[S\d+\] (?P<where>.+)$", re.M)


class StepFailed(Exception):
    pass


class Drive:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.library = folder / "drive.sqlite3"

    def run(self, *argv: str) -> str:
        output = io.StringIO()
        code = tutor(
            [argv[0], "--library", str(self.library), *argv[1:]],
            generator=StandInGeneration,
            out=output,
            err=output,
        )
        if code != 0:
            raise StepFailed(f"`{' '.join(argv)}` stopped with code {code}:\n{output.getvalue()}")
        return output.getvalue()

    def build_files(self) -> None:
        from pptx import Presentation
        from pptx.util import Inches
        from reportlab.pdfgen import canvas

        pdf = canvas.Canvas(str(self.folder / PDF), pagesize=(612, 792))
        for text in PDF_PAGES:
            pdf.drawString(72, 700, text)
            pdf.showPage()
        pdf.save()

        deck = Presentation()
        for text in PPTX_SLIDES:
            slide = deck.slides.add_slide(deck.slide_layouts[6])
            box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(1))
            box.text_frame.text = text
        deck.save(str(self.folder / PPTX))

    def make_classes(self) -> None:
        self.run("add-class", HYDROLOGY)
        self.run("add-class", HISTORY)

    def import_pdf(self) -> None:
        _expect(self.run("import", HYDROLOGY, str(self.folder / PDF)), f"Imported {PDF} (3 pages).")

    def import_pptx(self) -> None:
        printed = self.run("import", HISTORY, str(self.folder / PPTX))
        _expect(printed, f"Imported {PPTX} (2 slides).")

    def list_library(self) -> None:
        printed = self.run("list")
        _expect(printed, f"{HISTORY}\n  {PPTX} (2 slides)\n{HYDROLOGY}\n  {PDF} (3 pages)\n")

    def ask_covered(self) -> None:
        _grounded(self.run("ask", HYDROLOGY, COVERED), f"{PDF}, page 2")

    def ask_uncovered(self) -> None:
        printed = self.run("ask", HYDROLOGY, LEAGUE)
        if not printed.startswith("REFUSAL:") or _SOURCE.search(printed):
            raise StepFailed(f"Expected a refusal with no sources, got:\n{printed}")

    def ask_slides(self) -> None:
        _grounded(self.run("ask", HISTORY, LEAGUE), f"{PPTX}, slide 1")

    def check_library(self) -> None:
        _expect(self.run("check"), "The library is in good order.")


def _expect(printed: str, wanted: str) -> None:
    if wanted not in printed:
        raise StepFailed(f"Expected {wanted!r}, got:\n{printed}")


def _grounded(printed: str, source: str) -> None:
    cited = [found.group("where") for found in _SOURCE.finditer(printed)]
    if not printed.startswith("GROUNDED:") or cited != [source]:
        raise StepFailed(f"Expected a GROUNDED answer citing only {source}, got:\n{printed}")


def drive(out: TextIO | None = None) -> int:
    out = out or sys.stdout
    step = "make a throwaway library"
    try:
        with tempfile.TemporaryDirectory(prefix="gct-drive-") as folder:
            print(f"ok    {step}", file=out)
            car = Drive(Path(folder))
            steps: list[tuple[str, Callable[[], None]]] = [
                ("build a synthetic PDF and PowerPoint", car.build_files),
                ("make two classes", car.make_classes),
                ("import the PDF", car.import_pdf),
                ("import the PowerPoint", car.import_pptx),
                ("list the library", car.list_library),
                ("ask a covered question", car.ask_covered),
                ("ask a question this class does not cover", car.ask_uncovered),
                ("ask a question the slides cover", car.ask_slides),
                ("check the library", car.check_library),
            ]
            for step, run in steps:
                run()
                print(f"ok    {step}", file=out)
            step = "delete the throwaway library"
        if Path(folder).exists():
            raise StepFailed(f"{folder} is still there.")
        print(f"ok    {step}", file=out)
    except StepFailed as failure:
        print(failure, file=out)
        print(f"DRIVE FAILED at: {step}", file=out)
        return 1
    except Exception:
        traceback.print_exc(file=out)
        print(f"DRIVE FAILED at: {step}", file=out)
        return 1
    print("DRIVE PASSED", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(drive())
