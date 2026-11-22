from __future__ import annotations

import argparse
import logging
import os
import re
import shlex
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, TextIO

from gct.config import load_settings
from gct.contracts import AskResult
from gct.grounder.answer import GrounderState
from gct.library.ask import ask
from gct.library.importer import Imported, import_file
from gct.library.store import Library, LibraryClass, LibraryError
from gct.providers.active import CHOICES, CLAUDE, OPENAI, PROVIDERS_ENV, STANDIN
from gct.providers.base import Generation, Message
from gct.providers.claude import INSTALL_HINT, ClaudeGeneration, sdk_installed
from gct.providers.standin import StandInGeneration

PROG = "uv run tutor"
LIBRARY_ENV = "GCT_LIBRARY"
DEFAULT_LIBRARY = "~/tutor.sqlite3"
USAGE_MISTAKE = 2
INTERRUPTED = 130
LONGEST_GAP = 160

STATE_HEADINGS = {
    GrounderState.GROUNDED: "answered from your files.",
    GrounderState.PARTIAL: "partly answered from your files; some of the question is not covered.",
    GrounderState.REFUSAL: "not answered, because your files for this class do not cover it.",
    GrounderState.INTEGRITY_FLAGGED: "the answer's sources could not be checked, so do not "
    "rely on it.",
    GrounderState.ERROR: "no answer.",
}

PROVIDER_LINES = {
    CLAUDE: "Answers come from Claude on this Mac's login.",
    STANDIN: "Answers come from the free stand-in model (GCT_PROVIDERS=standin), which matches "
    "words and does not really read.",
    OPENAI: "Answers come from OpenAI, a paid service, using your API key (GCT_PROVIDERS=openai).",
}

EXAMPLES = {
    "": f'{PROG} ask Hydrology "What is base flow?"',
    "add-class": f'{PROG} add-class "Water Science"',
    "list": f"{PROG} list",
    "import": f"{PROG} import Hydrology ~/Downloads/notes.pdf",
    "ask": f'{PROG} ask Hydrology "What is base flow?"',
    "check": f"{PROG} check",
}

NEEDS = {
    "": "Say what to do: add-class, import, ask, list or check.",
    "add-class": "Say the name of the class to make.",
    "list": "The list command takes nothing after it.",
    "import": "Say which class, then the file or files to add.",
    "ask": "Say which class to ask, then the question.",
    "check": "The check command takes nothing after it.",
}

OVERVIEW = f"""\
Your class tutor: ask questions of your own course files and get answers with their sources.

  {PROG} add-class Hydrology                      make a class
  {PROG} import Hydrology ~/Downloads/notes.pdf   add a PDF or PowerPoint to it
  {PROG} ask Hydrology "What is base flow?"       ask it a question (keep the quotes)
  {PROG} list                                     see your classes and their files
  {PROG} check                                    check the library is sound

Your library is {DEFAULT_LIBRARY} unless you add --library PATH.
More about one command: {PROG} ask --help
"""


class _Stop(Exception):
    pass


class _Usage(Exception):
    def __init__(self, command: str, message: str) -> None:
        super().__init__(message)
        self.command = command


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _Usage(self.prog.removeprefix(PROG).strip(), message)


@dataclass(frozen=True)
class _Place:
    path: Path
    typed: bool


def provider() -> str:
    written = os.environ.get(PROVIDERS_ENV, "").strip()
    choice = written.casefold() or CLAUDE
    if choice not in CHOICES:
        raise _Stop(
            f"{PROVIDERS_ENV} is set to {written!r}, which the tutor does not recognise. "
            "To get answers from Claude on this Mac's login, remove the setting: "
            f"unset {PROVIDERS_ENV}. To try the tutor free with the stand-in model, which does "
            f"not really read, use {PROVIDERS_ENV}={STANDIN}."
        )
    return choice


def selected_generator() -> Generation:
    choice = provider()
    if choice == CLAUDE:
        if not sdk_installed():
            raise _Stop(INSTALL_HINT)
        return ClaudeGeneration()
    if choice == STANDIN:
        return StandInGeneration()
    if not load_settings().openai_api_key:
        raise _Stop(
            f"{PROVIDERS_ENV}={OPENAI} asks for OpenAI, a paid service, but no OpenAI API key is "
            f"set. To use Claude on this Mac's login instead, remove the setting: "
            f"unset {PROVIDERS_ENV}."
        )
    from gct.providers.openai_provider import OpenAIGeneration

    return OpenAIGeneration()


def run() -> int:
    return main(sys.argv[1:], generator=selected_generator)


def main(
    argv: Sequence[str],
    *,
    generator: Callable[[], Generation],
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    if not argv:
        print(OVERVIEW, file=out, end="")
        try:
            print(PROVIDER_LINES[provider()], file=out)
        except _Stop as exc:
            print(exc, file=out)
        return 0
    try:
        arguments = _parse(argv)
    except _Usage as mistake:
        print(_plain(mistake), file=err)
        print(f"For example: {EXAMPLES[mistake.command]}", file=err)
        return USAGE_MISTAKE
    arguments.library = _place(getattr(arguments, "library", None))
    print(f"Library: {arguments.library.path}", file=out, flush=True)
    try:
        with _pdf_reader_quiet():
            return arguments.command(arguments, generator, out)
    except (_Stop, LibraryError) as exc:
        print(exc, file=err)
        return 1
    except KeyboardInterrupt:
        print("Stopped.", file=err)
        return INTERRUPTED


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    arguments, extra = _parser().parse_known_args(list(argv))
    if extra:
        raise _Usage(arguments.command_name, "unrecognized arguments: " + " ".join(extra))
    return arguments


def _plain(mistake: _Usage) -> str:
    message = str(mistake)
    if "invalid choice" in message:
        wrong = re.search(r"invalid choice: '([^']*)'", message)
        named = f" called {wrong.group(1)!r}" if wrong else ""
        return (
            f"There is no command{named}. The commands are add-class, import, ask, list and "
            f"check; run {PROG} on its own to see what each one does."
        )
    if message.startswith("the following arguments are required"):
        return NEEDS[mistake.command]
    if "expected one argument" in message:
        return "Put the library's path after --library."
    if message.startswith("unrecognized arguments:"):
        extra = message.removeprefix("unrecognized arguments:").split()
        options = [word for word in extra if word.startswith("-")]
        if options:
            return f"There is no option {options[0]}; the only option is --library PATH."
        if mistake.command == "add-class":
            return "A class name with spaces needs quotes around it."
        if mistake.command in ("list", "check"):
            return NEEDS[mistake.command]
        return f"These words were not expected: {' '.join(extra)}."
    return f"{message[:1].upper()}{message[1:]}."


def _place(typed: str | None) -> _Place:
    if typed is not None:
        return _Place(Path(typed).expanduser(), typed=True)
    chosen = os.environ.get(LIBRARY_ENV, "").strip() or DEFAULT_LIBRARY
    return _Place(Path(chosen).expanduser(), typed=False)


def _parser() -> argparse.ArgumentParser:
    library = {
        "metavar": "PATH",
        "default": argparse.SUPPRESS,
        "help": f"use this library file instead of {DEFAULT_LIBRARY}",
    }
    shared = _Parser(add_help=False)
    shared.add_argument("--library", **library)
    parser = _Parser(
        prog=PROG,
        description="Your class tutor: ask questions of your own course files.",
        epilog=f"Example: {EXAMPLES['']}",
    )
    parser.add_argument("--library", **library)
    parser.set_defaults(command_name="")
    commands = parser.add_subparsers(title="commands", required=True, metavar="COMMAND")

    def command(name: str, summary: str, run: Callable[..., int]) -> argparse.ArgumentParser:
        added = commands.add_parser(
            name,
            parents=[shared],
            help=summary,
            description=f"{summary[0].upper()}{summary[1:]}.",
            epilog=f"Example: {EXAMPLES[name]}",
        )
        added.set_defaults(command_name=name, command=run)
        return added

    add_class = command("add-class", "make a class (and the library, the first time)", _add_class)
    add_class.add_argument(
        "name", metavar="CLASS", help="the class name, in quotes if it has spaces"
    )

    command("list", "list your classes and their files", _list)

    importing = command("import", "add PDF or PowerPoint files to a class", _import)
    importing.add_argument("class_name", metavar="CLASS", help="the class to add them to")
    importing.add_argument(
        "files",
        nargs="+",
        type=lambda text: Path(text).expanduser(),
        metavar="FILE",
        help="one or more files; you can drag a file from Finder onto Terminal",
    )

    asking = command("ask", "ask a class a question", _ask)
    asking.add_argument("class_name", metavar="CLASS", help="the class to ask")
    asking.add_argument(
        "question", nargs="+", metavar="QUESTION", help="the question, best kept in quotes"
    )

    command("check", "check the library is sound", _check)
    return parser


@contextmanager
def _pdf_reader_quiet() -> Iterator[None]:
    reader_log = logging.getLogger("pypdf")
    level = reader_log.level
    reader_log.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        reader_log.setLevel(level)


@contextmanager
def _existing(place: _Place) -> Iterator[Library]:
    if not place.path.exists():
        raise _Stop(
            f"There is no library at {place.path}. Check the path, or make a new library with:\n"
            f"  {_command('add-class', place, 'CLASS')}"
        )
    with Library(place.path) as library:
        yield library


def _command(name: str, place: _Place, *rest: str) -> str:
    library = ["--library", str(place.path)] if place.typed else []
    return shlex.join([*PROG.split(), name, *library, *rest])


def _class(library: Library, place: _Place, name: str) -> LibraryClass:
    classes = library.list_classes()
    wanted = name.strip()
    for matches in (
        [found for found in classes if found.name == wanted],
        [found for found in classes if found.name.casefold() == wanted.casefold()],
    ):
        if len(matches) == 1:
            return matches[0]
    if not classes:
        raise _Stop(
            f"There is no class called {name!r}; this library has no classes yet. Make one with:\n"
            f"  {_command('add-class', place, name)}"
        )
    names = ", ".join(found.name for found in classes)
    message = f"There is no class called {name!r} in this library. Its classes are: {names}."
    prefix = f"{wanted} ".casefold()
    spaced = [found for found in classes if found.name.casefold().startswith(prefix)]
    if spaced:
        message += f'\nA class name with spaces needs quotes around it, like "{spaced[0].name}".'
    raise _Stop(message)


def _unit(filename: str) -> str:
    return "slide" if Path(filename).suffix.lower() == ".pptx" else "page"


def _units(filename: str, count: int) -> str:
    return f"{count:,} {_unit(filename)}{'' if count == 1 else 's'}"


def _add_class(arguments: argparse.Namespace, _: Callable[[], Generation], out: TextIO) -> int:
    place = arguments.library
    new = not place.path.exists()
    with Library(place.path) as library:
        wanted = arguments.name.strip()
        for found in library.list_classes():
            if found.name != wanted and found.name.casefold() == wanted.casefold():
                raise _Stop(
                    f"There is already a class called {found.name!r}. Names that differ only in "
                    "capital letters are too easy to mix up; choose another name."
                )
        made = library.create_class(arguments.name)
    if new:
        print("This is a new library; it was made just now.", file=out)
    print(f"Made the class {made.name}. Add files to it with:", file=out)
    print(f"  {_command('import', place, made.name, 'FILE')}", file=out)
    return 0


def _list(arguments: argparse.Namespace, _: Callable[[], Generation], out: TextIO) -> int:
    with _existing(arguments.library) as library:
        classes = [(found, library.list_documents(found.id)) for found in library.list_classes()]
    if not classes:
        print("This library has no classes yet. Make one with:", file=out)
        print(f"  {_command('add-class', arguments.library, 'CLASS')}", file=out)
    for found, documents in classes:
        print(found.name, file=out)
        for document in documents:
            print(
                f"  {document.filename} ({_units(document.filename, document.page_count)})",
                file=out,
            )
        if not documents:
            print("  (no files yet)", file=out)
    return 0


def _import(arguments: argparse.Namespace, _: Callable[[], Generation], out: TextIO) -> int:
    refused = 0
    with _existing(arguments.library) as library:
        target = _class(library, arguments.library, arguments.class_name)
        for file in arguments.files:
            if file.is_dir():
                message = (
                    f"{file} is a folder, not a file. Import the PDF or PowerPoint files inside "
                    "it instead."
                )
            elif not file.exists():
                message = (
                    f"There is no file at {file}. Check the file's name and folder, then try "
                    "again; you can drag the file from Finder onto Terminal to type its path."
                )
            else:
                outcome = import_file(library, target.id, file)
                message = outcome.message
                refused -= isinstance(outcome, Imported)
            refused += 1
            print(message, file=out)
    if refused < len(arguments.files):
        print("Ask the class a question with:", file=out)
        print(f"  {_command('ask', arguments.library, target.name, 'QUESTION')}", file=out)
    return 1 if refused else 0


def _ask(arguments: argparse.Namespace, generator: Callable[[], Generation], out: TextIO) -> int:
    question = " ".join(arguments.question).strip()
    with _existing(arguments.library) as library:
        target = _class(library, arguments.library, arguments.class_name)
        if not re.search(r"[^\W_]", question):
            raise _Stop(
                "Type a question after the class name, for example:\n"
                f"  {_command('ask', arguments.library, target.name, 'What is base flow?')}"
            )
        if not library.list_documents(target.id):
            raise _Stop(
                f"The class {target.name} has no files yet, so there is nothing to answer from. "
                f"Add one with:\n  {_command('import', arguments.library, target.name, 'FILE')}"
            )
        model = _Watched(generator())
        if model.paid:
            print(f"Using {model.answered_by}.", file=out)
        asked = ask(library, target.id, question, generator=model)
    _print_answer(asked, model, out)
    return 1 if asked.result.state is GrounderState.ERROR else 0


def _print_answer(asked: AskResult, model: _Watched, out: TextIO) -> None:
    result = asked.result
    print(f"\n{result.state.value}: {STATE_HEADINGS[result.state]}", file=out)
    if result.state is GrounderState.ERROR:
        failure = model.failure or (result.error and result.error.message)
        print(f"\n{failure}", file=out)
        return
    if not asked.retrieved:
        print("\nNothing in your files for this class matched this question.", file=out)
        return
    if result.answer_prose:
        print(f"\n{result.answer_prose.strip()}", file=out)
    if result.integrity.reasons:
        print("\nWhy it could not be checked:", file=out)
        for reason in result.integrity.reasons:
            print(f"  {reason}", file=out)
    sources = {citation.label: citation for citation in result.citations}
    if sources:
        print("\nSources:", file=out)
        for label, citation in sources.items():
            print(
                f"  [{label}] {citation.file}, {_unit(citation.file)} {citation.page_or_slide}",
                file=out,
            )
    if result.coverage.gaps:
        print("\nNot covered by your files:", file=out)
        for gap in result.coverage.gaps:
            print(f"  {_shortened(gap)}", file=out)
    print(f"\nAnswered by {model.answered_by}.", file=out)


def _shortened(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= LONGEST_GAP:
        return text
    return f"{text[: LONGEST_GAP - 1].rstrip()}…"


def _check(arguments: argparse.Namespace, _: Callable[[], Generation], out: TextIO) -> int:
    with _existing(arguments.library) as library:
        problems = library.check()
    if not problems:
        print("The library is in good order.", file=out)
        return 0
    print("The library has problems:", file=out)
    for problem in problems:
        print(f"  {problem}", file=out)
    return 1


class _Watched:
    def __init__(self, inner: Generation) -> None:
        self._inner = inner
        self.failure: Exception | None = None
        self.paid = type(inner).__module__ == "gct.providers.openai_provider"

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    @property
    def answered_by(self) -> str:
        if self.paid:
            return f"OpenAI, using your paid API key ({self.model_id})"
        if isinstance(self._inner, ClaudeGeneration):
            return f"Claude on this Mac's login ({self.model_id})"
        if isinstance(self._inner, StandInGeneration):
            return f"the free stand-in model ({self.model_id}), which does not really read"
        return self.model_id

    def generate(self, messages: Sequence[Message]) -> str:
        try:
            return self._inner.generate(messages)
        except Exception as exc:
            self.failure = exc
            raise
