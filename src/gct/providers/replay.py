"""Save a model's replies once, keyed on exactly what was sent, and play them back with no model.

A reply is found again only when the model, its settings and every message are byte-identical to
the recording, so a change to any of them is a replay miss rather than a stale answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from gct.providers.base import Generation, Message

FORMAT = 1
NO_QUESTION = "no recording for this question"

_SOURCE_LINE = re.compile(r"^\[S\d+\] \((?P<where>[^\n]*)\)$", re.M)


class ReplayMiss(Exception):
    pass


class RecordingFileError(ValueError):
    pass


@dataclass(frozen=True)
class Setup:
    model_id: str
    settings: str


def setup_of(model: Generation) -> Setup:
    from gct.providers.claude import ClaudeGeneration

    settings = _claude_settings() if isinstance(model, ClaudeGeneration) else {}
    return Setup(model_id=model.model_id, settings=_digest(settings))


def _claude_settings() -> dict[str, Any]:
    from gct.providers.claude import build_options

    sdk = SimpleNamespace(ClaudeAgentOptions=lambda **options: options)
    return build_options(sdk, model="<model>", system="<system>", folder="<folder>")


def _digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Call:
    sent: str
    sources: tuple[str, ...]
    reply: str


def _call(setup: Setup, messages: Sequence[Message], reply: str = "") -> Call:
    sent = _digest(
        {
            "model": setup.model_id,
            "settings": setup.settings,
            "messages": [dict(message) for message in messages],
        }
    )
    sources = tuple(
        match.group("where")
        for message in messages
        if message["role"] == "user"
        for match in _SOURCE_LINE.finditer(message["content"])
    )
    return Call(sent=sent, sources=sources, reply=reply)


@dataclass(frozen=True)
class Recording:
    setup: Setup
    questions: Mapping[str, tuple[Call, ...]]

    def write(self, path: Path) -> None:
        body = {
            "format": FORMAT,
            "model": self.setup.model_id,
            "settings": self.setup.settings,
            "questions": {
                question_id: [
                    {"sent": c.sent, "sources": list(c.sources), "reply": c.reply.split("\n")}
                    for c in calls
                ]
                for question_id, calls in sorted(self.questions.items())
            },
        }
        text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
        draft = path.with_name(path.name + ".writing")
        draft.write_text(text, encoding="utf-8")
        os.replace(draft, path)

    @classmethod
    def read(cls, path: Path) -> Recording:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            if body["format"] != FORMAT:
                raise RecordingFileError(f"{path}: format {body['format']!r}, expected {FORMAT}")
            return cls(
                setup=Setup(model_id=_text(body["model"]), settings=_text(body["settings"])),
                questions={
                    _text(question_id): tuple(
                        Call(
                            sent=_text(c["sent"]),
                            sources=_lines(c["sources"]),
                            reply="\n".join(_lines(c["reply"])),
                        )
                        for c in calls
                    )
                    for question_id, calls in body["questions"].items()
                },
            )
        except (KeyError, TypeError, AttributeError, ValueError) as err:
            if isinstance(err, RecordingFileError):
                raise
            raise RecordingFileError(f"{path} is not a readable recording: {err!r}") from err


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected text, found {value!r}")
    return value


def _lines(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"expected a list of lines, found {value!r}")
    return tuple(_text(line) for line in value)


class Replayer:
    def __init__(self, recording: Recording, setup: Setup) -> None:
        self._recording = recording
        self._setup = setup

    @contextmanager
    def question(self, question_id: str) -> Iterator[Generation]:
        recorded = self._recording.questions.get(question_id)
        model = _Replaying(self._setup, self._recording.setup, recorded)
        yield model
        if model.missed:
            return
        if recorded is None:
            raise ReplayMiss(NO_QUESTION)
        unused = len(recorded) - model.used
        if unused:
            raise ReplayMiss(
                f"no recording matches: {unused} recorded repl{'y' if unused == 1 else 'ies'} "
                "went unasked, so the app now asks the model fewer times than when recorded"
            )


@dataclass
class _Replaying:
    setup: Setup
    recorded_setup: Setup
    recorded: tuple[Call, ...] | None
    used: int = 0
    missed: bool = False

    @property
    def model_id(self) -> str:
        return self.setup.model_id

    def generate(self, messages: Sequence[Message]) -> str:
        try:
            return self._replay(messages)
        except ReplayMiss:
            self.missed = True
            raise

    def _replay(self, messages: Sequence[Message]) -> str:
        if self.recorded is None:
            raise ReplayMiss(NO_QUESTION)
        now = _call(self.setup, messages)
        if self.used >= len(self.recorded):
            raise ReplayMiss(
                f"no recording for call {self.used + 1}: only {len(self.recorded)} recorded"
            )
        then = self.recorded[self.used]
        if now.sent != then.sent:
            raise ReplayMiss(f"no recording matches what would be sent now: {self._why(now, then)}")
        self.used += 1
        return then.reply

    def _why(self, now: Call, then: Call) -> str:
        if self.setup.model_id != self.recorded_setup.model_id:
            return (
                f"the model changed (recorded {self.recorded_setup.model_id}, "
                f"now {self.setup.model_id})"
            )
        if self.setup.settings != self.recorded_setup.settings:
            return "the model's settings changed"
        if now.sources != then.sources:
            return f"the passages changed (recorded {list(then.sources)}, now {list(now.sources)})"
        return "the instructions, the question or a passage's text changed"


@dataclass
class Recorder:
    model: Generation
    resume: Recording | None = None
    setup: Setup = field(init=False)
    failures: dict[str, Exception] = field(init=False, default_factory=dict)
    _questions: dict[str, tuple[Call, ...]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.setup = setup_of(self.model)
        if self.resume is not None:
            self._questions.update(self.resume.questions)

    @property
    def recording(self) -> Recording:
        return Recording(setup=self.setup, questions=dict(self._questions))

    def already(self, question_id: str) -> bool:
        return self.resume is not None and question_id in self.resume.questions

    @contextmanager
    def question(self, question_id: str) -> Iterator[Generation]:
        if self.resume is not None and question_id in self.resume.questions:
            with Replayer(self.resume, self.setup).question(question_id) as model:
                yield model
            return
        model = _Recording(self.model, self.setup)
        yield model
        if model.failure is None:
            self._questions[question_id] = tuple(model.calls)
        else:
            self.failures[question_id] = model.failure


@dataclass
class _Recording:
    inner: Generation
    setup: Setup
    calls: list[Call] = field(default_factory=list)
    failure: Exception | None = None

    @property
    def model_id(self) -> str:
        return self.inner.model_id

    def generate(self, messages: Sequence[Message]) -> str:
        try:
            reply = self.inner.generate(messages)
        except Exception as err:
            self.failure = self.failure or err
            raise
        self.calls.append(_call(self.setup, messages, reply))
        return reply
