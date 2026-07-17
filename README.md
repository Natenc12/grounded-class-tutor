# Grounded Class Tutor

A study tutor that answers from **your own course materials** — every answer is either cited to
the exact slide/page it came from, or an honest *"that's not in your materials."* Trust is the
product. See [`design/START-HERE.md`](design/START-HERE.md) for the full picture.

## Status

**Slice 0 — Foundation: complete** (schema + swappable provider interfaces, smoke-verified).

**Slice 1 — Tracer bullet: in progress** — the grounding loop end-to-end (parse → chunk → embed →
index → retrieve → ground → cited answer / refusal), driven by a script. Tracked on
[epic #9](https://github.com/Natenc12/grounded-class-tutor/issues/9); parse · chunk · embed · eval-suite
are merged, the ingest pipeline and format-coverage spikes are the current ready-frontier.

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

# 3. Python deps
uv sync

# 4. Create the schema, then prove the foundation is wired
uv run python scripts/migrate.py
uv run python scripts/smoke_slice0.py   # expect: "PASS — foundation is wired."
```
