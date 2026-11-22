# Grounded Class Tutor

A study tutor that answers from **your own course materials** — every answer is either cited to
the exact slide/page it came from, or an honest *"that's not in your materials."* Trust is the
product. [`design/START-HERE.md`](design/START-HERE.md) explains the idea and the words used; it
was written for the hosted web app, so read the note at its top first.

## Who this is for

This is a personal project, built for the owner's own use on his own Mac and his own Claude
subscription. It is not a service, and it does not offer Claude to anyone else.

The tutor gets its answers from Claude through the Claude Agent SDK, using the Claude login
already on the machine. It has no sign-in screen of its own and needs no API key. Today it runs
as a Terminal command (`uv run tutor`, below); a desktop window is planned
([#216](https://github.com/Natenc12/grounded-class-tutor/issues/216)).

The code is public, but if you clone it, you run it on your own responsibility, with your own
account. This is a statement of intended use, not legal advice.

## Status

**Now: a personal app on one Mac, built in stages.**
[#216](https://github.com/Natenc12/grounded-class-tutor/issues/216) tracks the move from the hosted
web app to a desktop app. Stage 1 is merged: one SQLite library file holds your classes and their
files, PDF and PowerPoint files are imported into it, search is keyword search, and answers come
from Claude on the Mac's login, all through the `tutor` command (see Local setup). Stage 2 has
removed the hosted web app: the web client, the HTTP API, the job queue and worker, Postgres and
the OpenAI provider are gone. Search with a free local model and the desktop window are planned;
#216 lists the stages.

**Before the move: the hosted web app (history).** These slices were built for the hosted app.
Stage 2 removed their web, queue and database code; the grounding core they proved (the Grounder,
the file reader and chunker, and the eval scoring) is still in use.

- **Slice 0 — Foundation: complete** (schema + swappable provider interfaces, smoke-verified).
- **Slice 1 — Tracer bullet: complete** — the grounding loop end-to-end (parse → chunk → embed →
  index → retrieve → ground → cited answer / refusal), driven by a script, proven on real course
  materials. See [`eval/FINDINGS.md`](eval/FINDINGS.md) for what the live runs showed.
- **Spike Pass 1 — validation: complete.** Chunking + generation run on the tracer; the verdict, and
  the bars it does not clear, are [ADR 0026](design/decisions/0026-spike-pass-1-verdict.md).
- **Slice 2 — Real write path: complete** — upload becomes a real job: queued → processing →
  ready/failed, at-least-once, crash-safe with one worker, with no partially-indexed file ever
  visible.
- **Slice 3 — API adapter: complete** — a thin HTTP layer over the core: create a class, upload,
  poll status, ask.
- **Slice 4 (client): not finished.** The React client only got as far as a typed API client and a
  placeholder page.

[The ready frontier](https://github.com/Natenc12/grounded-class-tutor/issues?q=is%3Aopen+label%3Aready)
tracks what's ready to pick up — the board is the single writer for that, so this file doesn't
restate it.

#216 has the live build sequence. [`design/roadmap.md`](design/roadmap.md) is the original plan for
the hosted app, kept as history.

## Local setup

Prereqs: Python 3.14 and [uv](https://docs.astral.sh/uv/). For real answers, a Claude login on this
Mac.

```sh
# 1. Python deps. --extra dev brings the Claude Agent SDK, pytest and ruff; a bare
#    `uv sync` leaves them out, and uninstalls them if they are there.
uv sync --extra dev

# 2. Make a class, add a course file to it, and ask it a question (keep the quotes).
uv run tutor add-class Hydrology
uv run tutor import Hydrology ~/Downloads/notes.pdf
uv run tutor ask Hydrology "What is base flow?"

# 3. See your classes and their files, and check the library is sound.
uv run tutor list
uv run tutor check
```

- `uv run tutor` on its own lists the commands; `uv run tutor ask --help` explains one.
- The library is the file `~/tutor.sqlite3`. To use another file, add `--library PATH` to a
  command, or set `GCT_LIBRARY`.
- Answers come from Claude on this Mac's login, and each question uses your Claude plan's limits.
  If the login is missing or has expired, the tutor stops and says how to sign in.
  `GCT_CLAUDE_MODEL` picks the Claude model (`sonnet` if unset).
- `GCT_PROVIDERS=standin` swaps in a free stand-in model that matches words and does not really
  read. It is for trying the commands and for tests, not for study. Unset, the tutor uses Claude.
- Settings come only from the shell environment; nothing reads a `.env` file.

The checks the merge check runs, all free (no model is called):

```sh
uv run ruff check
uv run ruff format --check .
uv run python tools/drive.py      # the whole tutor on the stand-in model and a throwaway library
uv run pytest -m "not live"       # `live` tests call Claude on your login; run them only by hand
```
