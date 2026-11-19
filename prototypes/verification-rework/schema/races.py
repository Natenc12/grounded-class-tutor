"""Revert each of the three race fixes (#24, #86, #92) and see what the suite/DB says, under:
   A = today's schema, today's code;  B = CHECK constraint only;  C = CHECK + publish clears reason."""
import os, re, subprocess, sys
W = "src/gct/jobs/worker.py"; I = "src/gct/ingest/index.py"
orig_w = open(W).read(); orig_i = open(I).read()
CLEAR = "                status     = 'ready',\n                failed_reason = null,\n"
assert CLEAR in orig_i
MUT = {
 "#24 (claim no longer clears reason)": lambda s: s.replace(
    "                set status = 'processing',\n                    failed_reason = null,\n", "                set status = 'processing',\n", 1),
 "#86 (bury has no lease guard)": lambda s: re.sub(
    r"\n              and exists \(\n                  select 1 from jobs\n.*?\n              \)\n", "\n", s, count=1, flags=re.S),
 "#92 (publish has no entitlement guard)": lambda s: s.replace("publish_guard=_publish_guard(job),", "publish_guard=None,", 1),
}
TESTS = {
 "#24 (claim no longer clears reason)": "test_the_claim_clears_the_previous_attempts_failed_reason or test_a_retry_that_succeeds_leaves_no_failed_reason_on_the_ready_file",
 "#86 (bury has no lease guard)": "test_a_zombie_whose_lease_expired_writes_nothing_through_bury",
 "#92 (publish has no entitlement guard)": "test_a_reaped_worker_publishing_after_a_live_leased_bury_writes_nothing",
}
def psql(sql): subprocess.run(["psql","-h","localhost","gct_agent_design","-qc",sql],check=True)
def run(k):
    r = subprocess.run(["uv","run","pytest","tests/gct/jobs/test_worker.py","-q","-p","no:cacheprovider","--tb=line","-k",k],capture_output=True,text=True)
    out = r.stdout
    summary = out.strip().splitlines()[-1]
    errs = [l[:230] for l in out.splitlines() if l.startswith("E ") or "Error" in l and l.startswith("/")]
    return summary, errs
try:
    for cfg in "ABC":
        if cfg == "A": psql("alter table files drop constraint if exists files_reason_iff_failed")
        else: psql("alter table files drop constraint if exists files_reason_iff_failed; alter table files add constraint files_reason_iff_failed check ((status = 'failed') = (failed_reason is not null))")
        open(I,"w").write(orig_i if cfg == "C" else orig_i.replace(CLEAR, "                status     = 'ready',\n"))
        for name, mut in MUT.items():
            m = mut(orig_w); assert m != orig_w, name
            open(W,"w").write(m)
            summary, errs = run(TESTS[name])
            print(f"[{cfg}] revert {name}: {summary}")
            for e in errs[:3]: print("       ", e)
finally:
    open(W,"w").write(orig_w); open(I,"w").write(orig_i)
    psql("alter table files drop constraint if exists files_reason_iff_failed; alter table files add constraint files_reason_iff_failed check ((status = 'failed') = (failed_reason is not null))")
