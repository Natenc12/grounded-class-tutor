# CONTRIBUTING — how we work on this repo

Three of us are on this and none of us have worked on a real team codebase before. This is our
playbook for not stepping on each other's changes and for making our reviews teach us something.
It's close to how professional teams work, with a couple of shortcuts because we're three
students, not thirty engineers.

Boring on purpose. Follow it and we'll be faster, not slower.

---

## The one rule

**No one pushes directly to `main`.** Every change goes through a pull request (PR) that at
least one other person on the team reviews and approves before it merges.

Why:
- Three people writing to the same file at once is how projects break silently.
- The reviewer learns what you built, so knowledge doesn't stay siloed in one head.
- If we ever get asked "how does your team review code?" in an interview, this is a real answer
  we can give.

`main` should always work — always pass the smoke test. If a PR breaks it, we roll back and try
again rather than "fixing it forward."

---

## The workflow, step by step

### 1. Start from a fresh main

```sh
git checkout main
git pull
```

### 2. Make a branch

Naming: `yourname/short-description`. Keep it short — this is just a label.

```sh
git checkout -b nate/grounder-refusal-logic
```

### 3. Do the work — commit as you go

Small, frequent commits are easier to review than one giant one. First line of the commit
message is a short summary of what changed; body is optional and used when the "why" is
non-obvious.

```sh
git add <files>
git commit -m "Grounder: refuse on empty retrieval"
```

### 4. Push the branch

```sh
git push -u origin nate/grounder-refusal-logic
```

The `-u` sets up the remote branch the first time. After that, just `git push`.

### 5. Open a pull request on GitHub

The URL appears in your terminal after the push, or go to the repo on github.com and click the
"compare & pull request" button GitHub shows for new branches.

In the PR description, write:
- **What changed and why** — one paragraph.
- **How to test it locally** — the commands you ran to verify it works.
- **Anything you're unsure about** — flag it explicitly. Reviewers can't help with things
  they don't know are uncertain.

### 6. Ask a teammate to review

Tag them on the PR or ping them in Discord. Don't wait for them to notice — they're
heads-down on their own work.

### 7. Address any comments

Push more commits to the same branch and they automatically show up in the PR. Reply to
comments as you address them so the reviewer knows what to re-check.

### 8. Merge when approved

GitHub gives you a merge button once someone approves. **Use "Squash and merge"** — this
collapses all your in-progress commits into one clean commit on `main`. Keeps main's history
readable (one entry per feature) instead of noisy (twenty WIP commits per feature).

### 9. Delete the branch

GitHub gives you a button. Keeps the branch list from growing forever.

---

## What we look for in reviews

Focus on things that matter:
- **Does it do what the PR description says?** Actually run it locally, don't just read.
- **Is the logic clear?** If you had to think hard about why it works, ask — the code will
  need to be re-understood next month.
- **Obvious edge cases missed?** Empty input, error paths, both-null-and-empty-string, huge
  input. You don't need to test every case; you just need to notice the ones we're likely to
  actually hit.
- **Does it respect the design invariants?** See `CLAUDE.md`'s constraint list — the embedding
  invariant (ADR 0018), hand-rolled RAG (ADR 0003), `owner_id AND class_id` filtering, the
  citation spine. These are the load-bearing rules; PRs must not break them.

Skip:
- **Style nits** — indentation, quote choice, line length. If we add a linter later, those get
  auto-checked. Reviewing them by hand is a waste of everyone's time.
- **Bikeshedding.** If you'd solve it two different ways and both are fine, don't argue about
  it in the PR. Approve it.

---

## How much back-and-forth is normal

- **Approved on first review** — happens for small, focused PRs. Aim for these.
- **1–2 rounds of comments** — normal for real work.
- **5+ rounds of comments** — the PR is too big, or the design isn't clear yet. Break it up,
  or step back and talk it through in Discord before continuing.

Being asked to change things isn't a knock. Reviews are a conversation, not a verdict.

---

## When to ask questions

Early. In the PR, in Discord, in a call. Don't guess for 90 minutes when someone who's touched
that file could unblock you in 2. This isn't weakness — it's how the team gets faster over
time.

---

## Glossary — terms you'll see

**pull request (PR)** — a proposal on GitHub to merge your branch into `main`. Others review
it, comment, request changes, or approve. It's the unit of code review.

**branch** — a parallel version of the codebase you can commit to without touching `main`.
Cheap and disposable — you make one per change, delete after merging.

**main** — the branch everyone treats as the "official" version of the code. The invariant
we're protecting: main should always work, always pass the smoke test.

**squash merge** — collapsing all a PR's commits into one clean commit on main when you merge.
Keeps main's history readable.

**diff** — the set of changes in a PR. GitHub shows it as red (deleted) + green (added) lines.
Reviewers read the diff.

**linter** — a tool that reads your code without running it and flags likely problems
(unused imports, inconsistent style, obvious mistakes). We haven't added one yet; if we do,
it'll be [Ruff](https://docs.astral.sh/ruff/).

**style bikeshedding** — arguing in a PR about tabs vs spaces, quote style, comma placement.
All trivial, all preferences. Waste of review time. A linter with agreed rules ends these
debates once.

**smoke test** — a quick check that the system's fundamentals work end-to-end. Ours is
`scripts/smoke_slice0.py` for Slice 0 — expect `"PASS — foundation is wired."` before you
push.
