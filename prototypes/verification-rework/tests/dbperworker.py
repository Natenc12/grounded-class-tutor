"""One database per pytest-xdist worker: gw0 -> gct_agent_tests_1, gw1 -> gct_agent_tests_2, ..."""
import os
def pytest_configure(config):
    w = os.environ.get("PYTEST_XDIST_WORKER")
    if w:
        os.environ["DATABASE_URL"] = "postgresql://localhost:5432/gct_agent_tests_%d" % (int(w[2:]) + 1)
