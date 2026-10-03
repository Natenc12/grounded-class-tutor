"""The default local path must work with hosted dependencies unavailable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_local_grounding_and_eval_do_not_import_hosted_stack(tmp_path):
    root = Path(__file__).resolve().parents[2]
    # Fresh interpreter: an earlier legacy test cannot hide an accidental import
    # by leaving hosted modules in sys.modules. -I also ignores caller PYTHONPATH.
    script = """
import importlib.abc
import io
import json
import sys

sys.path.insert(0, sys.argv[1])
blocked = {
    'psycopg', 'pgvector', 'openai', 'dotenv', 'fastapi', 'uvicorn',
    'gct.config', 'gct.db', 'gct.retriever.retrieve',
}

class NoHostedImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError('Unexpected hosted import: ' + fullname)

def audit(event, args):
    if event == 'open' and str(args[0]).endswith('.env'):
        raise AssertionError('Unexpected dotenv read')
    if event == 'socket.connect':
        raise AssertionError('Unexpected network connection')

sys.meta_path.insert(0, NoHostedImports())
sys.addaudithook(audit)
from gct.eval.scoring import Outcome, score_state
from gct.grounder.answer import GrounderState
from gct.local_proof import run

request = {'sample': True, 'question': 'What is active recall?'}
reply = {'type': 'generated', 'text': 'Retrieve from memory [S1].\\nCOVERAGE: complete'}
source = io.StringIO(json.dumps(request) + '\\n' + json.dumps(reply) + '\\n')
destination = io.StringIO()
assert run(source, destination) == 0
events = [json.loads(line) for line in destination.getvalue().splitlines()]
assert [event['type'] for event in events] == ['generate', 'result']
result = events[-1]['result']
assert result['state'] == 'GROUNDED'
assert result['citations'][0]['file'] == 'local-proof-sample.pdf'
assert result['citations'][0]['page_or_slide'] == 1
assert score_state(GrounderState(result['state']), 'answer') is Outcome.PASS
assert not any(name in sys.modules for name in blocked)
print(json.dumps(result))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script, str(root / "src")],
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout)["state"] == "GROUNDED"
