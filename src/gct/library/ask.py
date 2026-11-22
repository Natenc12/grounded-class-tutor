from __future__ import annotations

from gct.contracts import AskResult
from gct.grounder.answer import (
    Coverage,
    GrounderError,
    GrounderResult,
    GrounderState,
    Integrity,
    answer,
)
from gct.library import Library, LibraryError, UnknownClassError, UnknownDocumentError
from gct.providers.base import Generation
from gct.search import DEFAULT_K, search

ERROR_KIND_UNKNOWN_CLASS = "unknown_class"
ERROR_KIND_UNKNOWN_DOCUMENT = "unknown_document"
ERROR_KIND_SEARCH_FAILED = "search_failed"

_OWNER = "library"


def ask(
    library: Library,
    class_id: str,
    question: str,
    *,
    generator: Generation,
    document_id: str | None = None,
    k: int = DEFAULT_K,
) -> AskResult:
    try:
        retrieved = search(library, class_id, question, k=k, document_id=document_id)
    except LibraryError as exc:
        return AskResult(result=_search_error(exc), retrieved=[], retrieval_ran=False)
    return AskResult(
        result=answer(question, retrieved, _OWNER, generator=generator), retrieved=retrieved
    )


def _search_error(exc: LibraryError) -> GrounderResult:
    if isinstance(exc, UnknownClassError):
        kind = ERROR_KIND_UNKNOWN_CLASS
    elif isinstance(exc, UnknownDocumentError):
        kind = ERROR_KIND_UNKNOWN_DOCUMENT
    else:
        kind = ERROR_KIND_SEARCH_FAILED
    return GrounderResult(
        state=GrounderState.ERROR,
        answer_prose=None,
        citations=[],
        coverage=Coverage(complete=False, gaps=[]),
        integrity=Integrity(ok=True, reasons=[]),
        error=GrounderError(kind=kind, message=str(exc)),
    )
