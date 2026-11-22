from __future__ import annotations

import argparse
import logging
import os
import shlex
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from gct.grounder.answer import GrounderResult, GrounderState
from gct.library.ask import ask
from gct.library.importer import Imported, import_file
from gct.library.store import Library, LibraryClass, LibraryError
from gct.providers import active
from gct.providers.base import Generation, Message
from gct.providers.claude import INSTALL_HINT, ClaudeGeneration, sdk_installed

PROG = "uv run tutor"
LIBRARY_ENV = "GCT_LIBRARY"
DEFAULT_LIBRARY = "~/tutor.sqlite3"
INTERRUPTED = 130

STATE_HEADINGS = {
    GrounderState.GROUNDED: "answered from your files.",
    GrounderState.PARTIAL: "partly answered from your files; some of the question is not covered.",
    GrounderState.REFUSAL: "not answered, because your files for this class do not cover it.",
    GrounderState.INTEGRITY_FLAGGED: "the answer's sources could not be checked, so do not "
    "rely on it.",
    GrounderState.ERROR: "no answer.",
}

OVERVIEW = f"""\
Your class tutor: ask questions of your own course files and get answers with their sources.

  {PROG} add-class Hydrology                      make a class
  {PROG} import Hydrology ~/Downloads/notes.pdf   add a PDF or PowerPoint to it
  {PROG} ask Hydrology "What is base flow?"       ask it a question (keep the quotes)
  {PROG} list                                     see your classes and their files
  {PROG} check                                    check the library is sound

Answers come from Claude on this Mac's login. Your library is {DEFAULT_LIBRARY} unless you add
--library PATH. More about one command: {PROG} ask --help
"""


class _Stop(Exception):
    pass


@dataclass(frozen=True)
class _Place:
    path: Path
    typed: bool


def selected_generator() -> Generation:
    if not os.environ.get(active.PROVIDERS_ENV, "").strip():
        if not sdk_installed():
            raise _Stop(INSTALL_HINT)
        return ClaudeGeneration()
    try:
        active.require_usable()
    except RuntimeError as exc:
        raise _Stop(str(exc)) from exc
    return active.build_generator()


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
        print(OVERVIEW, end="", file=out)
        return 0
    arguments = _parser().parse_args(list(argv))
    arguments.library = _place(arguments.library)
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


def _place(typed: str | None) -> _Place:
    if typed is not None:
        return _Place(Path(typed).expanduser(), typed=True)
    chosen = os.environ.get(LIBRARY_ENV, "").strip() or DEFAULT_LIBRARY
    return _Place(Path(chosen).expanduser(), typed=False)


def _parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--library",
        metavar="PATH",
        help=f"use this library file instead of {DEFAULT_LIBRARY}",
    )
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Your class tutor: ask questions of your own course files.",
        epilog=f'Example: {PROG} ask Hydrology "What is base flow?"',
    )
    commands = parser.add_subparsers(title="commands", required=True, metavar="COMMAND")

    def command(name: str, summary: str, example: str) -> argparse.ArgumentParser:
        return commands.add_parser(
            name,
            parents=[shared],
            help=summary,
            description=f"{summary[0].upper()}{summary[1:]}.",
            epilog=f"Example: {PROG} {name} {example}".rstrip(),
        )

    add_class = command("add-class", "make a class (and the library, the first time)", "Hydrology")
    add_class.add_argument(
        "name", metavar="CLASS", help="the class name, in quotes if it has spaces"
    )
    add_class.set_defaults(command=_add_class)

    command("list", "list your classes and their files", "").set_defaults(command=_list)

    importing = command(
        "import", "add PDF or PowerPoint files to a class", "Hydrology ~/Downloads/notes.pdf"
    )
    importing.add_argument("class_name", metavar="CLASS", help="the class to add them to")
    importing.add_argument(
        "files",
        nargs="+",
        type=lambda text: Path(text).expanduser(),
        metavar="FILE",
        help="one or more files; you can drag a file from Finder onto Terminal",
    )
    importing.set_defaults(command=_import)

    asking = command("ask", "ask a class a question", 'Hydrology "What is base flow?"')
    asking.add_argument("class_name", metavar="CLASS", help="the class to ask")
    asking.add_argument("question", metavar="QUESTION", help="the question, in quotes")
    asking.set_defaults(command=_ask)

    command("check", "check the library is sound", "").set_defaults(command=_check)
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
    raise _Stop(f"There is no class called {name!r} in this library. Its classes are: {names}.")


def _unit(filename: str) -> str:
    return "slide" if Path(filename).suffix.lower() == ".pptx" else "page"


def _units(filename: str, count: int) -> str:
    return f"{count:,} {_unit(filename)}{'' if count == 1 else 's'}"


def _add_class(arguments: argparse.Namespace, _: Callable[[], Generation], out: TextIO) -> int:
    place = arguments.library
    new = not place.path.exists()
    with Library(place.path) as library:
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
            outcome = import_file(library, target.id, file)
            refused += not isinstance(outcome, Imported)
            print(outcome.message, file=out)
    return 1 if refused else 0


def _ask(arguments: argparse.Namespace, generator: Callable[[], Generation], out: TextIO) -> int:
    with _existing(arguments.library) as library:
        target = _class(library, arguments.library, arguments.class_name)
        model = _Watched(generator())
        result = ask(library, target.id, arguments.question, generator=model).result
    _print_answer(result, model, out)
    return 1 if result.state is GrounderState.ERROR else 0


def _print_answer(result: GrounderResult, model: _Watched, out: TextIO) -> None:
    print(f"\n{result.state.value}: {STATE_HEADINGS[result.state]}", file=out)
    if result.state is GrounderState.ERROR:
        failure = model.failure or (result.error and result.error.message)
        print(f"\n{failure}", file=out)
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
            print(f"  {gap}", file=out)
    print(f"\nAnswered by: {model.model_id}", file=out)


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

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    def generate(self, messages: Sequence[Message]) -> str:
        try:
            return self._inner.generate(messages)
        except Exception as exc:
            self.failure = exc
            raise
