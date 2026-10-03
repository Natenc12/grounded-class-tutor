"""Post-selection measurement adapter; frozen retrieval, real SQLite lexical query.

Measures a warm in-memory vector cache plus local SQLite (including loading the
scoped parent text/identity), query encoding, both ranks, RRF, and context packing.
Excludes app IPC, database startup validation, initial vector loading and generation.
"""

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from runner import ROOT, fusion, lexical_candidates, private_json, seal_network, select_context

sys.path.insert(0, str(ROOT / "src"))
seal_network()
import numpy as np  # noqa: E402 -- seal network before optional encoder imports
from encoder import Encoder  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    encoder = Encoder(args.spec)
    reports = []
    for source in args.inputs:
        data = json.loads(Path(source).read_text())
        chunks = data["chunks"]
        vectors = encoder.encode([c["text"] for c in chunks])
        with tempfile.TemporaryDirectory(prefix="gct-search-time-") as tmp:
            with sqlite3.connect(Path(tmp) / "library.sqlite3") as connection:
                connection.row_factory = sqlite3.Row
                connection.executescript("""
                    CREATE TABLE documents (
                        id TEXT PRIMARY KEY,class_id TEXT,filename TEXT,sha256 TEXT);
                    CREATE TABLE chunks (rowid INTEGER PRIMARY KEY,id TEXT UNIQUE,
                        document_id TEXT,text TEXT,page_or_slide INTEGER,ordinal INTEGER);
                    CREATE VIRTUAL TABLE chunks_fts USING fts5(
                        text,filename,tokenize='porter unicode61');
                """)
                for index, c in enumerate(chunks):
                    connection.execute(
                        "INSERT OR IGNORE INTO documents VALUES(?,?,?,?)",
                        (c["document_id"], c["class_id"], c["file"], c["sha256"]),
                    )
                    connection.execute(
                        "INSERT INTO chunks VALUES(?,?,?,?,?,?)",
                        (index + 1, c["id"], c["document_id"], c["text"], c["page"], c["ordinal"]),
                    )
                    connection.execute(
                        "INSERT INTO chunks_fts(rowid,text,filename) VALUES(?,?,?)",
                        (index + 1, c["text"], c["file"]),
                    )
                connection.commit()
                by_id = {c["id"]: i for i, c in enumerate(chunks)}
                class_id = chunks[0]["class_id"]
                timings = []
                for _ in range(3):
                    for case in data["cases"]:
                        start = time.perf_counter()
                        # Includes scope+text fetches in the measured boundary.
                        scoped = list(
                            connection.execute(
                                "SELECT c.id,c.text,d.filename,c.page_or_slide FROM chunks c "
                                "JOIN documents d ON d.id=c.document_id WHERE d.class_id=?",
                                (class_id,),
                            )
                        )
                        scope = [by_id[c["id"]] for c in scoped]
                        lexical = [
                            by_id[c]
                            for c in lexical_candidates(connection, class_id, case["question"])
                        ]
                        query = encoder.query(case["question"])
                        scores = {i: float(np.max(vectors[i] @ query)) for i in scope}
                        dense = sorted(scope, key=lambda i: (-scores[i], i))[:50]
                        selected = select_context(fusion(lexical, dense), chunks)
                        timings.append((time.perf_counter() - start) * 1000)
                        if len(selected) > 8:
                            raise AssertionError("Invalid final context")
                reports.append(
                    {
                        "name": data["name"],
                        "chunks": len(chunks),
                        "samples": len(timings),
                        "median_ms": float(np.median(timings)),
                        "p95_ms": float(np.percentile(timings, 95)),
                        "max_ms": max(timings),
                    }
                )
    result = {
        "model": encoder.spec["name"],
        "encoder_fingerprint": encoder.fingerprint(),
        "boundary": __doc__,
        "repetitions_per_case": 3,
        "corpora": reports,
    }
    private_json(args.output, result)
    print(json.dumps(reports))


if __name__ == "__main__":
    main()
