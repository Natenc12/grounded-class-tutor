from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verify.py"
_spec = importlib.util.spec_from_file_location("verify_script", _PATH)
verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify)

DOGFOOD = "postgresql://nate:secret@localhost:5432/grounded_class_tutor?sslmode=disable"


def test_scratch_url_keeps_the_server_and_swaps_only_the_database():
    assert (
        verify.with_database(DOGFOOD, verify.SCRATCH_DB)
        == "postgresql://nate:secret@localhost:5432/gct_verify?sslmode=disable"
    )


def test_scratch_database_is_never_the_dogfood_one():
    assert verify.SCRATCH_DB != "grounded_class_tutor"
    assert "grounded_class_tutor" not in verify.with_database(DOGFOOD, verify.SCRATCH_DB)


@pytest.mark.parametrize("url", ["host=localhost dbname=x", "sqlite:///x.db", "postgresql:///x"])
def test_a_url_it_cannot_redirect_is_refused_rather_than_used(url):
    with pytest.raises(verify.VerifyError):
        verify.with_database(url, verify.SCRATCH_DB)


def test_children_get_the_scratch_database_and_no_api_key():
    env = verify.child_env(
        {"DATABASE_URL": DOGFOOD, "OPENAI_API_KEY": "sk-real", "HOME": "/h"}, "S"
    )
    assert env == {"DATABASE_URL": "S", "OPENAI_API_KEY": "", "HOME": "/h"}


def test_environment_wins_over_dotenv_and_missing_url_names_the_remedy(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("DATABASE_URL=postgresql://u@h/from_file\n")
    assert verify.source_url({"DATABASE_URL": "postgresql://u@h/from_env"}, dotenv).endswith(
        "from_env"
    )
    assert verify.source_url({}, dotenv).endswith("from_file")
    with pytest.raises(verify.VerifyError, match="DATABASE_URL"):
        verify.source_url({}, tmp_path / "absent.env")


def test_every_test_step_excludes_paid_tests():
    pytest_steps = [cmd for _, cmd in verify.steps() if "pytest" in cmd]
    assert pytest_steps
    for cmd in pytest_steps:
        assert cmd[cmd.index("-m", 2) + 1] == "not live"
