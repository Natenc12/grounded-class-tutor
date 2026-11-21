"""PROTOTYPE (spike #210, never merged): one cited answer through the Claude Agent SDK.

Builds a tiny sample class (the CI corpus generator's water-cycle pages), ingests it with the
free stand-in embeddings, then asks through `gct.ask.ask()` - main's unchanged retrieval and
answer logic - with answers written by Claude on the Claude login already on this Mac.

    uv run python scripts/claude_demo.py                      # one covered question
    uv run python scripts/claude_demo.py --also-uncovered     # plus one the pages do not cover
    uv run python scripts/claude_demo.py "your own question"

Each question is one real call on your Claude plan (two if the answer fails the format check
and the Grounder retries). GCT_PROVIDERS=standin runs the same thing on the free stand-ins.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from gct.ask import ask
from gct.db import connect
from gct.ingest.pipeline import ingest_file
from gct.providers import active
from gct.providers.claude_sdk import CREDENTIAL_ENV_VARS

COVERED = "What is an aquifer, and how fast does groundwater move?"
UNCOVERED = "Who painted the ceiling of the Sistine Chapel?"
OWNER = "t210-claude-demo"


class Recording:
    """Wraps the generator to keep the raw text Claude returned, for the record."""

    def __init__(self, inner):
        self.inner = inner
        self.raw: list[str] = []

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    def generate(self, messages):
        text = self.inner.generate(messages)
        self.raw.append(text)
        return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?", default=COVERED)
    ap.add_argument("--also-uncovered", action="store_true")
    args = ap.parse_args()
    os.environ.setdefault(active.PROVIDERS_ENV, active.CLAUDE)

    generator = active.build_generator()
    embedder = active.build_embedder()
    print(f"providers: {active.selected()}  answers: {generator.model_id}  "
          f"search: {embedder.model_id}")
    print("credential variables removed before calling Claude:",
          getattr(generator, "scrubbed", []) or "none were set")
    still_set = [n for n in CREDENTIAL_ENV_VARS if n in os.environ]
    print("credential variables set now:", still_set or "none")

    with tempfile.TemporaryDirectory() as corpus:
        subprocess.run(
            [sys.executable, "scripts/ci_corpus.py", corpus, "--pdfs", "1", "--pptx", "1",
             "--pages", "4"],
            check=True, capture_output=True,
        )
        conn = connect()
        conn.autocommit = True
        class_id = str(uuid4())
        conn.execute(
            "insert into classes (class_id, owner_id, name) values (%s::uuid, %s, %s)",
            (class_id, OWNER, f"water-cycle-{class_id[:8]}"),
        )
        files = sorted(Path(corpus).iterdir())
        for path in files:
            ingest_file(path, OWNER, class_id, embedder=embedder, conn=conn)
        print(f"sample class: {len(files)} files ingested ({', '.join(p.name for p in files)})")

    questions = [args.question] + ([UNCOVERED] if args.also_uncovered else [])
    for question in questions:
        recorder = Recording(generator)
        start = time.monotonic()
        out = ask(conn, question, OWNER, class_id, embedder=embedder, generator=recorder)
        seconds = time.monotonic() - start
        r = out.result
        print("\n" + "=" * 72)
        print(f"QUESTION: {question}")
        print(f"STATE:    {r.state.value}   ({seconds:.1f}s, {len(recorder.raw)} model call(s))")
        print(f"ANSWER:   {r.answer_prose}")
        for c in r.citations:
            print(f"  [{c.label}] {c.file}, page {c.page_or_slide}")
        print(f"COVERAGE: complete={r.coverage.complete} gaps={r.coverage.gaps}")
        print(f"INTEGRITY ok={r.integrity.ok} reasons={r.integrity.reasons}")
        if r.error:
            print(f"ERROR: {r.error.kind}: {r.error.message}")
        for i, raw in enumerate(recorder.raw, 1):
            print(f"--- raw model text, call {i} ---\n{raw}")
        if getattr(generator, "last_call", None):
            print(f"--- SDK call details ---\n{generator.last_call}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
