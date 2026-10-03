"""The semantic service boundary never owns credentials or downloads models."""

from __future__ import annotations

import io
import json
from pathlib import Path
from uuid import uuid4

import pytest

from gct.ingest.parse import ParsedUnit
from gct.local import embedding_index, service
from gct.local.store import Library, LibraryError

READY = {"state": "ready", "mode": "hybrid", "message": "Semantic search is ready."}
FAILED = {
    "state": "failed",
    "mode": "lexical",
    "message": "Semantic search could not finish. Keyword search is available.",
}


def exchange(path, request, *replies, model_root=None):
    source = io.StringIO("\n".join(json.dumps(item) for item in (request, *replies)) + "\n")
    output = io.StringIO()
    code = service.run(source, output, library_path=path, model_root=model_root)
    return code, [json.loads(line) for line in output.getvalue().splitlines()]


@pytest.fixture
def saved(tmp_path):
    path = tmp_path / "library.sqlite3"
    with Library(path) as library:
        class_id = library.create_class("Memory")["id"]
        document = library.import_document(
            class_id,
            "Memory.pdf",
            b"synthetic original",
            [ParsedUnit("Active recall retrieves information from memory.", "Memory.pdf", 1)],
            1,
        )
    return path, class_id, document["id"]


@pytest.mark.parametrize("operation", ["search_status", "prepare_search"])
def test_local_search_actions_return_one_library_event_without_generation(
    saved, monkeypatch, operation
):
    path, class_id, _ = saved
    models = path.parent / "models"
    calls = []

    def action(library, selected_class, model_root, **options):
        assert isinstance(library, Library)
        assert selected_class == class_id
        assert model_root == models
        assert options == ({"deadline_seconds": 210} if operation == "prepare_search" else {})
        calls.append(operation)
        return READY

    monkeypatch.setattr(
        embedding_index, "prepare" if operation == "prepare_search" else "status", action
    )
    code, events = exchange(path, {"operation": operation, "class_id": class_id}, model_root=models)
    assert code == 0
    assert calls == [operation]
    assert events == [{"type": "library", "operation": operation, "value": READY}]


@pytest.mark.parametrize("operation", ["search_status", "prepare_search"])
def test_missing_local_model_keeps_keyword_library_usable_without_an_account(saved, operation):
    path, class_id, _ = saved
    code, events = exchange(
        path,
        {"operation": operation, "class_id": class_id},
        model_root=path.parent / "does-not-exist",
    )
    assert code == 0
    assert len(events) == 1 and events[0]["type"] == "library"
    status = events[0]["value"]
    assert set(status) == {"state", "mode", "message"}
    assert status["state"] == "unavailable" and status["mode"] == "lexical"
    with Library(path) as library:
        assert library.retrieve(class_id, "recall")


@pytest.mark.parametrize("hybrid", [False, True])
def test_automatic_ask_publishes_actual_retrieval_mode_before_generation(
    saved, monkeypatch, hybrid
):
    path, class_id, document_id = saved
    with Library(path) as library:
        chunks = library.retrieve(class_id, "recall")
    calls = []

    def retrieve(library, selected_class, question, model_root, document_id=None):
        calls.append((selected_class, question, model_root, document_id))
        return (chunks, READY) if hybrid else (None, FAILED)

    monkeypatch.setattr(embedding_index, "retrieve", retrieve)
    code, events = exchange(
        path,
        {
            "operation": "ask",
            "class_id": class_id,
            "document_id": document_id,
            "question": "What is recall?",
        },
        {"type": "generated", "text": "Recall retrieves information. [S1]\nCOVERAGE: complete"},
        model_root=path.parent / "models",
    )
    assert code == 0
    assert calls == [(class_id, "What is recall?", path.parent / "models", document_id)]
    assert [event["type"] for event in events] == ["search_status", "generate", "result"]
    assert events[0]["value"] == (READY if hybrid else FAILED)
    assert events[-1]["result"]["state"] == "GROUNDED"
    assert events[-1]["result"]["citations"][0]["chunk_id"] == chunks[0].chunk_id


def test_explicit_pages_never_load_semantic_search(saved, monkeypatch):
    path, class_id, document_id = saved

    def forbidden(*args, **kwargs):
        raise AssertionError("Page evidence must bypass semantic retrieval")

    monkeypatch.setattr(embedding_index, "retrieve", forbidden)
    code, events = exchange(
        path,
        {
            "operation": "ask",
            "class_id": class_id,
            "document_id": document_id,
            "pages": [1],
            "question": "Which technique makes me reconstruct concepts?",
        },
        {"type": "generated", "text": "Recall retrieves information. [S1]\nCOVERAGE: complete"},
    )
    assert code == 0
    assert [event["type"] for event in events] == ["generate", "result"]


@pytest.mark.parametrize("operation", ["search_status", "prepare_search", "ask"])
def test_canonical_library_failures_remain_errors(saved, monkeypatch, operation):
    path, class_id, _ = saved

    def damaged(*args, **kwargs):
        raise LibraryError("database_corrupt", "The local library needs recovery.")

    method = {"search_status": "status", "prepare_search": "prepare", "ask": "retrieve"}[operation]
    monkeypatch.setattr(embedding_index, method, damaged)
    request = {"operation": operation, "class_id": class_id}
    if operation == "ask":
        request["question"] = "What is recall?"
    code, events = exchange(path, request)
    assert code == 1
    assert events == [
        {
            "type": "error",
            "code": "database_corrupt",
            "message": "The local library needs recovery.",
        }
    ]


@pytest.mark.parametrize("operation", ["search_status", "prepare_search"])
@pytest.mark.parametrize(
    "extra", [{"model_root": "/tmp/model"}, {"api_key": "secret"}, {"pages": [1]}]
)
def test_semantic_requests_reject_ambient_paths_credentials_and_unknown_fields(
    tmp_path, operation, extra
):
    path = tmp_path / "absent.db"
    code, events = exchange(path, {"operation": operation, "class_id": str(uuid4()), **extra})
    assert code == 1 and events[0]["code"] == "invalid_request"
    assert not path.exists()


def test_relative_trusted_model_path_fails_before_creating_library(tmp_path):
    path = tmp_path / "absent.db"
    code, events = exchange(path, {"operation": "list_classes"}, model_root=Path("relative"))
    assert code == 1 and events[0]["code"] == "invalid_path"
    assert not path.exists()
