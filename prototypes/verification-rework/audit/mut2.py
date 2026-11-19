"""Adapted from the audit's mutate.py. Differences: no -x (records EVERY failing test per planted
bug so any slice of the suite can be scored offline), per-test timeout, control runs.

  mut2.py list <repo> <n> <seed> <out.json> [exclude_plan.json]
  mut2.py run  <repo> <plan.json> <shard> <nshards> <out.jsonl>
env: PYTEST_ARGS (json list, extra pytest args), ONLY_IDS (json list)
"""
from __future__ import annotations
import json, os, re, signal, subprocess, sys, time, importlib.util
from pathlib import Path
import random

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mutate", HERE / "mutate_orig.py")
mutate = importlib.util.module_from_spec(spec); spec.loader.exec_module(mutate)
PLUG = str(HERE.parent / "plug")


def cmd_list(repo, n, seed, out, exclude=None):
    repo = Path(repo)
    allc = []
    for p in sorted((repo / "src" / "gct").rglob("*.py")):
        for c in mutate.candidates(p):
            c["file"] = str(Path(c["file"]).relative_to(repo))
            allc.append(c)
    excl = set()
    if exclude:
        for c in json.loads(Path(exclude).read_text())["picked"]:
            excl.add((c["file"], c["line"]))
    rng = random.Random(seed)
    rng.shuffle(allc)
    seen, picked = set(), []
    for c in allc:
        key = (c["file"], c["line"])
        if key in seen or key in excl:
            continue
        seen.add(key); picked.append(c)
        if len(picked) == n:
            break
    for i, c in enumerate(picked):
        c["id"] = i
    Path(out).write_text(json.dumps({"total_candidates": len(allc), "picked": picked}, indent=1))
    print(f"{len(allc)} candidates, picked {len(picked)}")


def run_pytest(repo, env, tmp, extra):
    if os.path.exists(tmp):
        os.remove(tmp)
    e = dict(env, MUT_OUT=tmp, PYTHONPATH=PLUG + os.pathsep + env.get("PYTHONPATH", ""))
    cmd = ["uv", "run", "pytest", "-m", "not live", "-q", "-p", "no:cacheprovider",
           "-p", "mutplug", "--timeout=45"] + extra
    proc = subprocess.Popen(cmd, cwd=repo, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True,
                            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    try:
        outp, _ = proc.communicate(timeout=900)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        code, outp = "timeout", ""
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
    fails, sess = [], None
    if os.path.exists(tmp):
        for line in open(tmp):
            r = json.loads(line)
            if "session_exit" in r:
                sess = r
            else:
                fails.append(r)
    return code, fails, sess, outp[-400:]


def cmd_run(repo, plan, shard, nshards, out):
    repo = Path(repo)
    picked = json.loads(Path(plan).read_text())["picked"]
    only = set(json.loads(os.environ["ONLY_IDS"])) if os.environ.get("ONLY_IDS") else None
    if only is not None:
        picked = [c for c in picked if c["id"] in only]
    mine = [c for j, c in enumerate(picked) if j % nshards == shard]
    extra = json.loads(os.environ.get("PYTEST_ARGS", "[]"))
    env = dict(os.environ)
    tmp = out + ".tmp"
    control = {"id": -1, "file": None}
    with open(out, "a") as fh:
        for c in [control] + mine + [control]:
            orig = None
            if c["file"]:
                path = repo / c["file"]
                orig = path.read_bytes()
                assert orig[c["start"]:c["end"]].decode() == c["old"], c
                path.write_bytes(orig[:c["start"]] + c["new"].encode() + orig[c["end"]:])
            t0 = time.time()
            try:
                lint = subprocess.run(["uv", "run", "ruff", "check", "-q"], cwd=repo, env=env,
                                      capture_output=True, text=True)
                code, fails, sess, tail = run_pytest(repo, env, tmp, extra)
            finally:
                if orig is not None:
                    path.write_bytes(orig)
            rec = dict(c, lint_caught=lint.returncode != 0, test_exit=code, caught=(code != 0),
                       fails=fails, sess=sess, tail=tail if (code != 0 and not fails) else "",
                       secs=round(time.time() - t0, 1))
            fh.write(json.dumps(rec) + "\n"); fh.flush()


if __name__ == "__main__":
    if sys.argv[1] == "list":
        cmd_list(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5],
                 sys.argv[6] if len(sys.argv) > 6 else None)
    else:
        cmd_run(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), sys.argv[6])
