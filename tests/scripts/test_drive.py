from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / ".claude" / "skills" / "gct-app" / "drive.py"
_spec = importlib.util.spec_from_file_location("drive_tool", _PATH)
drive = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drive)

DOGFOOD = "postgresql://nate:secret@localhost:5432/grounded_class_tutor?sslmode=disable"


def test_the_scratch_database_is_on_the_same_server_and_is_never_the_dogfood_one():
    assert (
        drive.with_database(DOGFOOD, drive.SCRATCH_DB)
        == "postgresql://nate:secret@localhost:5432/gct_drive?sslmode=disable"
    )
    assert drive.SCRATCH_DB != "grounded_class_tutor"


@pytest.mark.parametrize("url", ["host=localhost dbname=x", "sqlite:///x.db", "postgresql:///x"])
def test_a_url_it_cannot_redirect_is_refused_rather_than_used(url):
    with pytest.raises(drive.DriveError):
        drive.with_database(url, drive.SCRATCH_DB)


def test_the_app_it_starts_gets_stand_in_models_no_key_and_its_own_database_and_staging():
    env = drive.child_env(
        {"DATABASE_URL": DOGFOOD, "OPENAI_API_KEY": "sk-real", "GCT_PROVIDERS": "openai"},
        "SCRATCH",
        "/tmp/staging",
    )
    assert env == {
        "DATABASE_URL": "SCRATCH",
        "OPENAI_API_KEY": "",
        "GCT_PROVIDERS": "standin",
        "GCT_STAGING_DIR": "/tmp/staging",
    }


def test_a_missing_database_url_names_the_remedy(tmp_path):
    with pytest.raises(drive.DriveError, match="DATABASE_URL"):
        drive.source_url({}, tmp_path / "absent.env")
