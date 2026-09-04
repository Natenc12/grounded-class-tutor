"""Tests for the staging knobs in `gct.config` (issue #105, ADR 0010).

`STAGING_DIR` is resolved at import, so pinning the env var's NAME and the default's location
means reloading the module under each environment - and restoring it afterwards, because
every other test in the session imported the original.
"""

from __future__ import annotations

import importlib
from pathlib import Path


def test_config_staging_dir_defaults_to_repo_data_staging_and_honors_the_env_var(
    tmp_path, monkeypatch
):
    """Pins the env var's NAME and the default's location; `gct.config` resolves both at import,
    so the module is reloaded under each environment and restored afterwards."""
    import gct.config as config

    repo_root = Path(config.__file__).resolve().parents[2]
    try:
        monkeypatch.delenv("GCT_STAGING_DIR", raising=False)
        monkeypatch.chdir(repo_root)
        importlib.reload(config)
        assert config.STAGING_DIR == repo_root / "data" / "staging"

        monkeypatch.setenv("GCT_STAGING_DIR", str(tmp_path / "elsewhere"))
        importlib.reload(config)
        assert config.STAGING_DIR == (tmp_path / "elsewhere").resolve()
        assert config.STAGING_DIR.is_absolute()
    finally:
        monkeypatch.undo()
        importlib.reload(config)
