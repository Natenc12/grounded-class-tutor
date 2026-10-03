import tempfile
import unittest
from pathlib import Path

from runner import fusion, lexical_candidates, private_json, scored_case, select_context, summarize

from gct.ingest.parse import ParsedUnit
from gct.local.store import Library


class StudyTests(unittest.TestCase):
    def test_private_atomic_writer_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            private_json(path, {"synthetic": "private"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            alias = Path(tmp) / "alias.json"
            alias.symlink_to(path)
            with self.assertRaises(ValueError):
                private_json(alias, {"overwrite": True})
            self.assertIn("synthetic", path.read_text())

    def test_hybrid_formula_and_tie(self):
        self.assertEqual(fusion([0, 1], [1, 0]), [0, 1])
        self.assertEqual(fusion([0], [1, 0]), [0, 1])
        self.assertLessEqual(len(fusion(list(range(100)), list(range(100, 200)))), 100)

    def test_exact_context_cap_and_parent_identity(self):
        chunks = [{"file": "course.pdf", "page": i + 1, "text": "a" * 20000} for i in range(10)]
        self.assertEqual(select_context(list(range(10)), chunks), [0])
        chunks = [{"file": "course.pdf", "page": i + 1, "text": "a"} for i in range(10)]
        self.assertEqual(select_context(list(range(10)), chunks), list(range(8)))

    def test_multiple_groups_and_unsupported_denominator(self):
        chunks = [
            {"id": "a", "file": "a.pdf", "page": 1, "ordinal": 0},
            {"id": "b", "file": "a.pdf", "page": 2, "ordinal": 1},
        ]
        case = {
            "id": "x",
            "expectation": "answer",
            "tags": [],
            "source_groups": [[["a.pdf", 1]], [["a.pdf", 2]]],
        }
        row = scored_case(case, [0, 1], chunks)
        refusal = {**case, "id": "r", "expectation": "refuse", "source_groups": []}
        result = summarize([row, scored_case(refusal, [0], chunks)])
        self.assertEqual(result["by_k"]["1"]["all_required_groups"], 0)
        self.assertEqual(result["by_k"]["3"]["all_required_groups"], 1)
        self.assertEqual(result["by_k"]["3"]["answerable_cases"], 1)
        self.assertEqual(result["unsupported_with_candidates"], 1)

    def test_actual_lexical50_matches_fts8_and_scopes_before_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with Library(Path(tmp) / "library.sqlite3") as library:
                first = library.create_class("first")["id"]
                second = library.create_class("second")["id"]
                docs = []
                for cls, name in ((first, "a.pdf"), (first, "b.pdf"), (second, "c.pdf")):
                    units = [
                        ParsedUnit(f"cedar ledger passage {i} count {i * i}", name, i)
                        for i in range(1, 16)
                    ]
                    docs.append(library.import_document(cls, name, name.encode(), units, 15))
                actual = library.retrieve(first, "cedar ledger count")
                expanded = lexical_candidates(library.connection, first, "cedar ledger count")
                self.assertEqual(expanded[:8], [c.chunk_id for c in actual])
                expected = {
                    r[0]
                    for r in library.connection.execute(
                        "SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id "
                        "WHERE d.class_id=?",
                        (first,),
                    )
                }
                self.assertEqual(set(expanded), expected)
                scoped = lexical_candidates(
                    library.connection,
                    first,
                    "cedar ledger count",
                    document_id=docs[0]["id"],
                    pages=[13],
                )
                rows = library.connection.execute(
                    "SELECT id FROM chunks WHERE document_id=? AND page_or_slide=13",
                    (docs[0]["id"],),
                ).fetchall()
                self.assertEqual(scoped, [r[0] for r in rows])
                self.assertEqual(
                    lexical_candidates(
                        library.connection, second, "cedar", document_id=docs[0]["id"]
                    ),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
