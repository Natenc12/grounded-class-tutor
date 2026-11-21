"""Tests for the PR risk gate: `.github/risk/risk_gate.py`, its rules file and its workflow (#211).

Offline and free. The GitHub API is replaced by a small fake that records what the gate writes, so
the whole flow (list files, set the label, pass or fail the check) runs without a network.
"""

from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RISK_DIR = ROOT / ".github" / "risk"
WORKFLOW = ROOT / ".github" / "workflows" / "risk-gate.yml"

_spec = importlib.util.spec_from_file_location("risk_gate", RISK_DIR / "risk_gate.py")
risk_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(risk_gate)

RULES = risk_gate.parse_rules((RISK_DIR / "rules.txt").read_text(encoding="utf-8"))
REPO = "Natenc12/grounded-class-tutor"


def tier(*paths: str) -> str:
    return risk_gate.pr_tier(paths, RULES)


# ---------------------------------------------------------------- the shipped rules


def test_rules_file_is_exactly_the_ticket_list():
    # Issue #211's list plus #218's desktop-move paths. Changing the rules is allowed, but must be
    # a deliberate edit here too.
    assert RULES == [
        ("high", "migrations/"),
        ("high", "src/gct/db.py"),
        ("high", "src/gct/jobs/"),
        ("high", "src/gct/grounder/"),
        ("high", "src/gct/ask*"),
        ("high", "src/gct/providers/"),
        ("high", ".github/"),
        ("high", "src/gct/library/"),
        ("high", "src/gct/search/"),
        ("high", "desktop/main*"),
        ("high", "desktop/preload*"),
        ("low", "docs/"),
        ("low", "design/"),
        ("low", "*.md"),
        ("low", "tests/"),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "migrations/0005_new.sql",
        "src/gct/db.py",
        "src/gct/jobs/worker.py",
        "src/gct/grounder/answer.py",
        "src/gct/ask.py",
        "src/gct/ask_helpers.py",
        "src/gct/ask/flow.py",
        "src/gct/providers/base.py",
        ".github/workflows/ci.yml",
        ".github/risk/rules.txt",
        ".github/risk/risk_gate.py",
        # #218: the desktop move's library, search and desktop main process
        "src/gct/library/x.py",
        "src/gct/search/x.py",
        "desktop/main.mjs",
        "desktop/preload.cjs",
        "desktop/main/window.mjs",
        "src/gct/library/README.md",  # high beats low
    ],
)
def test_high_paths(path):
    assert tier(path) == "high"


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "docs/guide.txt",
        "design/architecture.md",
        "design/diagrams/flow.svg",
        "tests/gct/test_classes.py",
        "eval/FINDINGS.md",
    ],
)
def test_low_paths(path):
    assert tier(path) == "low"


@pytest.mark.parametrize(
    "path",
    [
        "src/gct/cli.py",
        "scripts/migrate.py",
        "pyproject.toml",
        "uv.lock",
        ".gitignore",
        "src/gct/database.py",  # looks like db.py, is not
        "src/gct/db.py.bak",
        "migrations.txt",  # folder rules need the slash
        "srcx/gct/db.py",
        "README.md.txt",
        # #218: desktop screens stay medium; only main and preload are high
        "desktop/renderer/app.js",
        "desktop/renderer/main.js",  # named main, but not desktop/main*
        "src/gct/library.py",  # folder rules need the slash
    ],
)
def test_medium_paths(path):
    assert tier(path) == "medium"


def test_high_beats_low_inside_a_high_folder():
    assert tier("migrations/README.md") == "high"
    assert tier(".github/PULL_REQUEST_TEMPLATE.md") == "high"
    assert tier("src/gct/grounder/NOTES.md") == "high"


def test_mixed_pr_takes_the_highest_tier():
    assert tier("README.md", "tests/test_x.py", "docs/a.md") == "low"
    assert tier("README.md", "src/gct/cli.py") == "medium"
    assert tier("README.md", "src/gct/cli.py", "migrations/0005.sql") == "high"
    assert tier("tests/test_x.py", "src/gct/db.py") == "high"


def test_pr_with_no_files_is_low():
    assert tier() == "low"


def test_matching_is_case_sensitive_like_git():
    assert tier("Migrations/0005.sql") == "medium"
    assert tier("README.MD") == "medium"


# ---------------------------------------------------------------- parsing


def test_parse_rules_ignores_comments_and_blank_lines():
    assert risk_gate.parse_rules("# c\n\nhigh a/   # trailing\nlow *.md\n") == [
        ("high", "a/"),
        ("low", "*.md"),
    ]


@pytest.mark.parametrize("bad", ["medium src/", "high", "high a b", "hgh src/"])
def test_parse_rules_rejects_malformed_lines(bad):
    with pytest.raises(ValueError, match="line 1"):
        risk_gate.parse_rules(bad + "\nhigh x/\n")


def test_parse_rules_refuses_a_file_with_no_high_rules():
    with pytest.raises(ValueError, match="no 'high'"):
        risk_gate.parse_rules("# emptied\nlow docs/\n")


# ---------------------------------------------------------------- the gate, against a fake GitHub


class FakeGitHub:
    def __init__(self, files, labels=(), changed_files=None, head_repo=REPO, fail_writes=None):
        self.files = list(files)
        self.labels = list(labels)
        self.changed_files = len(self.files) if changed_files is None else changed_files
        self.head_repo = head_repo
        self.fail_writes = fail_writes
        self.writes: list[tuple[str, str, object]] = []

    def __call__(self, method, path, token, body=None):
        assert token == "tkn"
        if method == "GET" and path == f"/repos/{REPO}/pulls/7":
            head = {"repo": None if self.head_repo is None else {"full_name": self.head_repo}}
            return {
                "labels": [{"name": n} for n in self.labels],
                "changed_files": self.changed_files,
                "head": head,
            }
        if method == "GET" and path.startswith(f"/repos/{REPO}/pulls/7/files?"):
            query = dict(p.split("=") for p in path.split("?", 1)[1].split("&"))
            size, page = int(query["per_page"]), int(query["page"])
            return self.files[(page - 1) * size : page * size]
        if self.fail_writes:
            raise urllib.error.HTTPError(path, self.fail_writes, "nope", {}, io.BytesIO())
        self.writes.append((method, path, body))
        if method == "POST":
            self.labels += body["labels"]
        elif method == "DELETE":
            name = path.rsplit("/", 1)[1].replace("%3A", ":")
            self.labels.remove(name)
        return None


def f(name, previous=None):
    entry = {"filename": name}
    if previous:
        entry["previous_filename"] = previous
    return entry


def gate(gh):
    lines: list[str] = []
    code = risk_gate.run_gate(gh, REPO, 7, "tkn", RULES, out=lines.append)
    return code, "\n".join(lines)


def test_readme_only_pr_is_low_and_passes():
    gh = FakeGitHub([f("README.md")])
    code, out = gate(gh)
    assert code == 0
    assert gh.labels == ["risk:low"]
    assert gh.writes == [("POST", f"/repos/{REPO}/issues/7/labels", {"labels": ["risk:low"]})]


def test_migrations_pr_fails_until_owner_ok_then_passes_without_a_push():
    gh = FakeGitHub([f("migrations/0005_new.sql")])
    code, out = gate(gh)
    assert code == 1
    assert gh.labels == ["risk:high"]
    assert "add the owner-ok label to merge" in out

    gh.labels.append("owner-ok")  # the owner adds the label; same files, no new push
    code, out = gate(gh)
    assert code == 0
    assert sorted(gh.labels) == ["owner-ok", "risk:high"]


def test_owner_ok_does_nothing_on_low_or_medium():
    gh = FakeGitHub([f("src/gct/cli.py")], labels=["owner-ok"])
    assert gate(gh)[0] == 0
    assert sorted(gh.labels) == ["owner-ok", "risk:medium"]


def test_older_risk_label_is_replaced():
    gh = FakeGitHub([f("src/gct/db.py")], labels=["risk:low", "risk:medium", "in-progress"])
    gate(gh)
    assert sorted(gh.labels) == ["in-progress", "risk:high"]
    deleted = [p for m, p, _ in gh.writes if m == "DELETE"]
    assert deleted == [
        f"/repos/{REPO}/issues/7/labels/risk%3Alow",
        f"/repos/{REPO}/issues/7/labels/risk%3Amedium",
    ]


def test_correct_label_already_present_writes_nothing():
    gh = FakeGitHub([f("README.md")], labels=["risk:low"])
    assert gate(gh)[0] == 0
    assert gh.writes == []


def test_moving_a_high_file_out_of_its_folder_stays_high():
    gh = FakeGitHub([f("src/gct/storage.py", previous="src/gct/db.py")])
    assert gate(gh)[0] == 1
    assert gh.labels == ["risk:high"]


def test_every_page_of_files_is_read():
    files = [f(f"docs/page{i}.md") for i in range(250)] + [f("migrations/0009.sql")]
    gh = FakeGitHub(files)
    assert gate(gh)[0] == 1
    assert gh.labels == ["risk:high"]


def test_exactly_one_full_page_is_read_once():
    gh = FakeGitHub([f(f"docs/p{i}.md") for i in range(100)])
    assert gate(gh)[0] == 0
    assert gh.labels == ["risk:low"]


def test_more_files_than_github_will_list_is_high():
    gh = FakeGitHub([f(f"docs/p{i}.md") for i in range(3000)], changed_files=3500)
    code, out = gate(gh)
    assert code == 1
    assert gh.labels == ["risk:high"]
    assert "listed 3000 of 3500" in out


def test_files_missing_from_the_listing_is_high():
    gh = FakeGitHub([f("README.md")], changed_files=2)
    assert gate(gh)[0] == 1


def test_label_write_failure_on_own_repo_fails_the_check():
    gh = FakeGitHub([f("README.md")], fail_writes=403)
    with pytest.raises(urllib.error.HTTPError):
        gate(gh)


def test_fork_pr_cannot_be_labelled_but_is_still_gated():
    gh = FakeGitHub([f("src/gct/db.py")], head_repo="someone/fork", fail_writes=403)
    code, out = gate(gh)
    assert code == 1
    assert "::warning" in out
    assert "add the owner-ok label to merge" in out


def test_fork_pr_whose_fork_was_deleted_is_still_gated():
    gh = FakeGitHub([f("README.md")], head_repo=None, fail_writes=403)
    assert gate(gh)[0] == 0


def test_label_already_removed_by_someone_else_is_fine():
    gh = FakeGitHub([f("README.md")], labels=["risk:high"])
    real = gh.__call__

    def racing(method, path, token, body=None):
        if method == "DELETE":
            raise urllib.error.HTTPError(path, 404, "gone", {}, io.BytesIO())
        return real(method, path, token, body)

    assert risk_gate.run_gate(racing, REPO, 7, "tkn", RULES, out=lambda _: None) == 0


def test_classify_command(capsys):
    rules = str(RISK_DIR / "rules.txt")
    assert risk_gate.main(["--rules", rules, "classify", "README.md", "src/gct/cli.py"]) == 0
    assert capsys.readouterr().out.strip() == "medium"


# ---------------------------------------------------------------- the workflow file


WORKFLOW_TEXT = WORKFLOW.read_text(encoding="utf-8")


def test_workflow_check_is_named_risk_gate():
    # Branch protection will require this exact name.
    assert "\n  risk-gate:\n    # " in WORKFLOW_TEXT
    assert "\n    name: risk-gate\n" in WORKFLOW_TEXT


def test_workflow_runs_on_the_ticket_events():
    assert "  pull_request:\n" in WORKFLOW_TEXT
    assert "pull_request_target" not in WORKFLOW_TEXT.split("name: Risk gate", 1)[1]
    line = next(x for x in WORKFLOW_TEXT.splitlines() if x.strip().startswith("types:"))
    for event in ("opened", "synchronize", "labeled", "unlabeled"):
        assert event in line


def test_workflow_token_has_least_permissions():
    assert "\npermissions: {}\n" in WORKFLOW_TEXT
    perms = [x.split("#")[0].strip() for x in WORKFLOW_TEXT.splitlines()]
    granted = sorted(x for x in perms if x.endswith((": read", ": write")))
    assert granted == ["contents: read", "pull-requests: write"]
    assert "secrets." not in WORKFLOW_TEXT
    assert "${{ github.token }}" in WORKFLOW_TEXT


def test_workflow_reads_rules_from_the_base_commit():
    assert "ref: ${{ github.event.pull_request.base.sha }}" in WORKFLOW_TEXT
    assert "dir=base/.github/risk" in WORKFLOW_TEXT
    # The PR's own copy is checked out only when the base has none.
    assert "if: hashFiles('base/.github/risk/rules.txt') == ''" in WORKFLOW_TEXT
