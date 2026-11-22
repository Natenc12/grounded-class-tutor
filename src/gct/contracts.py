from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gct.grounder.answer import GrounderResult


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    text: str
    file: str
    page_or_slide: int
    score: float


@dataclass(frozen=True)
class AskResult:
    result: GrounderResult
    retrieved: list[RetrievedChunk]
    retrieval_ran: bool = True
