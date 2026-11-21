"""Build the synthetic eval course and its questions from the fact table (issue #199).

Reads eval/synthetic/facts.json (hand-written) and writes:
  - eval/synthetic/course/<class>/<file>.json - one text file per course file, one string per page
  - eval/synthetic/questions.jsonl            - about 100 questions in the ADR 0021 format

The expected outcome and expected page of every question come from the table, never from reading
model output. Output is deterministic: running this twice gives identical files.

    uv run python scripts/make_synthetic_course.py           # write the files
    uv run python scripts/make_synthetic_course.py --check   # exit 1 if any file would change
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC = ROOT / "eval" / "synthetic"
FACTS = SYNTHETIC / "facts.json"
COURSE = SYNTHETIC / "course"
QUESTIONS = SYNTHETIC / "questions.jsonl"

# Question kind -> the `expectation` written to the question file.
EXPECTATION_BY_KIND = {
    "single": "answer",
    "multi": "answer",
    "injection": "answer",
    "partial": "partial",
    "refuse": "refuse",
}
SUITE = "synthetic"


class TableError(ValueError):
    """The fact table contradicts itself. The message says where."""


def file_slug(name: str) -> str:
    """'Lecture 03 Reservoirs.pdf' -> 'lecture-03-reservoirs'."""
    stem = name.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def scan_noise(text: str) -> str:
    """Make text look badly scanned, deterministically: m->rn, some l->1 and o->0, stray marks.

    Used on filler text only, so every fact statement stays exact and checkable.
    """
    out: list[str] = []
    for i, ch in enumerate(text):
        if ch == "m":
            out.append("rn")
        elif ch == "l" and i % 3 == 0:
            out.append("1")
        elif ch == "o" and i % 5 == 0:
            out.append("0")
        elif ch == " " and i % 11 == 0:
            out.append(" ~ ")
        else:
            out.append(ch)
    return "".join(out)


def build(table: dict[str, Any]) -> dict[Path, str]:
    """Every output file's path and exact contents. Raises `TableError` on an inconsistent table."""
    classes: dict[str, str] = table["classes"]
    facts = {fact["id"]: fact for fact in table["facts"]}
    if len(facts) != len(table["facts"]):
        raise TableError("duplicate fact id")
    used: dict[str, tuple[str, str, int]] = {}

    # --- pages -> course files -------------------------------------------------------------
    files: dict[tuple[str, str], dict[int, str]] = {}
    for page in table["pages"]:
        key = (page["class"], page["file"], page["page"])
        if page["class"] not in classes:
            raise TableError(f"page {key}: unknown class")
        paragraphs = [page["title"]]
        for entry in page["body"]:
            if entry.startswith("@"):
                fact_id = entry[1:]
                fact = facts.get(fact_id)
                if fact is None:
                    raise TableError(f"page {key}: unknown fact {fact_id}")
                if (fact["class"], fact["file"], fact["page"]) != key:
                    raise TableError(f"page {key}: fact {fact_id} says it lives elsewhere")
                if fact_id in used:
                    raise TableError(f"fact {fact_id} placed twice")
                used[fact_id] = key
                paragraphs.append(fact["statement"])
            else:
                paragraphs.append(scan_noise(entry) if page.get("noisy") else entry)
        pages = files.setdefault((page["class"], page["file"]), {})
        if page["page"] in pages:
            raise TableError(f"page {key} defined twice")
        pages[page["page"]] = "\n\n".join(paragraphs)
    unplaced = sorted(set(facts) - set(used))
    if unplaced:
        raise TableError(f"facts on no page: {', '.join(unplaced)}")

    out: dict[Path, str] = {}
    for (class_slug, name), pages in sorted(files.items()):
        if sorted(pages) != list(range(1, len(pages) + 1)):
            raise TableError(f"{name}: pages must be numbered 1..n, got {sorted(pages)}")
        body = {"class": class_slug, "file": name, "pages": [pages[n] for n in sorted(pages)]}
        path = COURSE / class_slug / f"{file_slug(name)}.json"
        out[path] = json.dumps(body, indent=2, ensure_ascii=False) + "\n"

    # --- questions ------------------------------------------------------------------------
    lines: list[str] = []
    seen: set[str] = set()
    for q in table["questions"]:
        qid, kind = q["id"], q["kind"]
        if qid in seen:
            raise TableError(f"duplicate question id {qid}")
        seen.add(qid)
        if kind not in EXPECTATION_BY_KIND:
            raise TableError(f"question {qid}: unknown kind {kind!r}")
        if q["class"] not in classes:
            raise TableError(f"question {qid}: unknown class {q['class']!r}")
        fact_ids: list[str] = q.get("facts", [])
        if (kind == "refuse") != (not fact_ids):
            raise TableError(f"question {qid}: refuse questions have no facts, others need one")
        if kind == "injection" and not q.get("forbidden"):
            raise TableError(f"question {qid}: an injection question needs a forbidden word")
        if kind == "partial" and not q.get("missing"):
            raise TableError(f"question {qid}: a partial question must say what is missing")

        sources: list[dict[str, Any]] = []
        statements: list[str] = []
        for fact_id in fact_ids:
            fact = facts.get(fact_id)
            if fact is None:
                raise TableError(f"question {qid}: unknown fact {fact_id}")
            if fact["class"] != q["class"]:
                raise TableError(f"question {qid}: fact {fact_id} is in another class")
            source = {"file": fact["file"], "page_or_slide": fact["page"]}
            if source not in sources:
                sources.append(source)
            statements.append(fact["statement"])

        if kind == "refuse":
            notes = f"Refuse: {q['why']}."
        elif kind == "partial":
            notes = f"{' '.join(statements)} Not covered: {q['missing']}."
        else:
            notes = " ".join(statements)

        row = {
            "id": qid,
            "question": q["question"],
            "class": q["class"],
            "expectation": EXPECTATION_BY_KIND[kind],
            "expected_sources": sources,
            "answer_notes": notes,
            "suites": [SUITE],
            "tags": [kind],
            "added": table["added"],
            "required": q.get("required", []),
            "forbidden": q.get("forbidden", []),
        }
        lines.append(json.dumps(row, ensure_ascii=False))
    out[QUESTIONS] = "\n".join(lines) + "\n"
    return out


def stale(expected: dict[Path, str]) -> list[str]:
    """Paths whose contents on disk differ from `expected`, plus course files nobody generates."""
    problems = [
        str(path.relative_to(ROOT))
        for path, text in expected.items()
        if not path.exists() or path.read_text(encoding="utf-8") != text
    ]
    extra = sorted(set(COURSE.rglob("*.json")) - set(expected)) if COURSE.exists() else []
    problems += [f"{p.relative_to(ROOT)} (not generated by this script)" for p in extra]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if any file would change")
    args = parser.parse_args(argv)

    expected = build(json.loads(FACTS.read_text(encoding="utf-8")))
    if args.check:
        problems = stale(expected)
        for problem in problems:
            print(f"out of date: {problem}", file=sys.stderr)
        if problems:
            print("run: uv run python scripts/make_synthetic_course.py", file=sys.stderr)
            return 1
        print(f"up to date: {len(expected)} files")
        return 0

    for path in sorted(set(COURSE.rglob("*.json")) - set(expected)) if COURSE.exists() else []:
        path.unlink()
    for path, text in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(f"wrote {len(expected)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
