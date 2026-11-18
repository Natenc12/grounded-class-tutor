---
name: gct-app
description: Find your way around the Grounded Class Tutor product and run it for free. Use when a request or bug report names a behaviour and you need to locate it, when you need to see the real app do something, or before saying a change to the product works.
---

## Find a feature

Read `feature-map.md` in this directory. It is a map of the product from the outside, not of the
code. Its `Reach`, `Costs money`, `Code` and `Tests` lines are checked against the real app by
`tests/test_feature_map.py`, so trust them. If you add, remove or rename a feature, that test
fails until the map agrees.

Exact request and response fields are in `web/openapi.json`, which is pinned to the live API by its
own test. The map does not repeat them.

## Run the app and bring back evidence

```sh
uv run --extra dev python .claude/skills/gct-app/drive.py
```

This starts the real API and the real worker, then creates a class, uploads a file, waits for it,
asks one question the file covers and one it does not, and uploads a file that cannot be read. It
prints each step and ends with `DRIVE PASSED` or `DRIVE FAILED at: <step>`. Paste that output as
your evidence.

It is free and safe by construction: it creates its own database `gct_drive`, blanks the API key,
and selects stand-in models with `GCT_PROVIDERS=standin`.

To drive your own scenario, pass a file and the questions that go with it:

```sh
uv run --extra dev python .claude/skills/gct-app/drive.py \
  --corpus path/to/file.pdf --question "..." --out-of-corpus-question "..."
```

`--launch-only` proves only that the two processes start.

## What a free run does not show

The stand-in models do not read. The embedder counts shared words, and the answerer quotes the
sentence that shares the most words with the question, or declines when fewer than half the
question's words appear in a source. A free run shows that upload, queue, worker, search,
citations, refusals and failure reasons are wired correctly. It says nothing about whether a real
model would answer well.
