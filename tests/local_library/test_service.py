"""The desktop's real one-request service boundary and persistent workflows."""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from reportlab.pdfgen.canvas import Canvas

from gct.ingest.parse import ParsedUnit
from gct.local import service
from gct.local.store import Library


def exchange(path, request, *replies):
    source = io.StringIO("\n".join(json.dumps(value) for value in (request, *replies)) + "\n")
    destination = io.StringIO()
    status = service.run(source, destination, library_path=path)
    return status, [json.loads(line) for line in destination.getvalue().splitlines()]


def value(path, request):
    status, events = exchange(path, request)
    assert status == 0, events
    assert len(events) == 1
    assert events[0]["type"] == "library"
    assert events[0]["operation"] == request["operation"]
    return events[0]["value"]


def pdf(path, texts):
    canvas = Canvas(str(path))
    for text in texts:
        if text:
            canvas.drawString(72, 720, text)
        canvas.showPage()
    canvas.save()
    return path


def cli(path, request, *replies):
    completed = subprocess.run(
        [sys.executable, "-m", "gct.local.service", "--library", str(path)],
        input="\n".join(json.dumps(value) for value in (request, *replies)) + "\n",
        text=True,
        capture_output=True,
        timeout=10,
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONNOUSERSITE": "1",
        },
    )
    assert completed.stderr == ""
    return completed.returncode, [json.loads(line) for line in completed.stdout.splitlines()]


def test_real_process_restart_import_question_citation_and_delete(tmp_path):
    path = tmp_path / "library.db"
    status, events = cli(path, {"operation": "create_class", "name": "Learning"})
    assert status == 0
    class_id = events[0]["value"]["id"]
    original = pdf(
        tmp_path / "Lecture.pdf",
        ["An unrelated first page.", "", "Active recall retrieves information from memory."],
    )
    status, events = cli(
        path, {"operation": "import_document", "class_id": class_id, "file_path": str(original)}
    )
    assert status == 0
    document = events[0]["value"]
    original.unlink()
    status, events = cli(
        path, {"operation": "get_document", "class_id": class_id, "document_id": document["id"]}
    )
    assert status == 0
    assert events[0]["value"]["document"] == document
    preview = events[0]["value"]["preview"]
    assert preview["page_count"] == 3
    assert preview["pages"][1]["text"] == ""
    status, events = cli(
        path,
        {"operation": "ask", "class_id": class_id, "question": "What is active recall?"},
        {
            "type": "generated",
            "text": "Recall retrieves information from memory. [S1]\nCOVERAGE: complete",
        },
    )
    assert status == 0
    assert [event["type"] for event in events] == ["generate", "result"]
    result = events[-1]["result"]
    assert result["state"] == "GROUNDED"
    assert result["citations"][0]["page_or_slide"] == 3
    citation_id = result["citations"][0]["chunk_id"]
    status, events = cli(
        path, {"operation": "citation", "class_id": class_id, "chunk_id": citation_id}
    )
    assert status == 0
    assert events[0]["value"]["document_id"] == document["id"]
    assert "retrieves" in events[0]["value"]["text"]
    status, events = cli(
        path, {"operation": "delete_document", "class_id": class_id, "document_id": document["id"]}
    )
    assert status == 0 and events[0]["value"] is None
    status, events = cli(
        path, {"operation": "citation", "class_id": class_id, "chunk_id": citation_id}
    )
    assert status == 1 and events[0]["code"] == "citation_missing"
    status, events = cli(path, {"operation": "list_documents", "class_id": class_id})
    assert status == 0 and events[0]["value"] == []


def test_listing_backup_and_class_deletion_return_only_declared_wire_shape(tmp_path):
    path = tmp_path / "library.db"
    assert value(path, {"operation": "list_classes"}) == []
    course = value(path, {"operation": "create_class", "name": "A"})
    assert value(path, {"operation": "list_classes"}) == [course]
    backup = tmp_path / "saved.db"
    assert value(path, {"operation": "backup", "output_path": str(backup)}) is None
    assert value(path, {"operation": "delete_class", "class_id": course["id"]}) is None
    assert value(path, {"operation": "list_classes"}) == []
    assert value(backup, {"operation": "list_classes"}) == [course]


def test_explicit_pages_keep_nonmatching_evidence_and_saved_citation_id(tmp_path):
    path = tmp_path / "library.db"
    with Library(path) as library:
        class_id = library.create_class("A")["id"]
        document = library.import_document(
            class_id,
            "Lecture.pdf",
            b"original",
            [
                ParsedUnit("Active recall retrieves facts from memory.", "Lecture.pdf", 1),
                ParsedUnit("Spaced practice spreads sessions over time.", "Lecture.pdf", 2),
            ],
            2,
        )
        saved_chunk = library.retrieve(class_id, "recall")[0].chunk_id
    request = {
        "operation": "ask",
        "class_id": class_id,
        "document_id": document["id"],
        "pages": [1],
        "question": "What technique requires mental reconstruction?",
    }
    status, events = exchange(
        path,
        request,
        {
            "type": "generated",
            "text": "Active recall requires retrieving facts. [S1]\nCOVERAGE: complete",
        },
    )
    assert status == 0
    assert "Active recall" in events[0]["messages"][1]["content"]
    assert "Spaced practice" not in events[0]["messages"][1]["content"]
    assert events[-1]["result"]["citations"][0]["chunk_id"] == saved_chunk


def test_selected_evidence_is_rejected_whole_when_context_overflows(tmp_path):
    path = tmp_path / "library.db"
    with Library(path) as library:
        class_id = library.create_class("A")["id"]
        document = library.import_document(
            class_id,
            "Lecture.pdf",
            b"original",
            [
                ParsedUnit("recall " * 3500, "Lecture.pdf", 1),
                ParsedUnit("memory " * 3500, "Lecture.pdf", 2),
            ],
            2,
        )
    status, events = exchange(
        path,
        {
            "operation": "ask",
            "class_id": class_id,
            "document_id": document["id"],
            "pages": [1, 2],
            "question": "Explain recall.",
        },
    )
    assert status == 1
    assert [event["type"] for event in events] == ["error"]
    assert events[0]["code"] == "context_too_large"


def test_source_changes_after_snapshot_cannot_change_saved_original_or_parsed_text(
    tmp_path, monkeypatch
):
    path = tmp_path / "library.db"
    class_id = value(path, {"operation": "create_class", "name": "A"})["id"]
    selected = pdf(tmp_path / "Lecture.pdf", ["Immutable evidence in the chosen source."])
    original = selected.read_bytes()
    load = service._load_document

    def change_original_before_parse(request):
        selected.write_bytes(b"Replaced after admission")
        assert request["file_path"] != str(selected)
        assert Path(request["file_path"]).parent.parent == selected.parent
        return load(request)

    monkeypatch.setattr(service, "_load_document", change_original_before_parse)
    document = value(
        path, {"operation": "import_document", "class_id": class_id, "file_path": str(selected)}
    )
    with Library(path) as library:
        assert library.original(class_id, document["id"])[1] == original
        assert "Immutable evidence" in library.pages(class_id, document["id"])[0]["text"]


def test_parser_stdout_stderr_logs_and_exception_paths_cannot_escape(tmp_path, monkeypatch, capsys):
    path = tmp_path / "library.db"
    class_id = value(path, {"operation": "create_class", "name": "A"})["id"]
    selected = tmp_path / "SECRET-PRIVATE-NAME.pdf"
    selected.write_bytes(b"fake PDF")

    def noisy_parser(_request):
        print("SECRET document text")
        print("SECRET local path", file=sys.stderr)
        logging.error("SECRET parser diagnostic")
        raise RuntimeError("SECRET /Users/private/document.pdf")

    monkeypatch.setattr(service, "_load_document", noisy_parser)
    status, events = exchange(
        path, {"operation": "import_document", "class_id": class_id, "file_path": str(selected)}
    )
    assert status == 1 and events[0]["code"] == "internal"
    assert "SECRET" not in json.dumps(events)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert list(selected.parent.glob("gct-import-*")) == []
    assert value(path, {"operation": "list_documents", "class_id": class_id}) == []


def test_killed_parser_snapshot_stays_inside_parent_owned_staging(tmp_path):
    path = tmp_path / "library.db"
    class_id = value(path, {"operation": "create_class", "name": "A"})["id"]
    staging = tmp_path / "owned-source-folder"
    staging.mkdir(mode=0o700)
    selected = pdf(staging / "Lecture.pdf", ["Private material must stay inside owned staging."])
    script = """
import os, sys
from gct.local import service
service._load_document = lambda _request: os._exit(71)
raise SystemExit(service.run(sys.stdin, sys.stdout, library_path=sys.argv[1]))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path)],
        input=json.dumps(
            {"operation": "import_document", "class_id": class_id, "file_path": str(selected)}
        )
        + "\n",
        text=True,
        capture_output=True,
        timeout=10,
        env={
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "PYTHON_DOTENV_DISABLED": "1",
        },
    )
    assert completed.returncode == 71
    leftovers = list(staging.glob("gct-import-*/Lecture.pdf"))
    assert len(leftovers) == 1
    assert leftovers[0].read_bytes() == selected.read_bytes()
    # Electron's existing sourceStore recursively removes this owned folder
    # after process shutdown, including parser children killed before finally.
    shutil.rmtree(staging)
    assert not leftovers[0].exists()
    assert value(path, {"operation": "list_documents", "class_id": class_id}) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "unknown"},
        {"operation": []},
        {"operation": "list_classes", "owner_id": "foreign"},
        {"operation": "create_class"},
        {"operation": "create_class", "name": None},
        {"operation": "create_class", "name": "A", "api_key": "SECRET"},
        {"operation": "import_document", "class_id": str(uuid4()), "file_path": "relative.pdf"},
        {"operation": "backup", "output_path": "relative.db"},
        {"operation": "backup", "output_path": "/tmp/bad\x00.db"},
        {"operation": "ask", "class_id": str(uuid4()), "question": "x", "model": "forbidden"},
        {"operation": "ask", "class_id": str(uuid4()), "question": ""},
        {"operation": "ask", "class_id": str(uuid4()), "question": "x" * 2001},
        {"operation": "ask", "class_id": str(uuid4()), "question": "bad\ud800"},
        {"operation": "ask", "class_id": str(uuid4()), "question": "x", "pages": [1]},
        {"operation": "ask", "class_id": str(uuid4()), "question": "x", "document_id": None},
    ],
)
def test_unknown_missing_and_forbidden_fields_fail_before_database_creation(tmp_path, payload):
    path = tmp_path / "library.db"
    status, events = exchange(path, payload)
    assert status == 1
    assert len(events) == 1 and events[0]["type"] == "error"
    assert not path.exists()
    assert "SECRET" not in json.dumps(events)


@pytest.mark.parametrize("pages", [[], [1, 1], [True], [0], [501], [1, 2, 3, 4, 5, 6], "1", None])
def test_page_selection_is_strictly_bounded_before_database_creation(tmp_path, pages):
    path = tmp_path / "library.db"
    status, events = exchange(
        path,
        {
            "operation": "ask",
            "class_id": str(uuid4()),
            "document_id": str(uuid4()),
            "question": "x",
            "pages": pages,
        },
    )
    assert status == 1 and events[0]["code"] == "invalid_pages"
    assert not path.exists()


def test_empty_retrieval_refuses_without_requesting_generation(tmp_path):
    path = tmp_path / "library.db"
    class_id = value(path, {"operation": "create_class", "name": "Empty"})["id"]
    status, events = exchange(
        path, {"operation": "ask", "class_id": class_id, "question": "What do my materials teach?"}
    )
    assert status == 0
    assert [event["type"] for event in events] == ["result"]
    assert events[0]["result"]["state"] == "REFUSAL"


def test_generation_error_is_safe_terminal_result_and_does_not_retry(tmp_path):
    path = tmp_path / "library.db"
    with Library(path) as library:
        class_id = library.create_class("A")["id"]
        library.import_document(
            class_id, "a.pdf", b"bytes", [ParsedUnit("Recall retrieves memory.", "a.pdf", 1)], 1
        )
    status, events = exchange(
        path,
        {"operation": "ask", "class_id": class_id, "question": "What is recall?"},
        {"type": "generation_error", "message": "SECRET"},
    )
    assert status == 0
    assert [event["type"] for event in events] == ["generate", "result"]
    assert events[-1]["result"]["state"] == "ERROR"
    assert "SECRET" not in json.dumps(events)


@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystem admission")
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_source_admission_cannot_follow_links_or_block_on_fifo(tmp_path, kind):
    path = tmp_path / "library.db"
    class_id = value(path, {"operation": "create_class", "name": "A"})["id"]
    selected = tmp_path / "unsafe.pdf"
    if kind == "symlink":
        target = pdf(tmp_path / "original.pdf", ["Preserve me."])
        selected.symlink_to(target)
    else:
        os.mkfifo(selected)
    status, events = cli(
        path, {"operation": "import_document", "class_id": class_id, "file_path": str(selected)}
    )
    assert status == 1 and events[0]["type"] == "error"
    assert value(path, {"operation": "list_documents", "class_id": class_id}) == []


def test_invalid_json_and_relative_library_path_have_closed_errors(tmp_path):
    for content in ["[1,2]\n", "not-json\n", '{"x": ' + "[" * 1000 + "}\n"]:
        output = io.StringIO()
        assert service.run(io.StringIO(content), output, library_path=tmp_path / "x.db") == 1
        assert json.loads(output.getvalue())["type"] == "error"
    output = io.StringIO()
    assert (
        service.run(
            io.StringIO('{"operation":"list_classes"}\n'), output, library_path="relative.db"
        )
        == 1
    )
    assert json.loads(output.getvalue())["code"] == "invalid_path"
