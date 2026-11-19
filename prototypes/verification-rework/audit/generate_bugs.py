"""Tiny mutation tester for the audit. Plants one small bug at a time in src/gct, runs the
merge check's test command, and records whether any test noticed.

Usage:
  mutate.py list  <repo> <n> <seed> <out.json>     # pick n random mutations
  mutate.py run   <repo> <plan.json> <shard> <nshards> <out.jsonl>
"""

from __future__ import annotations

import ast
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

SWAP = {
    "==": "!=", "!=": "==", "<=": "<", ">=": ">", "<": "<=", ">": ">=",
    "is not": "is", "is": "is not", "not in": "in", "in": "not in",
}
OPNAME = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
    ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in",
}


def offsets(src: str):
    starts = [0]
    for line in src.splitlines(keepends=True):
        starts.append(starts[-1] + len(line.encode()))
    return starts


def candidates(path: Path):
    raw = path.read_bytes()
    src = raw.decode()
    tree = ast.parse(src)
    starts = offsets(src)

    def off(lineno, col):
        return starts[lineno - 1] + col

    def span(node):
        return off(node.lineno, node.col_offset), off(node.end_lineno, node.end_col_offset)

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
                docstrings.add(id(body[0]))

    out = []

    def add(kind, node, s, e, new):
        old = raw[s:e].decode()
        if old == new:
            return
        out.append({"file": str(path), "line": node.lineno, "kind": kind,
                    "start": s, "end": e, "old": old, "new": new})

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            name = OPNAME.get(type(node.ops[0]))
            if name:
                s = span(node.left)[1]
                e = span(node.comparators[0])[0]
                gap = raw[s:e].decode()
                pat = r"\s+".join(map(re.escape, name.split()))
                m = re.search(pat, gap)
                if m:
                    add("flip-comparison", node, s + len(gap[:m.start()].encode()),
                        s + len(gap[:m.end()].encode()), SWAP[name])
        elif isinstance(node, ast.BoolOp):
            word = "and" if isinstance(node.op, ast.And) else "or"
            other = "or" if word == "and" else "and"
            s = span(node.values[0])[1]
            e = span(node.values[1])[0]
            gap = raw[s:e].decode()
            m = re.search(rf"\b{word}\b", gap)
            if m:
                add("swap-and-or", node, s + len(gap[:m.start()].encode()),
                    s + len(gap[:m.end()].encode()), other)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            s, e = span(node)
            os_, oe = span(node.operand)
            add("drop-not", node, s, e, "(" + raw[os_:oe].decode() + ")")
        elif isinstance(node, ast.Constant) and id(node) not in docstrings:
            s, e = span(node)
            if node.value is True:
                add("true-to-false", node, s, e, "False")
            elif node.value is False:
                add("false-to-true", node, s, e, "True")
            elif isinstance(node.value, int) and not isinstance(node.value, bool):
                add("number-plus-one", node, s, e, str(node.value + 1))
        elif isinstance(node, ast.Return) and node.value is not None:
            if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                s, e = span(node)
                add("return-nothing", node, s, e, "return None")
        elif isinstance(node, ast.Raise):
            s, e = span(node)
            add("remove-raise", node, s, e, "pass")
        elif isinstance(node, ast.If):
            s, e = span(node.test)
            add("negate-if", node, s, e, "not (" + raw[s:e].decode() + ")")
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
                and id(node) not in docstrings:
            s, e = span(node)
            add("delete-call", node, s, e, "pass")
    return out


def cmd_list(repo, n, seed, out):
    repo = Path(repo)
    allc = []
    for p in sorted((repo / "src" / "gct").rglob("*.py")):
        for c in candidates(p):
            c["file"] = str(Path(c["file"]).relative_to(repo))
            allc.append(c)
    rng = random.Random(seed)
    rng.shuffle(allc)
    # at most one mutation per (file, line) so the sample spreads out
    seen, picked = set(), []
    for c in allc:
        key = (c["file"], c["line"])
        if key in seen:
            continue
        seen.add(key)
        picked.append(c)
        if len(picked) == n:
            break
    for i, c in enumerate(picked):
        c["id"] = i
    Path(out).write_text(json.dumps({"total_candidates": len(allc), "picked": picked}, indent=1))
    print(f"{len(allc)} candidates, picked {len(picked)}")


def cmd_run(repo, plan, shard, nshards, out):
    repo = Path(repo)
    picked = json.loads(Path(plan).read_text())["picked"]
    only = set(json.loads(os.environ["ONLY_IDS"])) if os.environ.get("ONLY_IDS") else None
    if only is not None:
        picked = [c for c in picked if c["id"] in only]
    mine = [c for j, c in enumerate(picked) if j % nshards == shard]
    env = dict(os.environ)
    with open(out, "a") as fh:
        for c in mine:
            path = repo / c["file"]
            orig = path.read_bytes()
            assert orig[c["start"]:c["end"]].decode() == c["old"], c
            path.write_bytes(orig[:c["start"]] + c["new"].encode() + orig[c["end"]:])
            t0 = time.time()
            try:
                lint = subprocess.run(["uv", "run", "ruff", "check", "-q"], cwd=repo, env=env,
                                      capture_output=True, text=True)
                try:
                    r = subprocess.run(
                        ["uv", "run", "pytest", "-m", "not live", "-x", "-q", "-p",
                         "no:cacheprovider"],
                        cwd=repo, env=env, capture_output=True, text=True, timeout=420,
                        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
                    code, tail = r.returncode, r.stdout[-1500:]
                except subprocess.TimeoutExpired:
                    code, tail = "timeout", ""
            finally:
                path.write_bytes(orig)
            first_fail = ""
            m = re.search(r"^(FAILED|ERROR) (\S+)", tail, re.M)
            if m:
                first_fail = m.group(2)
            rec = dict(c, lint_caught=lint.returncode != 0, test_exit=code,
                       caught=(code != 0), first_fail=first_fail,
                       secs=round(time.time() - t0, 1))
            fh.write(json.dumps(rec) + "\n")
            fh.flush()


if __name__ == "__main__":
    if sys.argv[1] == "list":
        cmd_list(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5])
    else:
        cmd_run(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), sys.argv[6])
