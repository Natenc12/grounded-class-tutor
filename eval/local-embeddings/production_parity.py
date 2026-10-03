"""Manual, local-model parity check against the measured experimental pipeline.

No generation or network. A temporary canonical library is built from a public
suite or the private corpus. Reports contain counts, timings, hashes and case IDs;
questions, source bodies, source filenames and machine paths remain private.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path

from runner import ROOT, digest, private_json, seal_network


def _imports():
    from gct.eval.local_quality import load_suite, private_cases, render_documents
    from gct.local import embedding_index, embeddings
    from gct.local.store import Library
    from gct.local_proof import MAX_FILE_BYTES, _load_document

    return (
        load_suite,
        private_cases,
        render_documents,
        embedding_index,
        embeddings,
        Library,
        MAX_FILE_BYTES,
        _load_document,
    )


def _references(path, name):
    if path is None:
        return None
    report = json.loads(Path(path).read_text())
    corpus = next((item for item in report["corpora"] if item["name"] == name), None)
    if corpus is None:
        raise ValueError("Reference report lacks the requested corpus")
    return corpus


def run(args):
    # Imports occur only after socket connections have been disabled.
    import numpy as np
    from encoder import Encoder as StudyEncoder

    (
        load_suite,
        private_cases,
        render_documents,
        index,
        embeddings,
        Library,
        max_file_bytes,
        load_document,
    ) = _imports()
    assets = embeddings.admit(Path(args.model_root))
    reference_corpus = _references(args.reference, args.reference_name or args.name)
    reference = (
        {case["id"]: case for case in reference_corpus["arms"]["hybrid"]["cases"]}
        if reference_corpus is not None
        else None
    )
    with tempfile.TemporaryDirectory(prefix="gct-production-parity-") as temporary:
        temporary = Path(temporary)
        if args.suite:
            suite, cases = load_suite(Path(args.suite))
            paths = render_documents(suite, temporary / "pdfs")
        elif args.questions and args.corpus:
            cases = private_cases(Path(args.questions))
            paths = [
                p for p in Path(args.corpus).iterdir() if p.suffix.lower() in {".pdf", ".pptx"}
            ]
        else:
            raise ValueError("Supply a suite or private questions and corpus")
        if not paths or len({p.name for p in paths}) != len(paths):
            raise ValueError("The corpus needs distinct nonempty source files")
        if reference is not None and set(reference) != {case.id for case in cases}:
            raise ValueError("Reference case identities do not match this suite")
        with Library(temporary / "library.sqlite3") as library:
            class_id = library.create_class("Isolated parity evaluation")["id"]
            for path in sorted(paths):
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                with os.fdopen(os.open(path, flags), "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > max_file_bytes:
                        raise ValueError("Source file exceeds the regular-file byte limit")
                    raw = source.read(max_file_bytes + 1)
                if not 0 < len(raw) <= max_file_bytes:
                    raise ValueError("Source file exceeds the nonempty byte limit")
                snapshot = temporary / path.name
                snapshot.write_bytes(raw)
                document = load_document({"file_path": str(snapshot)})
                library.import_document(
                    class_id, path.name, raw, document.units, document.page_count
                )
            # This measurement intentionally materializes the small comparison
            # corpus. The production encoder/index itself uses bounded streaming.
            rows = list(index._chunks(library, class_id))
            by_id = {row["id"]: row for row in rows}
            corpus_digest = digest(
                [
                    {
                        "text": row["text"],
                        "file": row["filename"],
                        "sha256": row["sha256"],
                        "page": row["page_or_slide"],
                        "ordinal": row["ordinal"],
                    }
                    for row in rows
                ]
            )
            case_digest = digest([case.__dict__ for case in cases])
            if reference_corpus is not None and (
                reference_corpus["corpus_digest"] != corpus_digest
                or reference_corpus["case_digest"] != case_digest
            ):
                raise ValueError("Reference belongs to different source bytes or cases")
            study_encoder = StudyEncoder(args.spec)
            study_vectors = study_encoder.encode([row["text"] for row in rows])
            streamed = embeddings.Encoder(assets).stream((row["id"], row["text"]) for row in rows)
            window_count = 0
            for row, vectors in zip(rows, study_vectors, strict=True):
                for ordinal, vector in enumerate(vectors):
                    actual = next(streamed, None)
                    expected = (row["id"], ordinal, vector.astype("<f4").tobytes())
                    if actual != expected:
                        raise AssertionError("Production embedding differs from study bytes")
                    window_count += 1
            if next(streamed, None) is not None:
                raise AssertionError("Production encoder emitted extra windows")
            del study_encoder, study_vectors, streamed
            started = time.perf_counter()
            status = index.prepare(library, class_id, assets.root)
            prepare_ms = (time.perf_counter() - started) * 1000
            if status["state"] != "ready":
                raise AssertionError("Production index did not become ready")
            latencies = []
            matched = 0
            for case in cases:
                started = time.perf_counter()
                chunks, status = index.retrieve(library, class_id, case.question, assets.root)
                latencies.append((time.perf_counter() - started) * 1000)
                if status["state"] != "ready":
                    raise AssertionError("Production query unexpectedly fell back")
                if reference is not None:
                    actual = [
                        {
                            "file": c.file,
                            "page": c.page_or_slide,
                            "ordinal": by_id[c.chunk_id]["ordinal"],
                        }
                        for c in chunks
                    ]
                    expected = [
                        {key: source[key] for key in ("file", "page", "ordinal")}
                        for source in reference[case.id]["retrieved"]
                    ]
                    if actual != expected:
                        raise AssertionError(f"Production ranking differs for case {case.id}")
                    matched += 1
            started = time.perf_counter()
            if index.status(library, class_id, assets.root)["state"] != "ready":
                raise AssertionError("Completed index failed its status validation")
            status_ms = (time.perf_counter() - started) * 1000
    files = ("src/gct/local/embeddings.py", "src/gct/local/embedding_index.py")
    report = {
        "schema": 1,
        "name": args.name,
        "chunks": len(rows),
        "windows": window_count,
        "questions": len(cases),
        "vectors_bit_identical": True,
        "reference_rankings_checked": matched,
        "reference_sha256": hashlib.sha256(Path(args.reference).read_bytes()).hexdigest()
        if args.reference
        else None,
        "case_ids": [case.id for case in cases],
        "model_fingerprint": assets.fingerprint,
        "corpus_digest": corpus_digest,
        "case_digest": case_digest,
        "source_sha256": {
            file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest() for file in files
        },
        "prepare_ms": prepare_ms,
        "status_ms": status_ms,
        "retrieve_ms": {
            "median": float(np.median(latencies)),
            "p95": float(np.percentile(latencies, 95)),
            "max": max(latencies),
        },
        "boundary": "Core calls include asset hashing, canonical source validation, model load, "
        "cached vector checks and ranking. Excludes process/app startup and generation.",
        "limitations": [
            "Counts and exact parity on these cases do not establish answer accuracy.",
            "Driver materializes a small comparison corpus; production does not.",
            "Without --reference, historical rankings are not checked.",
        ],
        "no_generation": True,
    }
    private_json(args.output, report)
    return {
        key: report[key]
        for key in (
            "name",
            "chunks",
            "windows",
            "questions",
            "vectors_bit_identical",
            "reference_rankings_checked",
        )
    }


def main():
    seal_network()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--suite")
    parser.add_argument("--questions")
    parser.add_argument("--corpus")
    parser.add_argument("--spec", required=True, help="Machine-local study MiniLM spec JSON")
    parser.add_argument(
        "--model-root", required=True, help="Directory with model.onnx/tokenizer.json"
    )
    parser.add_argument(
        "--reference", help="Prior runner report containing per-case selected sources"
    )
    parser.add_argument("--reference-name", help="Corpus name within the prior report")
    parser.add_argument("--output", required=True)
    print(json.dumps(run(parser.parse_args())))


if __name__ == "__main__":
    main()
