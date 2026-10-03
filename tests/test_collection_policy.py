"""Exercise test selection in fresh pytest processes, before hosted imports."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def collection_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    shutil.copyfile(Path(__file__).with_name("conftest.py"), tests / "conftest.py")
    (tests / "test_local.py").write_text("def test_local():\n    pass\n", encoding="utf-8")
    hosted = tests / "gct" / "api"
    hosted.mkdir(parents=True)
    # A broken legacy conftest is enough to break a local-only install unless
    # the collector declines the directory before loading its fixtures.
    (hosted / "conftest.py").write_text(
        'raise RuntimeError("legacy fixture imported")\n', encoding="utf-8"
    )
    (hosted / "test_hosted.py").write_text("def test_hosted():\n    pass\n", encoding="utf-8")
    (hosted.parent / "test_config.py").write_text(
        'raise RuntimeError("legacy module imported")\n', encoding="utf-8"
    )
    return tmp_path


def _collect(project, *arguments):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *arguments],
        cwd=project,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_default_collection_never_imports_legacy_fixtures(collection_project):
    result = _collect(collection_project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_local" in result.stdout
    assert "1 test collected" in result.stdout
    assert "legacy" not in result.stderr


def test_explicit_legacy_file_needs_opt_in_before_module_import(collection_project):
    result = _collect(collection_project, "tests/gct/test_config.py")
    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert "require --legacy-hosted" in result.stderr
    assert "legacy module imported" not in result.stdout + result.stderr


def test_legacy_opt_in_does_not_hide_broken_hosted_coverage(collection_project):
    result = _collect(collection_project, "--legacy-hosted")
    assert result.returncode != 0
    assert "legacy fixture imported" in result.stdout + result.stderr
    assert "legacy module imported" in result.stdout + result.stderr
