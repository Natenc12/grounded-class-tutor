"""The committed snapshot of the API's published OpenAPI schema, and its one writer (ADR 0033).

`web/openapi.json` is what the client's TypeScript types are generated from
(`web/scripts/api-types.mjs`). It is `app.openapi()` with the prose taken out: FastAPI copies
every handler's and every model's docstring into the schema as `description`, and every field
name into a `title`, so a snapshot that kept them would change on every docstring edit while the
generated types did not. What is left is the contract's shape.

Two callers: `npm run api:refresh` runs this file to rewrite the snapshot, and
`tests/gct/api/test_openapi_snapshot.py` imports it to assert that the committed file is exactly
what this module would write from the live app. That test is the half of the drift pin CI runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "openapi.json"

# Keywords that hold prose, dropped wherever they appear as a KEYWORD.
PROSE_KEYWORDS = frozenset({"description", "summary", "title"})

# Keywords whose value is DATA - an example, a default, the members of an enum - copied as is.
# `info` is here too: its `title` and `version` are required by OpenAPI and do not churn.
VERBATIM_KEYWORDS = frozenset({"info", "default", "enum", "const", "example", "examples"})

# Keywords whose value maps a NAME the API chose to an object. The names are kept even when one
# is spelt like a prose keyword: a property called `title` is a field on the wire, not prose.
NAME_MAP_KEYWORDS = frozenset(
    {"paths", "schemas", "properties", "patternProperties", "$defs", "content", "encoding"}
)


def normalize(node: Any) -> Any:
    """`node` with every prose keyword removed and everything else unchanged, in order."""
    if isinstance(node, list):
        return [normalize(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in PROSE_KEYWORDS:
            continue
        if key in VERBATIM_KEYWORDS:
            out[key] = value
        elif key == "responses":
            out[key] = {status: _response(response) for status, response in value.items()}
        elif key in NAME_MAP_KEYWORDS:
            out[key] = {name: normalize(child) for name, child in value.items()}
        else:
            out[key] = normalize(value)
    return out


def _response(response: dict[str, Any]) -> dict[str, Any]:
    # A Response Object's `description` is REQUIRED by OpenAPI, so it survives: dropping it would
    # make the snapshot an invalid document. FastAPI fills it with a fixed phrase ("Successful
    # Response"), not a docstring, so keeping it costs no churn.
    return {"description": response["description"], **normalize(response)}


def live_schema() -> dict[str, Any]:
    """The normalized schema of the app uvicorn serves. Needs no key and no database."""
    from gct.api.app import create_app

    return normalize(create_app().openapi())


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    SNAPSHOT_PATH.write_text(render(live_schema()), encoding="utf-8")
    print(f"wrote {SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
