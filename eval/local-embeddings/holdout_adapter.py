"""Released holdout adapter only: import class scopes, preserve frozen ranking."""

import argparse
import json
import tempfile
from pathlib import Path

from runner import core_sources, digest, lexical_candidates, private_json, seal_network

from gct.eval.local_quality import load_suite, render_documents
from gct.local.store import Library
from gct.local_proof import _load_document


def main():
    seal_network()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--foreign", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    suite, cases = load_suite(Path(args.suite))
    foreign = json.loads(Path(args.foreign).read_text())
    with tempfile.TemporaryDirectory(prefix="gct-holdout-") as tmp:
        tmp = Path(tmp)
        with Library(tmp / "library.sqlite3") as library:
            primary = library.create_class("Released holdout primary")["id"]
            class_ids = [primary]

            def import_class(class_id, documents, folder):
                for path in render_documents({"documents": documents}, folder):
                    original = path.read_bytes()
                    document = _load_document({"file_path": str(path)})
                    library.import_document(
                        class_id, path.name, original, document.units, document.page_count
                    )

            import_class(primary, suite["documents"], tmp / "primary")

            def snapshot(name, selected):
                chunks = []
                encoding_groups = []
                for class_id in class_ids:
                    start = len(chunks)
                    chunks.extend(
                        dict(row)
                        for row in library.connection.execute(
                            "SELECT c.id,c.text,d.filename AS file,d.sha256,"
                            "c.page_or_slide AS page, "
                            "c.ordinal,d.id AS document_id,d.class_id FROM chunks c "
                            "JOIN documents d ON d.id=c.document_id WHERE d.class_id=? "
                            "ORDER BY d.filename,d.sha256,c.page_or_slide,c.ordinal",
                            (class_id,),
                        )
                    )
                    if len(chunks) > start:
                        encoding_groups.append(list(range(start, len(chunks))))
                index = {chunk["id"]: i for i, chunk in enumerate(chunks)}
                scope = [i for i, chunk in enumerate(chunks) if chunk["class_id"] == selected]
                rows = [
                    {
                        **case.__dict__,
                        "scope": scope,
                        "fts": [
                            index[c.chunk_id] for c in library.retrieve(selected, case.question)
                        ],
                        "lexical50": [
                            index[c]
                            for c in lexical_candidates(library.connection, selected, case.question)
                        ],
                    }
                    for case in cases
                ]
                identity = [
                    {k: c[k] for k in ("text", "file", "sha256", "page", "ordinal")} for c in chunks
                ]
                private_json(
                    output_dir / f"{name}.json",
                    {
                        "name": name,
                        "chunks": chunks,
                        "cases": rows,
                        "encoding_groups": encoding_groups,
                        "selected_class_id": selected,
                        "corpus_digest": digest(identity),
                        "case_digest": digest([c.__dict__ for c in cases]),
                        "core_source_sha256": core_sources(),
                        "adapter": "Class batches preserve primary vectors across foreign imports",
                    },
                )
                return {
                    "name": name,
                    "chunks": len(chunks),
                    "selected_chunks": len(scope),
                    "cases": len(rows),
                }

            reports = [snapshot("holdout-before", primary)]
            for number, cls in enumerate(foreign["classes"]):
                class_id = library.create_class(cls["name"])["id"]
                class_ids.append(class_id)
                import_class(class_id, cls["documents"], tmp / f"foreign-{number}")
            reports.append(snapshot("holdout-after", primary))
            empty = library.create_class(foreign["empty_class_name"])["id"]
            class_ids.append(empty)
            reports.append(snapshot("holdout-empty", empty))
    print(json.dumps(reports))


if __name__ == "__main__":
    main()
