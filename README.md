# Grounded Class Tutor

A study tutor that answers from **your own course materials** — every answer is either cited to
the exact slide/page it came from, or an honest *"that's not in your materials."* Trust is the
product. See [`design/START-HERE.md`](design/START-HERE.md) for the full picture.

## Who this is for

This is a personal project, built for the owner's own use on his own Mac and his own Claude
subscription. It is not a service, and it does not offer Claude to anyone else.

The desktop app now being built is meant to get its answers from Claude through the Claude Agent SDK,
using the Claude login already on the machine. It has no sign-in screen of its own and needs no API key. (Until
the move is done, the hosted code described under Local setup still uses an OpenAI key.)

The code is public, but if you clone it, you run it on your own responsibility, with your own
account. This is a statement of intended use, not legal advice.

## Status

**Slice 0 — Foundation: complete** (schema + swappable provider interfaces, smoke-verified).

**Slice 1 — Tracer bullet: complete** — the grounding loop end-to-end (parse → chunk → embed →
index → retrieve → ground → cited answer / refusal), driven by a script, proven on real course
materials. See [`eval/FINDINGS.md`](eval/FINDINGS.md) for what the live runs showed.

**Spike Pass 1 — validation: complete.** Chunking + generation run on the tracer; the verdict, and
the bars it does not clear, are [ADR 0026](design/decisions/0026-spike-pass-1-verdict.md).

**Slice 2 — Real write path: complete** — upload becomes a real job: queued → processing →
ready/failed, at-least-once, crash-safe with one worker, with no partially-indexed file ever visible.

**Slice 3 — API adapter: complete** — a thin HTTP layer over the core: create a class, upload, poll
status, ask. No business logic in the adapter; the whole loop is driven over HTTP by
`scripts/http_smoke.py`.

**Now: moving to a local desktop app.** The hosted web app above is being replaced by a desktop app
on one Mac, step by step; [#216](https://github.com/Natenc12/grounded-class-tutor/issues/216) tracks
the move. The web client only got as far as a typed API client and a placeholder page.

[The ready frontier](https://github.com/Natenc12/grounded-class-tutor/issues?q=is%3Aopen+label%3Aready)
tracks what's ready to pick up — the board is the single writer for that, so this file doesn't
restate it.

See [`design/roadmap.md`](design/roadmap.md) for the full build sequence.

## Local setup

Prereqs: Python 3.10+, [uv](https://docs.astral.sh/uv/), and Postgres 17 with pgvector.

```sh
# 1. Postgres + pgvector (Homebrew). pgvector is a standalone bottle that drops the
#    `vector` extension into Postgres's extension dir — install it alongside postgresql@17.
brew install postgresql@17 pgvector
brew services start postgresql@17
createdb grounded_class_tutor
# postgresql@17 is keg-only; to put psql/createdb on your PATH add this to your shell profile:
#   export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"

# 2. Config
cp .env.example .env        # then edit .env: set OPENAI_API_KEY

# 3. Python deps  (--extra dev, or pytest/ruff are UNINSTALLED — they're an optional extra)
uv sync --extra dev

# 4. Create the schema, then prove the foundation is wired
uv run python scripts/migrate.py
uv run python scripts/smoke_slice0.py   # expect: "PASS — foundation is wired."
uv run python scripts/ask_smoke.py      # Slice 1 exit gate — SPENDS MONEY (real models)
```
