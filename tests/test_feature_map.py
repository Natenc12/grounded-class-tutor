from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from gct.api import deps
from gct.api.app import app
from gct.api.routers import ask, classes, files, health
from gct.api.routers.files import _FAILED_MESSAGE, _STATUS_MESSAGE
from gct.grounder.answer import GrounderState

REPO = Path(__file__).resolve().parents[1]
MAP = REPO / ".claude" / "skills" / "gct-app" / "feature-map.md"
FIX = f"Update {MAP.relative_to(REPO)} so it describes the product as it is now."
PAID_PROVIDERS = {deps.get_embedder, deps.get_generator}
ROUTER_MODULES = (ask, classes, files, health)
PAID_IN_A_SCRIPT = re.compile(r"openai|gct\.providers\.active", flags=re.I)


def _entries() -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    for block in MAP.read_text(encoding="utf-8").split("\n### ")[1:]:
        fields = dict(re.findall(r"^- ([A-Za-z ]+): `?([^`\n]+)`?$", block, flags=re.M))
        assert fields.get("Reach") not in entries, f"Two entries reach {fields.get('Reach')}. {FIX}"
        entries[fields["Reach"]] = fields
    return entries


def _listed(label: str) -> set[str]:
    line = re.search(rf"^- {label}: (.+)$", MAP.read_text(encoding="utf-8"), flags=re.M)
    assert line, f"The map has no '{label}:' line. {FIX}"
    return set(re.findall(r"`([^`]+)`", line.group(1)))


def _routes() -> dict[str, APIRoute]:
    routes = {
        f"{method} {route.path}": route
        for module in ROUTER_MODULES
        for route in module.router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    published = {
        f"{method.upper()} {path}"
        for path, methods in app.openapi()["paths"].items()
        for method in methods
    }
    assert set(routes) == published, (
        f"The API publishes {sorted(published - set(routes))} from a router this test does not "
        "know. Add its module to ROUTER_MODULES in this file."
    )
    return routes


def _uses_paid_provider(route: APIRoute) -> bool:
    pending = list(route.dependant.dependencies)
    while pending:
        dependency = pending.pop()
        if dependency.call in PAID_PROVIDERS:
            return True
        pending.extend(dependency.dependencies)
    return False


def _scripts() -> dict[str, Path]:
    return {
        f"scripts/{path.name}": path
        for path in (REPO / "scripts").iterdir()
        if path.suffix in (".py", ".sh")
    }


def test_every_endpoint_and_script_is_on_the_map_and_nothing_else_is():
    real = set(_routes()) | set(_scripts())
    mapped = set(_entries())
    assert mapped == real, (
        f"Not on the map: {sorted(real - mapped)}. On the map but gone: {sorted(mapped - real)}. "
        f"{FIX}"
    )


def test_the_map_says_costs_money_exactly_where_a_paid_provider_is_used():
    paid = {reach for reach, route in _routes().items() if _uses_paid_provider(route)} | {
        reach
        for reach, path in _scripts().items()
        if path.suffix == ".py" and PAID_IN_A_SCRIPT.search(path.read_text(encoding="utf-8"))
    }
    claimed = {reach for reach, fields in _entries().items() if fields["Costs money"] == "yes"}
    assert claimed == paid, (
        f"Paid but mapped as free: {sorted(paid - claimed)}. Free but mapped as paid: "
        f"{sorted(claimed - paid)}. An agent reads this line to decide what is safe to run. {FIX}"
    )


def test_every_code_and_test_path_on_the_map_exists():
    missing = [
        f"{reach}: {fields[key]}"
        for reach, fields in _entries().items()
        for key in ("Code", "Tests")
        if fields[key] != "none" and not (REPO / fields[key]).is_file()
    ]
    assert not missing, f"The map points at files that do not exist: {missing}. {FIX}"


def test_the_outcomes_on_the_map_are_the_outcomes_the_code_can_produce():
    real = {
        "Answer states": {state.value for state in GrounderState},
        "Statuses": set(_STATUS_MESSAGE) | {"failed"},
        "Failure reasons": set(_FAILED_MESSAGE),
    }
    for label, values in real.items():
        mapped = _listed(label)
        assert mapped == values, (
            f"'{label}' on the map is missing {sorted(values - mapped)} and lists "
            f"{sorted(mapped - values)}, which the code cannot produce. {FIX}"
        )


def test_the_map_says_no_screens_only_while_the_web_client_has_none():
    screens = sorted(
        str(path.relative_to(REPO))
        for path in (REPO / "web" / "src").rglob("*.tsx")
        if path.name != "main.tsx" and ".test." not in path.name
    )
    assert _listed_plain("Screens") == ("none" if not screens else "listed"), (
        f"The web client now has screens ({screens}) but the map says it has none. Replace the "
        f"'Screens: none' line with an entry per screen. {FIX}"
    )


def _listed_plain(label: str) -> str:
    line = re.search(rf"^- {label}: (.+)$", MAP.read_text(encoding="utf-8"), flags=re.M)
    assert line, f"The map has no '{label}:' line. {FIX}"
    return line.group(1).strip()
