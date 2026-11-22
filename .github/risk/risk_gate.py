"""Label a pull request by risk from the paths it changes.

Issues #211 and #240. The check passes for every tier: the label shows the risk, and the owner
approves each merge himself. It fails only on a real fault, such as GitHub refusing to list the
files or set the label. Run by .github/workflows/risk-gate.yml on every pull request event.
Standard library only, so CI runs it with the runner's own python3 and installs nothing.

Two ways to run it:
  risk_gate.py --rules rules.txt gate            # CI: needs GITHUB_TOKEN, REPO, PR in the env
  risk_gate.py --rules rules.txt classify PATH…  # local: print the tier for a list of paths

The tier comes only from the rules file (see rules.txt for the format), never from anyone's
opinion of the change. The workflow passes the rules and this script from the branch the PR
merges into, so a PR cannot loosen the rules it is judged by.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from fnmatch import fnmatchcase
from pathlib import Path

TIERS = ("low", "medium", "high")
RISK_LABELS = tuple(f"risk:{t}" for t in TIERS)
# GitHub's "list pull request files" endpoint returns at most 3,000 files. Past that we cannot
# see every path, so we cannot prove the PR is not high: treat it as high.
GITHUB_FILE_LIMIT = 3000
PAGE_SIZE = 100

Rule = tuple[str, str]  # (tier, pattern)
Api = Callable[..., object]


# ---------------------------------------------------------------- rules (pure, unit-tested)


def parse_rules(text: str) -> list[Rule]:
    """Parse rules.txt. Malformed lines are an error, not skipped: a typo must not drop a rule."""
    rules: list[Rule] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] not in ("high", "low"):
            want = "'high <pattern>' or 'low <pattern>'"
            raise ValueError(f"rules line {number}: want {want}: {raw!r}")
        rules.append((parts[0], parts[1]))
    if not any(tier == "high" for tier, _ in rules):
        raise ValueError("rules have no 'high' line; refusing to run with an emptied rules file")
    return rules


def matches(pattern: str, path: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if "/" not in pattern:
        return fnmatchcase(path.rsplit("/", 1)[-1], pattern)
    return fnmatchcase(path, pattern)


def file_tier(path: str, rules: list[Rule]) -> str:
    if any(tier == "high" and matches(p, path) for tier, p in rules):
        return "high"
    if any(tier == "low" and matches(p, path) for tier, p in rules):
        return "low"
    return "medium"


def pr_tier(paths: Iterable[str], rules: list[Rule]) -> str:
    """Highest tier among the paths. A PR that changes no files is low."""
    return max((file_tier(p, rules) for p in paths), key=TIERS.index, default="low")


# ---------------------------------------------------------------- GitHub


def github_api(method: str, path: str, token: str, body: object | None = None) -> object:
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(
        base + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "risk-gate",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def changed_paths(api: Api, repo: str, pr: int, token: str) -> tuple[list[str], int]:
    """Every path the PR touches (a moved file under both names), and how many files were listed."""
    paths: list[str] = []
    listed = 0
    page = 1
    while listed < GITHUB_FILE_LIMIT:
        query = f"per_page={PAGE_SIZE}&page={page}"
        batch = api("GET", f"/repos/{repo}/pulls/{pr}/files?{query}", token)
        for entry in batch:
            listed += 1
            paths.append(entry["filename"])
            if entry.get("previous_filename"):
                paths.append(entry["previous_filename"])
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return paths, listed


def sync_risk_label(
    api: Api, repo: str, pr: int, token: str, want: str, current: list[str]
) -> None:
    """Make `want` the PR's only risk:* label."""
    for label in current:
        if label in RISK_LABELS and label != want:
            quoted = urllib.parse.quote(label, safe="")
            try:
                api("DELETE", f"/repos/{repo}/issues/{pr}/labels/{quoted}", token)
            except urllib.error.HTTPError as err:
                if err.code != 404:  # 404: someone removed it first, which is what we wanted
                    raise
    if want not in current:
        api("POST", f"/repos/{repo}/issues/{pr}/labels", token, {"labels": [want]})


def run_gate(api: Api, repo: str, pr: int, token: str, rules: list[Rule], out=print) -> int:
    """Label the PR and return the check's exit code: 0 for every tier. API faults raise."""
    info = api("GET", f"/repos/{repo}/pulls/{pr}", token)
    labels = [label["name"] for label in info["labels"]]
    paths, listed = changed_paths(api, repo, pr, token)

    if listed < info["changed_files"] or listed >= GITHUB_FILE_LIMIT:
        tier = "high"
        reason = f"GitHub listed {listed} of {info['changed_files']} changed files"
    else:
        tier = pr_tier(paths, rules)
        reason = f"{listed} changed files"
    want = f"risk:{tier}"
    out(f"{want} ({reason})")
    for path in sorted(set(paths)):
        out(f"  {file_tier(path, rules):6}  {path}")

    head_repo = (info.get("head") or {}).get("repo") or {}
    from_fork = head_repo.get("full_name") != repo
    try:
        sync_risk_label(api, repo, pr, token, want, labels)
    except urllib.error.HTTPError as err:
        # A PR from a fork gets a read-only token, so it cannot be labelled; the tier is still in
        # the log. On our own repo a failed label write is a real fault, so it fails the check.
        if not from_fork:
            raise
        out(f"::warning title=risk-gate::Could not set {want} on a PR from a fork ({err.code}).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rules", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gate", help="label the PR in $PR of $REPO by risk (CI)")
    classify = sub.add_parser("classify", help="print the tier for the given paths")
    classify.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)

    rules = parse_rules(args.rules.read_text(encoding="utf-8"))
    if args.command == "classify":
        print(pr_tier(args.paths, rules))
        return 0
    return run_gate(
        github_api, os.environ["REPO"], int(os.environ["PR"]), os.environ["GITHUB_TOKEN"], rules
    )


if __name__ == "__main__":
    sys.exit(main())
