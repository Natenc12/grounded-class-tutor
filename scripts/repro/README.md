# scripts/repro

One runnable reproduction per issue that needed one, named `issue_<N>_<what>.py`.

**What these are for:** demonstrating a *defect*, and demonstrating that a fix closes it. Each one
drives the real code — the real queue verbs, the real writers — in the order that produces the bug,
and reads the result back on a connection neither writer holds. It exits non-zero on the defect and
zero once the fix is in, so the same script is both the before and the after.

**What they are not:** tests. Nothing in CI runs them, and they are not evidence that the test suite
is any good — that argument is made by mutation, and it lives on the pull request rather than here.
A reproduction answers "did this actually happen?"; the suite answers "would we notice if it came
back?".

They live here rather than in `scripts/` proper so the peer callers that CLAUDE.md names —
`migrate.py`, `smoke_slice0.py`, `ask_smoke.py` — stay easy to find.

Each takes `DATABASE_URL` and refuses to run against `grounded_class_tutor`.
