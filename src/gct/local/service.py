"""One-request local desktop service, carried over bounded JSON lines.

Run ``python -m gct.local.service --library /absolute/path/library.sqlite3``.
Only the Electron main process supplies admitted file/backup paths. This peer
has no account credentials or provider client; it delegates generation text to
the parent while retaining Grounder and citation ownership.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import stat
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from gct.contracts import RetrievedChunk
from gct.grounder.answer import _build_labeled_context, answer
from gct.ingest.parse import ParsedUnit
from gct.local.store import MAX_FILE_BYTES, Library, LibraryError
from gct.local_proof import (
    MAX_QUESTION_CHARS,
    MAX_SELECTED_PAGES,
    MAX_TEXT_CHARS,
    Document,
    ProofError,
    StdioGeneration,
    _DiscardOutput,
    _emit,
    _load_document,
    _read_object,
    inspect_document,
)

_OPERATIONS = {
    "list_classes": (set(), set()),
    "create_class": ({"name"}, set()),
    "list_documents": ({"class_id"}, set()),
    "import_document": ({"class_id", "file_path"}, set()),
    "get_document": ({"class_id", "document_id"}, set()),
    "delete_document": ({"class_id", "document_id"}, set()),
    "delete_class": ({"class_id"}, set()),
    "citation": ({"class_id", "chunk_id"}, set()),
    "backup": ({"output_path"}, set()),
    "search_status": ({"class_id"}, set()),
    "prepare_search": ({"class_id"}, set()),
    "ask": ({"class_id", "question"}, {"document_id", "pages"}),
}


def _path(value: object) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4_096
        or "\x00" in value
        or not Path(value).is_absolute()
    ):
        raise ProofError("invalid_path", "Choose a valid local file through the app.")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ProofError("invalid_path", "Choose a valid local file through the app.") from exc
    return Path(value)


def _request(value: dict) -> dict:
    operation = value.get("operation")
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise ProofError("invalid_operation", "Choose an available library action.")
    required, optional = _OPERATIONS[operation]
    fields = set(value) - {"operation"}
    if not required.issubset(fields) or fields - required - optional:
        raise ProofError(
            "invalid_request", "The library request has missing or unsupported fields."
        )
    for field, limit in (("class_id", 36), ("document_id", 36), ("chunk_id", 64), ("name", 200)):
        if field in value and (
            not isinstance(value[field], str)
            or not value[field].strip()
            or len(value[field]) > limit
        ):
            raise ProofError("invalid_request", "Check the selected class, document, or name.")
    for field in ("file_path", "output_path"):
        if field in value:
            _path(value[field])
    if operation == "ask":
        question = value["question"]
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > MAX_QUESTION_CHARS
        ):
            raise ProofError("invalid_question", "Enter a question of 1 to 2,000 characters.")
        try:
            question.encode("utf-8")
        except UnicodeError as exc:
            raise ProofError("invalid_question", "Enter a readable question.") from exc
        if "pages" in value:
            pages = value["pages"]
            if (
                "document_id" not in value
                or not isinstance(pages, list)
                or not 1 <= len(pages) <= MAX_SELECTED_PAGES
                or any(type(page) is not int or not 1 <= page <= 500 for page in pages)
                or len(set(pages)) != len(pages)
            ):
                raise ProofError("invalid_pages", "Select one to five distinct document pages.")
    return value


def _import(library: Library, request: dict) -> dict:
    path = _path(request["file_path"])
    if path.suffix.lower() not in {".pdf", ".pptx"}:
        raise ProofError("unsupported", "Only PDF and PPTX documents are supported.")
    # Admit a regular source without hanging on a FIFO, and read it once with a
    # hard byte ceiling. The private parser snapshot and saved BLOB then contain
    # exactly these same bytes, even if the caller's staged file later changes.
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            information = os.fstat(source.fileno())
            if not stat.S_ISREG(information.st_mode) or information.st_size > MAX_FILE_BYTES:
                raise ProofError("invalid_file", "Choose a regular PDF/PPTX of at most 10 MiB.")
            original = source.read(MAX_FILE_BYTES + 1)
        if not 0 < len(original) <= MAX_FILE_BYTES:
            raise ProofError("invalid_file", "Choose a nonempty PDF/PPTX of at most 10 MiB.")
        # The parent owns this admitted staging directory and recursively reaps
        # it on cancellation/restart. A killed parser cannot orphan source bytes
        # in a separate global temporary directory whose finally never runs.
        with tempfile.TemporaryDirectory(prefix="gct-import-", dir=path.parent) as directory:
            snapshot = Path(directory) / path.name
            descriptor = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(original)
            document = _load_document({"file_path": str(snapshot)})
            return library.import_document(
                request["class_id"], path.name, original, document.units, document.page_count
            )
    except OSError as exc:
        raise ProofError(
            "file_unavailable", "The selected document could not be read safely."
        ) from exc


def _document(library: Library, class_id: str, document_id: str) -> dict:
    metadata = library.document(class_id, document_id)
    pages = library.pages(class_id, document_id)
    document = Document(
        metadata["filename"],
        metadata["page_count"],
        [ParsedUnit(page["text"], metadata["filename"], page["page_or_slide"]) for page in pages],
    )
    return {"document": metadata, "preview": inspect_document(document)}


def _selected_evidence(library: Library, request: dict) -> list[RetrievedChunk]:
    """Preserve all explicitly selected evidence, including its saved identities.

    An explicit page selection is a user's evidence choice, not a lexical search.
    Reject an oversized context rather than silently dropping selected passages.
    """
    metadata = library.document(request["class_id"], request["document_id"])
    pages = request["pages"]
    if any(page > metadata["page_count"] for page in pages):
        raise ProofError("invalid_pages", "Select existing pages in this document.")
    page_text = library.pages(request["class_id"], request["document_id"])
    if (
        sum(len(page["text"]) for page in page_text if page["page_or_slide"] in pages)
        > MAX_TEXT_CHARS
    ):
        raise ProofError(
            "context_too_large", "Selected text exceeds 40,000 characters. Choose fewer pages."
        )
    # All interpolated SQL is placeholder count, bounded to at most five. Saved
    # IDs remain the citation spine; re-chunking would create different IDs.
    rows = library.connection.execute(
        "SELECT c.id, c.text, d.filename, c.page_or_slide FROM chunks c "
        "JOIN documents d ON d.id = c.document_id WHERE d.class_id = ? AND d.id = ? "
        "AND c.page_or_slide IN (" + ",".join("?" for _ in pages) + ") "
        "ORDER BY c.page_or_slide, c.ordinal",
        [request["class_id"], request["document_id"], *pages],
    ).fetchall()
    chunks = [RetrievedChunk(row[0], row[1], row[2], row[3], 1.0) for row in rows]
    context, _ = _build_labeled_context(chunks)
    if len(context) > MAX_TEXT_CHARS:
        raise ProofError(
            "context_too_large", "Selected context exceeds 40,000 characters. Choose fewer pages."
        )
    return chunks


def _action(library: Library, request: dict, model_root: Path | None):
    operation = request["operation"]
    if operation == "list_classes":
        return library.list_classes()
    if operation == "create_class":
        return library.create_class(request["name"])
    if operation == "list_documents":
        return library.list_documents(request["class_id"])
    if operation == "import_document":
        return _import(library, request)
    if operation == "get_document":
        return _document(library, request["class_id"], request["document_id"])
    if operation == "delete_document":
        return library.delete_document(request["class_id"], request["document_id"])
    if operation == "delete_class":
        return library.delete_class(request["class_id"])
    if operation == "citation":
        return library.citation(request["class_id"], request["chunk_id"])
    if operation == "backup":
        return library.backup(_path(request["output_path"]))
    if operation in {"search_status", "prepare_search"}:
        from gct.local import embedding_index

        if operation == "search_status":
            return embedding_index.status(library, request["class_id"], model_root)
        return embedding_index.prepare(
            library, request["class_id"], model_root, deadline_seconds=210
        )
    raise ProofError("invalid_operation", "Choose an available library action.")


def run(
    source: TextIO,
    destination: TextIO,
    *,
    library_path: str | Path,
    model_root: str | Path | None = None,
) -> int:
    """Serve exactly one operation; complete answers keep the existing wire contract."""
    previous_dotenv = os.environ.get("PYTHON_DOTENV_DISABLED")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    old_logging = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        with (
            contextlib.redirect_stdout(_DiscardOutput()),
            contextlib.redirect_stderr(_DiscardOutput()),
        ):
            request = _request(_read_object(source))
            path = _path(str(library_path))
            models = _path(str(model_root)) if model_root is not None else None
            with Library(path) as library:
                if request["operation"] == "ask":
                    if "pages" in request:
                        chunks = _selected_evidence(library, request)
                    else:
                        from gct.local import embedding_index

                        chunks, search_status = embedding_index.retrieve(
                            library,
                            request["class_id"],
                            request["question"],
                            models,
                            document_id=request.get("document_id"),
                        )
                        if chunks is None:
                            chunks = library.retrieve(
                                request["class_id"],
                                request["question"],
                                document_id=request.get("document_id"),
                            )
                        _emit(destination, {"type": "search_status", "value": search_status})
                    # No database transaction remains open while a network reply
                    # is pending; generation receives text and source labels only.
                    result = answer(
                        request["question"].strip(),
                        chunks,
                        "local-device",
                        generator=StdioGeneration(source, destination),
                    )
                    _emit(destination, {"type": "result", "result": asdict(result)})
                else:
                    value = _action(library, request, models)
                    _emit(
                        destination,
                        {"type": "library", "operation": request["operation"], "value": value},
                    )
            return 0
    except (ProofError, LibraryError) as exc:
        _emit(destination, {"type": "error", "code": exc.code, "message": exc.message})
        return 1
    except Exception:
        _emit(
            destination,
            {
                "type": "error",
                "code": "internal",
                "message": "The local library action could not finish.",
            },
        )
        return 1
    finally:
        logging.disable(old_logging)
        if previous_dotenv is None:
            os.environ.pop("PYTHON_DOTENV_DISABLED", None)
        else:
            os.environ["PYTHON_DOTENV_DISABLED"] = previous_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, help="Absolute path to the local library.")
    parser.add_argument("--model-root", help="Absolute path to the bundled local model assets.")
    options = parser.parse_args()
    return run(sys.stdin, sys.stdout, library_path=options.library, model_root=options.model_root)


if __name__ == "__main__":
    raise SystemExit(main())
