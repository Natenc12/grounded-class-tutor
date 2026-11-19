import json, glob, sys, collections
A = sys.argv[1]
plan = {c["id"]: c for c in json.load(open(A + "/planAB.json"))["picked"]}
recs, controls = {}, []
for f in sorted(glob.glob(A + "/out/mut/full-*.jsonl")) + sorted(glob.glob(A + "/out/mut/refull-*.jsonl")):
    for l in open(f):
        r = json.loads(l)
        if r["id"] == -1: controls.append(r)
        else: recs[r["id"]] = r
dbtests = {l.strip() for l in open(A + "/out/collect-db.txt") if "::" in l}
nodbtests = {l.strip() for l in open(A + "/out/collect-nodb.txt") if "::" in l}
minset = set(json.load(open(A + "/out/cov-minset.json")))
print("controls:", [(c["test_exit"], len(c["fails"])) for c in controls])
def tf(n): return n.split("::")[0]
def kills(r):
    """set of failing test ids; None means the whole session crashed (every slice sees it)"""
    if r["test_exit"] == 0: return set()
    if not r["fails"]: return None
    return {x["nodeid"] for x in r["fails"]}
def caught(r, pred):
    k = kills(r)
    if k is None: return True
    return any(pred(n) for n in k)
SLICES = {
 "full suite": lambda n: True,
 "(a) without tests/scripts": lambda n: not n.startswith("tests/scripts"),
 "    only tests/scripts": lambda n: n.startswith("tests/scripts"),
 "(b) outside-in only (tests/gct/api + tests/gct/ask)": lambda n: n.startswith("tests/gct/api") or n.startswith("tests/gct/ask"),
 "    only tests/gct/api": lambda n: n.startswith("tests/gct/api"),
 "    everything except api+ask": lambda n: not (n.startswith("tests/gct/api") or n.startswith("tests/gct/ask")),
 "(c) only db-marked tests": lambda n: n in dbtests or ("::" not in n),
 "    only non-db tests": lambda n: n in nodbtests or ("::" not in n),
 "(d) without tests/gct/jobs/test_worker.py": lambda n: tf(n) != "tests/gct/jobs/test_worker.py",
 "    without tests/gct/jobs (both files)": lambda n: not n.startswith("tests/gct/jobs"),
 "    without tests/gct/eval": lambda n: not n.startswith("tests/gct/eval"),
 "    without tests/scripts and test_worker.py": lambda n: not n.startswith("tests/scripts") and tf(n) != "tests/gct/jobs/test_worker.py",
 "    coverage-minimal 132 tests": lambda n: n in minset,
}
ids = sorted(recs)
for name, sel in (("A (audit's 100)", [i for i in ids if i < 100]), ("B (fresh 100, seed 777)", [i for i in ids if i >= 100]), ("A+B", ids)):
    print(f"\n== sample {name}: {len(sel)} planted bugs run")
    for s, pred in SLICES.items():
        c = sum(caught(recs[i], pred) for i in sel)
        print(f"  {s:55s} {c:4d} / {len(sel)}")
    print("  lint (ruff check) alone:", sum(recs[i]["lint_caught"] for i in sel))
    missed = [i for i in sel if not caught(recs[i], lambda n: True)]
    print("  missed by full suite:", [(i, plan[i]["file"].replace("src/gct/", ""), plan[i]["line"], plan[i]["kind"]) for i in missed])
# redundancy of kills
nk = sorted(len(kills(recs[i]) or []) for i in ids if recs[i]["test_exit"] != 0)
import statistics
if nk:
    print("\nfailing tests per caught bug: median", statistics.median(nk), "mean %.1f" % statistics.mean(nk), "min", nk[0], "max", nk[-1],
          "| caught by exactly 1 test:", sum(x == 1 for x in nk), "| by <=3:", sum(x <= 3 for x in nk), "| by >=10:", sum(x >= 10 for x in nk))
# unique kills per test file / dir
uniq = collections.Counter(); uniqd = collections.Counter(); anyk = collections.Counter()
def grp(f):
    if f.startswith("tests/scripts"): return "tests/scripts"
    p = f.split("/"); return "/".join(p[:3]) if len(p) > 3 else "tests/gct/(top)"
for i in ids:
    k = kills(recs[i])
    if not k: continue
    fs = {tf(n) for n in k}; ds = {grp(f) for f in fs}
    for f in fs: anyk[f] += 1
    if len(fs) == 1: uniq[next(iter(fs))] += 1
    if len(ds) == 1: uniqd[next(iter(ds))] += 1
print("\nper test file: bugs it catches / bugs ONLY it catches")
for f, n in sorted(anyk.items(), key=lambda x: -x[1]): print(f"  {f:50s} {n:4d} {uniq[f]:4d}")
print("per dir, bugs only that dir catches:", dict(uniqd))
# greedy minimal kill set chosen on A, validated on B (and vice versa)
def greedy(sel):
    need = {i: kills(recs[i]) for i in sel if kills(recs[i])}
    chosen = []
    while need:
        cnt = collections.Counter(n for k in need.values() for n in k)
        t = cnt.most_common(1)[0][0]; chosen.append(t)
        need = {i: k for i, k in need.items() if t not in k}
    return set(chosen)
Aids = [i for i in ids if i < 100]; Bids = [i for i in ids if i >= 100]
if Aids and Bids:
    for nm, tr, te in (("A", Aids, Bids), ("B", Bids, Aids)):
        g = greedy(tr)
        print(f"\nsmallest test set that catches everything caught in {nm}: {len(g)} tests; on the OTHER sample it catches",
              sum(caught(recs[i], lambda n: n in g) for i in te), "of", len(te), "(full suite:", sum(caught(recs[i], lambda n: True) for i in te), ")")
json.dump({str(i): (sorted(kills(recs[i])) if kills(recs[i]) is not None else None) for i in ids}, open(A + "/out/kills.json", "w"))
