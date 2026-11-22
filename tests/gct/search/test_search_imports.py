"""Search must run without Postgres or the hosted app (#216, #230).

The practice exam asks through this search on a machine with no Postgres, and stage 2 deletes the
pgvector retriever. Importing `gct.search` in a fresh interpreter must not load either.
"""

from __future__ import annotations

import subprocess
import sys

RETIRED = ("gct.retriever", "gct.ask", "gct.api", "gct.db", "psycopg", "pgvector")


def test_search_loads_nothing_the_desktop_move_retires():
    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, gct.search; print('\\n'.join(sys.modules))"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert loaded
    assert [name for name in loaded if name.startswith(RETIRED)] == []
