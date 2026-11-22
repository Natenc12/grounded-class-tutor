"""Temporary probe for #198: do the messages sent to the model match across machines?"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from gct.eval.questions import load_questions
from gct.eval.runner import load_course
from gct.library import Library
from gct.library.ask import ask

SYNTHETIC = Path(__file__).resolve().parents[3] / "eval" / "synthetic"
PROBE = SYNTHETIC / "sent-probe.json"


class Capture:
    model_id = "probe"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def generate(self, messages):
        canonical = json.dumps(list(messages), ensure_ascii=False, sort_keys=True)
        self.sent.append(hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        return "COVERAGE: complete"


def fingerprints() -> dict[str, list[str]]:
    course = load_course(SYNTHETIC / "course")
    questions = load_questions(SYNTHETIC / "questions.jsonl")
    out: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory() as folder, Library(Path(folder) / "p.db") as library:
        ids = {s: library.create_class(s).id for s in sorted({f.class_slug for f in course})}
        for f in course:
            library.save_document(ids[f.class_slug], f.file, f.original(), len(f.pages), f.units())
        for q in questions:
            capture = Capture()
            ask(library, ids[q.class_slug], q.question, generator=capture)
            out[q.id] = capture.sent
    return dict(sorted(out.items()))


def test_messages_sent_match_the_fingerprints_made_on_the_mac():
    now = fingerprints()
    if os.environ.get("GCT_WRITE_PROBE"):
        PROBE.write_text(json.dumps(now, indent=2) + "\n", encoding="utf-8")
    recorded = json.loads(PROBE.read_text(encoding="utf-8"))
    differ = sorted(q for q in recorded if recorded[q] != now.get(q))
    assert not differ, f"{len(differ)} of {len(recorded)} questions differ: {differ}"
    assert len(recorded) == 100
