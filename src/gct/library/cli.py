from __future__ import annotations

import argparse
import logging
import shlex
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from gct.grounder.answer import GrounderResult, GrounderState
from gct.library.ask import ask
from gct.library.importer import Imported, import_file
from gct.library.store import Library, LibraryClass, LibraryError
from gct.providers import active
from gct.providers.base import Generation, Message

PROG = "python -m gct.library"
INTERRUPTED = 130

STATE_HEADINGS = {
    GrounderState.GROUNDED: "answered from your files.",
    GrounderState.PARTIAL: "partly answered from your files; some of the question is not covered.",
    GrounderState.REFUSAL: "not answered, because your files for this class do not cover it.",
    GrounderState.INTEGRITY_FLAGGED: "the answer's sources could not be checked, so do not "
    "rely on it.",
    GrounderState.ERROR: "no answer.",
}


class _Stop(Exception):
    pass


def selected_generator() -> Generation:
    try:
        active.require_usable()
    except RuntimeError as exc:
        raise _Stop(str(exc)) from exc
    return active.build_generator()


def main(
    argv: Sequence[str],
    *,
    generator: Callable[[], Generation],
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    arguments = _parser().parse_args(list(argv))
    try:
        with _pdf_reader_quiet():
            return arguments.command(arguments, generator, out)
    except (_Stop, LibraryError) as exc:
        print(exc, file=err)
        return 1
    except KeyboardInterrupt:
        print("Stopped.", file=err)
        return INTERRUPTED


def _parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--library",
        required=True,
        type=_path,
        metavar="PATH",
        help="the library file, for example ~/tutor.sqlite3",
    )
    parser = argparse.ArgumentParser(
        prog=PROG, description="Your class tutor: classes, course files and cited answers."
    )
    commands = parser.add_subparsers(title="commands", required=True, metavar="COMMAND")

    add_class = commands.add_parser(
        "add-class",
        parents=[shared],
        help="make a class (and the library, if it does not exist yet)",
    )
    add_class.add_argument("name", metavar="CLASS")
    add_class.set_defaults(command=_add_class)

    listing = commands.add_parser("list", parents=[shared], help="list the classes and files")
    listing.set_defaults(command=_list)

    importing = commands.add_parser(
        "import", parents=[shared], help="import PDF or PowerPoint files into a class"
    )
    importing.add_argument("class_name", metavar="CLASS")
    importing.add_argument("files", nargs="+", type=_path, metavar="FILE")
    importing.set_defaults(command=_import)

    asking = commands.add_parser(
        "ask", parents=[shared], help="ask a class a question; put the question in quotes"
    )
    asking.add_argument("class_name", metavar="CLASS")
    asking.add_argument("question", metavar="QUESTION")
    asking.set_defaults(command=_ask)

    checking = commands.add_parser("check", parents=[shared], help="check the library is sound")
    checking.set_defaults(command=_check)
    return parser


def _path(text: str) -> Path:
    return Path(text).expanduser()


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
def _existing(path: Path) -> Iterator[Library]:
    if not path.exists():
        raise _Stop(
            f"There is no library at {path}. Check the path, or make a new library with:\n"
            f"  {_command('add-class', path, 'CLASS')}"
        )
    with Library(path) as library:
        yield library


def _command(name: str, path: Path, *rest: str) -> str:
    return shlex.join([*PROG.split(), name, "--library", str(path), *rest])


def _class(library: Library, name: str) -> LibraryClass:
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
            f"  {_command('add-class', library.path, name)}"
        )
    names = ", ".join(found.name for found in classes)
    raise _Stop(f"There is no class called {name!r} in this library. Its classes are: {names}.")


def _unit(filename: str) -> str:
    return "slide" if Path(filename).suffix.lower() == ".pptx" else "page"


def _units(filename: str, count: int) -> str:
    return f"{count:,} {_unit(filename)}{'' if count == 1 else 's'}"


def _add_class(arguments: argparse.Namespace, _: Callable[[], Generation], out: TextIO) -> int:
    path = arguments.library
    new = not path.exists()
    with Library(path) as library:
        made = library.create_class(arguments.name)
    if new:
        print(f"Made a new library at {path}.", file=out)
    print(f"Made the class {made.name}. Import files into it with:", file=out)
    print(f"  {_command('import', path, made.name, 'FILE')}", file=out)
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
        target = _class(library, arguments.class_name)
        for file in arguments.files:
            outcome = import_file(library, target.id, file)
            refused += not isinstance(outcome, Imported)
            print(outcome.message, file=out)
    return 1 if refused else 0


def _ask(arguments: argparse.Namespace, generator: Callable[[], Generation], out: TextIO) -> int:
    with _existing(arguments.library) as library:
        target = _class(library, arguments.class_name)
        model = _Watched(generator())
        result = ask(library, target.id, arguments.question, generator=model).result
    _print_answer(result, model, out)
    return 1 if result.state is GrounderState.ERROR else 0


def _print_answer(result: GrounderResult, model: _Watched, out: TextIO) -> None:
    print(f"{result.state.value}: {STATE_HEADINGS[result.state]}", file=out)
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
