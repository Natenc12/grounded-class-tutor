"""Hand-planted SQL-level bugs in the queue/worker/index (the #24/#86/#92/#95 class, which the
random Python-level mutation operators cannot produce). usage: sqlmut.py <repo> <out.jsonl> <pytest args...>"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
W, Q, I = "src/gct/jobs/worker.py", "src/gct/jobs/queue.py", "src/gct/ingest/index.py"
MUTANTS = [
 ("W1 bury may overwrite a ready file", W, 384, "and status <> 'ready'", ""),
 ("W2 bury no longer checks the lease is ours", W, 389, "and lease_token = %(lease_token)s::uuid", ""),
 ("W3 claim may move a ready file back to processing", W, 853, "and status <> 'ready'", ""),
 ("W4 claim no longer clears the previous failed_reason", W, 850, "failed_reason = null,", ""),
 ("W5 publish guard no longer checks the lease is ours", W, 282, "and lease_token = %(lease_token)s::uuid", ""),
 ("Q1 settle no longer checks the lease is ours", Q, 389, "and lease_token = %(lease_token)s::uuid", ""),
 ("Q2 settle no longer requires state=processing", Q, 388, "and state = 'processing'", ""),
 ("Q3 reaper leaves the old lease token on the job", Q, 657, "lease_token = null,", ""),
 ("Q4 reaper takes live leases instead of expired ones", Q, 659, "leased_until < now()", "leased_until > now()"),
 ("Q5 claim does not count the attempt", Q, 303, "attempts = attempts + 1,", ""),
 ("Q6 release leaves the lease deadline set", Q, 528, "state = 'queued', leased_until = null, last_error", "state = 'queued', last_error"),
 ("Q7 renew_lease no longer checks the lease is ours", Q, 597, "and lease_token = %(lease_token)s::uuid", ""),
 ("Q8 claim takes the newest job, not the oldest", Q, 283, "order by j.created_at", "order by j.created_at desc"),
 ("Q9 claim also takes jobs that are not queued", Q, 282, "where j.state = 'queued'", "where j.state <> 'done'"),
 ("I1 index_file ignores the publish guard", I, 220, "if publish_guard is not None and not publish_guard(conn):", "if False:"),
 ("I2 re-index does not delete the old chunks", I, 279, 'file_id = %(file_id)s::uuid",', 'file_id = %(file_id)s::uuid and false",'),
]
MUTANTS_N = [  # second batch, written AFTER the v3 state machine was frozen and before running it on these
 ("N1 re-publish does not set status back to ready", I, 272, "status     = 'ready',", "status     = files.status,"),
 ("N2 bury records failed with no reason", W, 381, "failed_reason = %(reason)s,", "failed_reason = null,"),
 ("N3 claim hands out the job but leaves it queued", Q, 302, "set state = 'processing',", "set state = 'queued',"),
 ("N4 claim does not issue a fresh lease token", Q, 304, "lease_token = gen_random_uuid(),", "lease_token = coalesce(lease_token, gen_random_uuid()),"),
 ("N5 reaper marks expired jobs failed instead of queued", Q, 655, "set state = 'queued',", "set state = 'failed',"),
 ("N6 reaper also requeues finished jobs with an old deadline", Q, 659, "where state = 'processing' and leased_until < now()", "where leased_until < now()"),
 ("N7 complete records the job as failed", Q, 449, "state = 'done'", "state = 'failed'"),
 ("N8 claim's status write says queued, not processing", W, 849, "set status = 'processing',", "set status = 'queued',"),
 ("N9 release keeps the job in processing", Q, 528, "state = 'queued', leased_until = null", "state = 'processing', leased_until = null"),
 ("N10 publish guard does not lock the job row", W, 283, "for update", ""),
]
if os.environ.get("SET") == "N": MUTANTS = MUTANTS_N
repo, out, args = Path(sys.argv[1]), sys.argv[2], sys.argv[3:]
def run():
    p = subprocess.run(["uv", "run", "pytest", "-m", "not live", "-q", "-x", "-p", "no:cacheprovider", "--timeout=120", *args],
                       cwd=repo, capture_output=True, text=True, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    return p.returncode, p.stdout[-3000:]
import re
with open(out, "a") as fh:
    for name, f, line, old, new in [("control", None, 0, "", "")] + MUTANTS + [("control", None, 0, "", "")]:
        orig = None
        if f:
            path = repo / f; orig = path.read_text(); lines = orig.split("\n")
            assert old in lines[line - 1], (name, lines[line - 1])
            lines[line - 1] = lines[line - 1].replace(old, new); path.write_text("\n".join(lines))
        t0 = time.time()
        try:
            code, tail = run()
        finally:
            if orig is not None: path.write_text(orig)
        m = re.search(r"^(FAILED|ERROR) (\S+)", tail, re.M)
        fh.write(json.dumps({"name": name, "caught": code != 0, "exit": code, "first": m.group(2)[-90:] if m else "", "secs": round(time.time() - t0, 1)}) + "\n"); fh.flush()
