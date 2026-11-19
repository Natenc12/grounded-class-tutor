"""Records every non-passing test (and collection error) to $MUT_OUT, one JSON line each, as it happens."""
import json, os

_fh = None
def _w(rec):
    global _fh
    if _fh is None:
        _fh = open(os.environ["MUT_OUT"], "a")
    _fh.write(json.dumps(rec) + "\n"); _fh.flush()

def pytest_collectreport(report):
    if report.failed:
        _w({"nodeid": report.nodeid, "when": "collect"})

def pytest_runtest_logreport(report):
    if report.failed:
        _w({"nodeid": report.nodeid, "when": report.when})

def pytest_sessionfinish(session, exitstatus):
    _w({"session_exit": int(exitstatus), "collected": getattr(session, "testscollected", -1)})
