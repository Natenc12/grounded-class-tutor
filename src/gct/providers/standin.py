"""Free stand-in models. They prove the plumbing; their answers are word matching, not reading."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from gct.config import EMBEDDING_DIM
from gct.providers.base import Message

_WORD_RE = re.compile(r"[a-z0-9]+")
_SOURCE_RE = re.compile(r"^\[S(?P<ordinal>\d+)\] \([^\n]*\)\n", re.M)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_QUESTION_MARKER = "\n\nQUESTION\n"
_FILLER = frozenset(
    "what which when where were does that this with from have about there their they them "
    "into your".split()
)
_MIN_WORD_LETTERS = 4
_MIN_SHARE_OF_QUESTION = 0.5
_MAX_CITED_SOURCES = 2


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _content_words(text: str) -> set[str]:
    return {w for w in _words(text) if len(w) >= _MIN_WORD_LETTERS and w not in _FILLER}


class StandInEmbeddings:
    model_id = "standin-hashed-words-v1"
    dim = EMBEDDING_DIM

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        for word in _words(text):
            digest = hashlib.sha256(word.encode()).digest()
            vector[int.from_bytes(digest[:8], "big") % self.dim] += 1.0
        length = math.sqrt(sum(value * value for value in vector))
        if length == 0.0:
            vector[0] = 1.0
            return vector
        return [value / length for value in vector]


class StandInGeneration:
    model_id = "standin-quoted-sentence-v1"

    def generate(self, messages: Sequence[Message]) -> str:
        context, _, question = messages[-1]["content"].rpartition(_QUESTION_MARKER)
        asked = _content_words(question)
        gaps = f"COVERAGE: gaps: {' '.join(question.split()) or 'the question'}"
        if not asked:
            return gaps

        ranked = sorted(
            (
                (len(asked & _content_words(text)) / len(asked), -ordinal, text)
                for ordinal, text in self._sources(context)
            ),
            reverse=True,
        )
        cited = [
            f"{self._best_sentence(text, asked)} [S{-negative_ordinal}]"
            for share, negative_ordinal, text in ranked[:_MAX_CITED_SOURCES]
            if share >= _MIN_SHARE_OF_QUESTION
        ]
        if not cited:
            return gaps
        return "\n".join([*cited, "COVERAGE: complete"])

    @staticmethod
    def _sources(context: str) -> list[tuple[int, str]]:
        headers = list(_SOURCE_RE.finditer(context))
        return [
            (
                int(header.group("ordinal")),
                context[header.end() : following.start() if following else len(context)].strip(),
            )
            for header, following in zip(headers, [*headers[1:], None], strict=True)
        ]

    @staticmethod
    def _best_sentence(text: str, asked: set[str]) -> str:
        sentences = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
        return max(sentences, key=lambda s: len(asked & _content_words(s)))
