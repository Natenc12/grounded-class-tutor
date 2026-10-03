"""Frozen two-stage local retrieval study; no generation or network.

Prepare with the existing GCT dev environment; encode with the isolated ONNX
environment. Inputs and reports stay in ignored .ship, including private text.
Nothing opens the user's live library. Baseline calls the actual production
retriever; only the experimental hybrid expands its lexical candidate pool to50.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import stat
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CODE = Path(__file__).resolve().parent
KS = (1, 3, 5, 8)
CANDIDATES = 50
RRF_K = 60


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def core_sources():
    """Bind preparation to the caller's actual core, not a historical Git label."""
    return {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
        for path in (
            "src/gct/contracts.py",
            "src/gct/ingest/parse.py",
            "src/gct/ingest/chunk.py",
            "src/gct/local/store.py",
            "src/gct/local/search.py",
            "src/gct/local_proof.py",
        )
    }


def private_json(path, value):
    """Publish a complete mode600 artifact; never follow an output symlink."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Study artifacts cannot replace symbolic links")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".study-private-", delete=False
        ) as target:
            temporary = Path(target.name)
            # mkstemp has already atomically created the file with mode0600.
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def no_network(*args, **kwargs):
    raise RuntimeError("Network access is prohibited during the local embedding study")


def seal_network():
    # Assets must exist already. Neither corpus stage has a network client.
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network
    socket.create_connection = no_network


def lexical_candidates(connection, class_id, question, *, document_id=None, pages=None):
    """Exact production query/ranking, with only its candidate limit expanded."""
    from gct.local.search import query_expression

    expression = query_expression(question)
    if not expression:
        return []
    conditions = ["chunks_fts MATCH ?", "d.class_id = ?"]
    parameters = [expression, class_id]
    if document_id is not None:
        conditions.append("d.id = ?")
        parameters.append(document_id)
    if pages is not None:
        if document_id is None or not pages:
            raise ValueError("Pages require a nonempty document scope")
        conditions.append("c.page_or_slide IN (" + ",".join("?" for _ in pages) + ")")
        parameters.extend(pages)
    return [
        row[0]
        for row in connection.execute(
            "SELECT c.id FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
            "JOIN documents d ON d.id = c.document_id WHERE "
            + " AND ".join(conditions)
            + " ORDER BY bm25(chunks_fts,1.0,0.35), d.filename, d.sha256, "
            "c.page_or_slide,c.ordinal LIMIT ?",
            [*parameters, CANDIDATES],
        )
    ]


def prepare_one(name, paths, cases, output):
    from gct.local.store import Library
    from gct.local_proof import MAX_FILE_BYTES, _load_document

    if not paths or len({path.name for path in paths}) != len(paths):
        raise ValueError("Distinct nonempty corpus filenames required")
    with tempfile.TemporaryDirectory(prefix="gct-embedding-study-") as temporary:
        temporary = Path(temporary)
        with Library(temporary / "library.sqlite3") as library:
            class_id = library.create_class("Isolated embedding experiment")["id"]
            for path in sorted(paths):
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                with os.fdopen(os.open(path, flags), "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                        raise ValueError("Corpus input exceeds the regular file byte cap")
                    original = source.read(MAX_FILE_BYTES + 1)
                if not 0 < len(original) <= MAX_FILE_BYTES:
                    raise ValueError("Corpus input exceeds the nonempty byte cap")
                snapshot = temporary / path.name
                snapshot.write_bytes(original)
                document = _load_document({"file_path": str(snapshot)})
                library.import_document(
                    class_id, path.name, original, document.units, document.page_count
                )
            chunks = [
                dict(row)
                for row in library.connection.execute(
                    "SELECT c.id,c.text,d.filename AS file,d.sha256,c.page_or_slide AS page, "
                    "c.ordinal,d.id AS document_id,d.class_id FROM chunks c "
                    "JOIN documents d ON d.id=c.document_id WHERE d.class_id=? "
                    "ORDER BY d.filename,d.sha256,c.page_or_slide,c.ordinal",
                    (class_id,),
                )
            ]
            index = {chunk["id"]: i for i, chunk in enumerate(chunks)}
            available = {(chunk["file"], chunk["page"]) for chunk in chunks}
            rows = []
            for case in cases:
                if bool(case.source_groups) != (case.expectation != "refuse"):
                    raise ValueError("Answerable cases require evidence groups")
                if any(not available.intersection(group) for group in case.source_groups):
                    raise ValueError("Expected source absent from parsed corpus")
                rows.append(
                    {
                        **case.__dict__,
                        "fts": [
                            index[c.chunk_id] for c in library.retrieve(class_id, case.question)
                        ],
                        "lexical50": [
                            index[c]
                            for c in lexical_candidates(library.connection, class_id, case.question)
                        ],
                        "scope": list(range(len(chunks))),
                    }
                )
    # Keep live UUIDs for provenance but exclude them from repeatable content identity.
    identity = [{k: c[k] for k in ("text", "file", "sha256", "page", "ordinal")} for c in chunks]
    data = {
        "name": name,
        "chunks": chunks,
        "cases": rows,
        "corpus_digest": digest(identity),
        "case_digest": digest([case.__dict__ for case in cases]),
        "baseline_reference_git": "06b45e6129c456ae7736aea0f11469716341820d",
        "core_source_sha256": core_sources(),
        "retrieval_source_sha256": hashlib.sha256(
            (ROOT / "src/gct/local/search.py").read_bytes()
        ).hexdigest(),
    }
    private_json(output, data)
    return {
        "name": name,
        "chunks": len(chunks),
        "cases": len(rows),
        "corpus_digest": data["corpus_digest"],
    }


def prepare(args):
    from gct.eval.local_quality import load_suite, private_cases, render_documents

    summaries = []
    if args.suite:
        suite, cases = load_suite(Path(args.suite))
        with tempfile.TemporaryDirectory(prefix="gct-synthetic-study-") as temporary:
            paths = render_documents(suite, Path(temporary))
            summaries.append(prepare_one(args.name, paths, cases, Path(args.output)))
    elif args.questions and args.corpus:
        cases = private_cases(Path(args.questions))
        paths = [p for p in Path(args.corpus).iterdir() if p.suffix.lower() in {".pdf", ".pptx"}]
        summaries.append(prepare_one(args.name, paths, cases, Path(args.output)))
    else:
        raise ValueError("Specify a public suite or private questions and corpus")
    return summaries


def write_specs(args):
    """Resolve the checked-in asset manifest into private machine-local specs."""
    candidates = json.loads((CODE / "runtime" / "assets.json").read_text())
    results = []
    for candidate in candidates:
        spec = {
            key: candidate[key]
            for key in (
                "key",
                "name",
                "model_id",
                "revision",
                "max_tokens",
                "pooling",
                "query_prefix",
                "dimensions",
            )
        }
        for key in ("model_path", "tokenizer_path"):
            spec[key] = str(Path(args.assets_root).resolve() / candidate[key])
        path = Path(args.output_dir) / f"{candidate['key']}-spec.json"
        private_json(path, spec)
        results.append(candidate["key"])
    return {"wrote_local_specs": results}


def select_context(order, chunks):
    """Use the product's exact labeled character accounting and final limit8."""
    selected = []
    length = 0
    for index in order:
        chunk = chunks[index]
        block = f"[S{len(selected) + 1}] ({chunk['file']}, p.{chunk['page']})\n{chunk['text']}"
        increment = len(block) + (2 if selected else 0)
        if length + increment > 40000:
            continue
        selected.append(index)
        length += increment
        if len(selected) == 8:
            break
    return selected


def fusion(lexical, dense):
    scores = {}
    for ranking in (lexical[:CANDIDATES], dense[:CANDIDATES]):
        for rank, index in enumerate(ranking, 1):
            scores[index] = scores.get(index, 0) + 1 / (RRF_K + rank)
    # Index is the stable file/hash/page/ordinal tie order from preparation.
    return sorted(scores, key=lambda index: (-scores[index], index))


def scored_case(case, order, chunks):
    sources = [(chunks[i]["file"], chunks[i]["page"]) for i in order]
    ranks = [
        next(
            (
                rank
                for rank, source in enumerate(sources, 1)
                if source in [tuple(pair) for pair in group]
            ),
            None,
        )
        for group in case["source_groups"]
    ]
    return {
        "id": case["id"],
        "expectation": case["expectation"],
        "tags": case["tags"],
        "required_group_ranks": ranks,
        "candidate_count": len(order),
        "retrieved": [
            {
                "chunk_id": chunks[i]["id"],
                "file": chunks[i]["file"],
                "page": chunks[i]["page"],
                "ordinal": chunks[i]["ordinal"],
            }
            for i in order
        ],
    }


def summarize(rows):
    answerable = [row for row in rows if row["required_group_ranks"]]
    unsupported = [row for row in rows if row["expectation"] == "refuse"]
    result = {}
    for k in KS:
        result[str(k)] = {
            "any_required_source": sum(
                any(r is not None and r <= k for r in row["required_group_ranks"])
                for row in answerable
            ),
            "all_required_groups": sum(
                all(r is not None and r <= k for r in row["required_group_ranks"])
                for row in answerable
            ),
            "answerable_cases": len(answerable),
            "required_groups_found": sum(
                r is not None and r <= k for row in answerable for r in row["required_group_ranks"]
            ),
            "required_groups_total": sum(len(row["required_group_ranks"]) for row in answerable),
        }
    return {
        "by_k": result,
        "unsupported_cases": len(unsupported),
        "unsupported_with_candidates": sum(row["candidate_count"] > 0 for row in unsupported),
    }


def compare(baseline, candidate):
    def full(row, k):
        return bool(row["required_group_ranks"]) and all(
            rank is not None and rank <= k for rank in row["required_group_ranks"]
        )

    return {
        str(k): {
            "gains": [
                a["id"]
                for a, b in zip(baseline, candidate, strict=True)
                if not full(a, k) and full(b, k)
            ],
            "losses": [
                a["id"]
                for a, b in zip(baseline, candidate, strict=True)
                if full(a, k) and not full(b, k)
            ],
        }
        for k in KS
    }


def run(args):
    import numpy as np
    from encoder import Encoder

    encoder = Encoder(args.spec)
    reports = []
    for input_path in args.inputs:
        data = json.loads(Path(input_path).read_text())
        chunks = data["chunks"]
        # Released cross-class adapter freezes each class's batch boundaries so
        # foreign documents cannot perturb primary vectors via int8 padding.
        if "encoding_groups" in data:
            groups = data["encoding_groups"]
            if sorted(i for group in groups for i in group) != list(range(len(chunks))):
                raise ValueError("Encoding groups must partition exact parent chunks")
            vectors = [None] * len(chunks)
            stats = []
            for group in groups:
                batch = encoder.encode([chunks[i]["text"] for i in group])
                for i, vector in zip(group, batch, strict=True):
                    vectors[i] = vector
                stats.append(dict(encoder.last_stats))
            index_stats = {
                key: sum(row[key] for row in stats)
                for key in (
                    "texts",
                    "tokens",
                    "windows",
                    "multiwindow_texts",
                    "batches",
                    "inference_ms",
                    "total_ms",
                    "truncated_tokens",
                )
            }
            index_stats["class_batches"] = len(groups)
        else:
            vectors = encoder.encode([c["text"] for c in chunks])
            index_stats = dict(encoder.last_stats)
        rows = {"fts": [], "dense": [], "hybrid": []}
        timings = []
        for case in data["cases"]:
            start = time.perf_counter()
            query = encoder.query(case["question"])
            scope = case["scope"]
            scores = {i: float(np.max(vectors[i] @ query)) for i in scope}
            dense = sorted(scope, key=lambda i: (-scores[i], i))[:CANDIDATES]
            lexical = [i for i in case["lexical50"] if i in scores]
            hybrid = fusion(lexical, dense)
            timings.append((time.perf_counter() - start) * 1000)
            orders = {
                "fts": case["fts"],
                "dense": select_context(dense, chunks),
                "hybrid": select_context(hybrid, chunks),
            }
            for key, order in orders.items():
                if any(i not in scope for i in order):
                    raise AssertionError("Evidence escaped its admitted scope")
                rows[key].append(scored_case(case, order, chunks))
        reports.append(
            {
                "name": data["name"],
                "corpus_digest": data["corpus_digest"],
                "input_sha256": hashlib.sha256(Path(input_path).read_bytes()).hexdigest(),
                "core_source_sha256": data.get("core_source_sha256"),
                "selected_class_id": data.get("selected_class_id"),
                "scope_digest": digest([case["scope"] for case in data["cases"]]),
                "encoding_groups_digest": digest(data.get("encoding_groups")),
                "case_digest": data["case_digest"],
                "chunks": len(chunks),
                "index": index_stats,
                "query_ms": {
                    "count": len(timings),
                    "median": float(np.median(timings)),
                    "p95": float(np.percentile(timings, 95)),
                    "max": max(timings),
                },
                "arms": {
                    key: {
                        "summary": summarize(values),
                        "cases": values,
                        "by_tag": {
                            tag: summarize([r for r in values if tag in r["tags"]])
                            for tag in sorted({t for r in values for t in r["tags"]})
                        },
                    }
                    for key, values in rows.items()
                },
                "dense_vs_fts": compare(rows["fts"], rows["dense"]),
                "hybrid_vs_fts": compare(rows["fts"], rows["hybrid"]),
            }
        )
    result = {
        "schema": 1,
        "model": encoder.spec["name"],
        "encoder_fingerprint": encoder.fingerprint(),
        "encoder_code_sha256": hashlib.sha256((CODE / "encoder.py").read_bytes()).hexdigest(),
        "runner_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "config": {
            "rrf_k": RRF_K,
            "candidates": CANDIDATES,
            "final_k": 8,
            "max_context_chars": 40000,
            "no_generation": True,
            "network_blocked": True,
        },
        "load_ms": encoder.load_ms,
        "corpora": reports,
        "limitations": [
            "Recall measures expected source pages, not claim support or answer accuracy.",
            "Unsupported questions may retrieve candidates; no refusal was generated.",
            "Existing development suites are known; fresh holdout remains separate.",
        ],
    }
    private_json(args.output, result)
    return {
        "model": result["model"],
        "corpora": [
            {
                "name": r["name"],
                "all_groups_at8": {
                    key: arm["summary"]["by_k"]["8"]["all_required_groups"]
                    for key, arm in r["arms"].items()
                },
                "answerable": r["arms"]["fts"]["summary"]["by_k"]["8"]["answerable_cases"],
            }
            for r in reports
        ],
    }


def main():
    seal_network()
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    prep = subs.add_parser("prepare")
    prep.add_argument("--suite")
    prep.add_argument("--questions")
    prep.add_argument("--corpus")
    prep.add_argument("--name", required=True)
    prep.add_argument("--output", required=True)
    infer = subs.add_parser("run")
    infer.add_argument("--spec", required=True)
    infer.add_argument("--inputs", nargs="+", required=True)
    infer.add_argument("--output", required=True)
    specs = subs.add_parser("specs")
    specs.add_argument("--assets-root", required=True)
    specs.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    action = {"prepare": prepare, "run": run, "specs": write_specs}[args.command]
    print(json.dumps(action(args)))


if __name__ == "__main__":
    main()
