"""Configuration + the embedding-consistency anchor (ADR 0018).

The invariant: index-time and query-time embeddings use the *identical* model + version, or
similarity search is meaningless. Two distinct rules enforce it — don't collapse them:

  - *Which embedder gets constructed* is sourced only from `ACTIVE_EMBEDDING_MODEL_ID`, here.
    Never hardcode the model id anywhere else.
  - *What gets recorded about a run* is NOT sourced from here: `chunks.embedding_model_id` is
    stamped from `embedder.model_id` — the model that ACTUALLY produced the stored vectors —
    and the Retriever's guard compares that stamp against the active embedder's `model_id`.
    Sourcing both sides from config would make the guard compare config to itself and never
    fire (ADR 0018; the mismatch test in tests/gct/retriever/ proves the guard is real).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- The embedding invariant anchor (ADR 0018) ---------------------------------------------
# Provisional default (ADR 0005 / roadmap Slice 0). `EMBEDDING_DIM` is the one schema-coupled
# spike variable — changing the embedder to a different dim requires a migration + re-index.
ACTIVE_EMBEDDING_MODEL_ID = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# Generation provisional default (ADR 0007); the GPT-vs-Claude bake-off is Spike Pass 2.
DEFAULT_GENERATION_MODEL = "gpt-4o-mini"

# --- The ingest input ceiling (ADR 0029) ----------------------------------------------------
# The largest input the ingest pipeline will embed, counted in whitespace-split words. Past it,
# ingest refuses TERMINALLY (`ParseError("too_long")` -> `files.failed_reason='too_long'`, ADR
# 0020, terminal set extended per ADR 0029) instead of buying an unbounded embedding run.
#
# Why words rather than bytes, pages, or chunks - and why this number - is ADR 0029's argument,
# measured on the dogfood corpus; do not re-derive it here. One knob: `compose`/`ingest_file`
# take `max_words` defaulted to this, so a caller may lower it without a second constant
# existing anywhere.
MAX_INGEST_WORDS = 250_000

# --- File staging (ADR 0010) ----------------------------------------------------------------
# Where an upload's bytes land before the worker opens them (`gct.staging.stage`), and the most
# bytes ONE upload may put there. Both are one-knob defaults in the shape of `MAX_INGEST_WORDS`:
# `stage` takes `max_bytes` / `staging_dir` defaulted to these, so a caller may lower or redirect
# without a second constant existing anywhere. The API skeleton (#104) and the files router
# (#110) MUST read them from here - this module is the only settings layer, and a second one (a
# pydantic Settings, an env read inside a router) would be two writers for one fact.
#
# `MAX_STAGE_BYTES` is PROVISIONAL: the NUMBER is a placeholder no ADR owns yet, and a later ADR
# sets it. ADR 0029's ceiling is counted in words over `parse_file`'s output, so it cannot
# protect the disk - the bytes are already there when it fires. What the placeholder must admit
# is N6's shape, a <=50-page deck with images: the dogfood corpus's fattest deck is 7.11 MB for
# 8 slides (ADR 0029 §1), so ~45 MB at 50, and 100 MiB is that with ~2x headroom. Exceeding it
# is refused at REQUEST time as `StagingError("too_large")` - never a `files.failed_reason`,
# because no `files` row exists yet to carry one (see `gct.staging`).
MAX_STAGE_BYTES = 100 * 1024 * 1024

# Only the WRITER needs this: the worker never looks it up, it opens the absolute path
# `enqueue` stored as `files.staging_ref`. Env-overridable (`GCT_STAGING_DIR`) so a deployment
# can point uploads at a different disk without a code change; the default sits beside the
# dogfood corpus and is gitignored like it (`data/staging/`). Resolved at import so a relative
# value is pinned to the cwd of the process that read it - `staging_ref` must stay openable
# after any later `chdir`.
STAGING_DIR = Path(os.environ.get("GCT_STAGING_DIR", "data/staging")).resolve()

# --- The adapter's request-body bound (issue #125) -------------------------------------------
# What the API will buffer and hand to a JSON parser: a byte ceiling and a nesting ceiling,
# enforced in ONE place (`gct.api.limits`, in front of every route) and never per-router.
#
# They sit BESIDE `MAX_STAGE_BYTES` because the three numbers are only legible together: a
# multipart upload is EXEMPT from the byte bound below, and it is exempt because `MAX_STAGE_BYTES`
# owns that path. Split them across two modules and the next editor to lower one cannot see that
# the other is the reason this one has a deliberate hole in it.
#
# WHAT `MAX_STAGE_BYTES` DOES NOT BOUND, since the exemption is often read as "so the upload is
# bounded": it bounds what the STAGING DIR holds and what gets queued, not what a client can make
# the server write. Starlette's multipart parser spools the whole part to the OS temp directory
# before `routers/files.py` is entered, so `stage` refuses a file that is already on disk in full.
# `gct.api.limits.BodyLimit` refuses a multipart request whose DECLARED `content-length` exceeds
# this number, in front of that parser - an interim bound that a chunked or lying request skips.
#
# Both are PROVISIONAL in the shape `MAX_STAGE_BYTES` is: no ADR owns either NUMBER yet, so what
# each must admit is written down instead of an argument for its exact value.
#
# `MAX_JSON_BODY_BYTES` must admit the largest LEGITIMATE JSON body the adapter has, which is
# `POST /ask`: a uuid plus a question of `MAX_QUESTION_CHARS` characters. At its worst that is not
# 2000 bytes but 24,068 - a client may send astral-plane characters as `\uXXXX\uXXXX` surrogate
# pairs, twelve bytes per character, and `max_length` counts characters. 64 KiB is that with ~2.7x
# headroom, and still two orders of magnitude below the multi-megabyte class name this bound
# exists to refuse.
MAX_JSON_BODY_BYTES = 64 * 1024

# `MAX_JSON_BODY_DEPTH` must admit 1: every request body in this adapter is a flat object of
# strings. What it must stay UNDER is the depth at which rendering a REJECTED body recurses -
# `errors._validation` runs `jsonable_encoder` over pydantic's echo of the offending value, and
# past some depth that raises `RecursionError` inside the handler, so the 422 collapses into a
# 500 (issue #125). That depth is not a constant and must not be treated as one: the same
# interpreter broke anywhere between 480 and 970 depending only on how much stack the caller had
# already spent. So this is set far below the lowest of them rather than near any of them - 32 is
# ~15x under the shallowest break ever measured and 32x over the deepest legitimate body.
MAX_JSON_BODY_DEPTH = 32

# --- The error envelope's echo bound (issue #134) ---------------------------------------------
# The most CHARACTERS of any ONE client-controlled string an error envelope will echo back
# (`gct.api.errors._json_safe`). Past it the string is truncated and marked with its true
# length; the envelope's SHAPE is untouched - every field a client reads is still there.
#
# It sits with the two bounds above because the three are only legible together: a bound on what
# comes IN cannot bound what goes OUT. A body labelled `multipart/form-data` is exempt from
# `MAX_JSON_BODY_BYTES` by design - that exemption is what lets a 100 MiB upload through - and
# pydantic echoes such a body back WHOLE when it lands on a JSON route instead. Measured before
# this bound existed: 2,000,000 bytes in, 2,000,215 bytes out (issue #134).
#
# PROVISIONAL in the shape `MAX_STAGE_BYTES` is: no ADR owns the NUMBER. What it must admit is
# enough of a rejected value for a client to RECOGNISE which value was refused - the opening of a
# question or a class name, not the whole of either; past it the client is TOLD the full length
# rather than shown it. Counted in characters rather than bytes because that is what the values
# it bounds are counted in (`routers/ask.py`'s `MAX_QUESTION_CHARS`); the wire cost is higher,
# since `_SafeJSONResponse.render` escapes with `ensure_ascii=True` and one astral character
# costs twelve bytes there, so the true ceiling per string is ~6 KiB and not 512 bytes.
MAX_ERROR_ECHO_CHARS = 512

# --- The error envelope's detail bound (issue #137) -------------------------------------------
# The most BYTES of serialised `detail` an error envelope will carry (`gct.api.errors`). Past it
# the TAIL of the list is dropped and ONE marker entry saying how many went is appended.
#
# WHAT HAPPENS TO A SURVIVING ENTRY IS NOT WRITTEN HERE. That contract has two writers on purpose,
# the way every client-facing contract in this adapter does — `_detail_marker`'s docstring for the
# code and api.md's error-envelope section for the spec — and this comment was a THIRD, saying
# "untouched": a word `_bounded` makes false, since it may already have truncated a string inside a
# kept entry. It outlived the correction of the other two by using a DIFFERENT WORD from the one
# the census swept for. That is the concrete form of CLAUDE.md's rule: a comment may name what the
# code must do, and restating the contract adds a writer nobody will sweep.
#
# It sits beside the bound above because the two are one mechanism split across two axes, and
# neither covers the other's: `MAX_ERROR_ECHO_CHARS` bounds each VALUE, this bounds the SHAPE's
# total size. An `extra="forbid"` model emits one `detail` entry per unexpected key and the CLIENT
# picks how many keys it sends, so every string can sit far under 512 characters while the list
# itself is unbounded in length. Measured on `main`: a 65,347-byte body - legal under
# `MAX_JSON_BODY_BYTES` - bought a 547,133-byte response, ~9x the bytes of each key it echoed
# (issue #137).
#
# PROVISIONAL in the shape `MAX_STAGE_BYTES` is: no ADR owns the NUMBER. What it must admit is
# every legitimate malformed request this adapter can produce, which is a handful of entries - one
# per field of the body that was wrong, and the largest request model here has three. At ~100
# bytes per pydantic entry, 16 KiB admits ~160 of them, so the headroom over anything a client
# sends by mistake is two orders of magnitude. Counted in bytes rather than in entries because an
# entry is not a fixed size: a single entry echoing a whole rejected body was measured at 54,333
# bytes, and a bound on the COUNT would have let that one through untouched.
MAX_ERROR_DETAIL_BYTES = 16 * 1024

# --- The V1 owner (ADR 0004) ----------------------------------------------------------------
# V1 is ONE hardcoded user with no auth (ADR 0004; ADR 0002's tenancy clause as amended by it).
# Every row the API writes and every scoped query it runs carries this owner_id, so V3 turns
# enforcement (auth + RLS) on over the same column instead of reshaping the schema.
#
# ONE source, on purpose: `gct.api.deps.owner_id` reads it and `scripts/ask_smoke.py` defaults
# `--owner` to it, so the API answers over the SAME corpus the smoke ingested. A second literal
# anywhere would be a second writer of this value, and the day they differ the API silently
# answers from an empty class. Not an environment variable: nothing in V1 has a second user to
# select, and a knob with one legal value is a place for the two copies to start (issue #104).
V1_OWNER_ID = "nate-dogfood"


@dataclass(frozen=True)
class Settings:
    database_url: str
    openai_api_key: str


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://localhost:5432/grounded_class_tutor"
        ),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
    )
