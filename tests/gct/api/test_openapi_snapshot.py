"""The web client's committed OpenAPI snapshot is the API's live schema (issue #140, ADR 0032).

`web/openapi.json` is what the client's TypeScript types are generated from, so it is a derived
copy of the routers' pydantic models - and a copy with no pin drifts. This file is the half of
the pin CI runs today (CI runs pytest; the client's own tests are a local gate). The other half,
generated TS vs this snapshot, is `web/scripts/api-types.test.ts`.

The writer under test is `web/scripts/openapi_snapshot.py`, loaded by path: it is a script of the
client's, not a module of the `gct` package.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "web" / "scripts" / "openapi_snapshot.py"


def _load(path: Path = _SCRIPT) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"openapi_snapshot_{abs(hash(path))}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def snapshot() -> ModuleType:
    return _load()


def test_the_committed_snapshot_is_the_live_schema(snapshot):
    committed = snapshot.SNAPSHOT_PATH.read_text(encoding="utf-8")
    assert committed == snapshot.render(snapshot.live_schema()), (
        "web/openapi.json is not the API's published schema any more. Refresh the snapshot and "
        "the types generated from it with `cd web && npm run api:refresh`, and commit both."
    )


def test_prose_is_dropped_at_every_depth_and_nothing_else_is(snapshot):
    raw = {
        "openapi": "3.1.0",
        "info": {"title": "Grounded Class Tutor", "version": "0.1.0", "summary": "kept: info"},
        "paths": {
            "/things": {
                "summary": "path prose",
                "description": "path prose",
                "post": {
                    "summary": "operation prose",
                    "description": "a handler docstring",
                    "operationId": "make_thing",
                    "requestBody": {
                        "description": "request prose",
                        "content": {
                            "multipart/form-data": {
                                "schema": {"$ref": "#/components/schemas/Form"},
                                "encoding": {"title": {"contentType": "text/plain"}},
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Successful Response",
                            "content": {
                                "application/json": {"schema": {"title": "Thing", "type": "object"}}
                            },
                        }
                    },
                },
            }
        },
        "components": {
            "schemas": {
                "title": {"title": "A model named title", "type": "string"},
                "Thing": {
                    "title": "Thing",
                    "description": "a model docstring",
                    "type": "object",
                    "properties": {
                        "title": {"title": "Title", "type": "string"},
                        "description": {"description": "prose", "type": "string"},
                        "summary": {
                            "anyOf": [{"title": "member", "type": "string"}, {"type": "null"}]
                        },
                    },
                    "patternProperties": {"^description$": {"title": "x", "type": "string"}},
                    "$defs": {"summary": {"description": "prose", "type": "integer"}},
                    "required": ["title", "description"],
                },
                "Data": {
                    "type": "object",
                    "default": {"title": "a default is data"},
                    "enum": [{"description": "an enum member is data"}],
                    "const": {"summary": "a const is data"},
                    "example": {"title": "an example is data"},
                    "examples": [{"description": "examples are data"}],
                },
            }
        },
    }
    assert snapshot.normalize(raw) == {
        "openapi": "3.1.0",
        "info": {"title": "Grounded Class Tutor", "version": "0.1.0", "summary": "kept: info"},
        "paths": {
            "/things": {
                "post": {
                    "operationId": "make_thing",
                    "requestBody": {
                        "content": {
                            "multipart/form-data": {
                                "schema": {"$ref": "#/components/schemas/Form"},
                                "encoding": {"title": {"contentType": "text/plain"}},
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Successful Response",
                            "content": {"application/json": {"schema": {"type": "object"}}},
                        }
                    },
                },
            }
        },
        "components": {
            "schemas": {
                "title": {"type": "string"},
                "Thing": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "summary": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "patternProperties": {"^description$": {"type": "string"}},
                    "$defs": {"summary": {"type": "integer"}},
                    "required": ["title", "description"],
                },
                "Data": {
                    "type": "object",
                    "default": {"title": "a default is data"},
                    "enum": [{"description": "an enum member is data"}],
                    "const": {"summary": "a const is data"},
                    "example": {"title": "an example is data"},
                    "examples": [{"description": "examples are data"}],
                },
            }
        },
    }


def test_a_docstring_edit_does_not_change_the_snapshot(snapshot):
    """What the prose-stripping is FOR: the same shape with different prose normalizes equal."""
    before = {
        "components": {"schemas": {"M": {"description": "old", "title": "M", "type": "object"}}}
    }
    after = {
        "components": {"schemas": {"M": {"description": "new", "title": "M", "type": "object"}}}
    }
    assert snapshot.normalize(before) == snapshot.normalize(after)
    shape_changed = {"components": {"schemas": {"M": {"description": "old", "type": "array"}}}}
    assert snapshot.normalize(before) != snapshot.normalize(shape_changed)


def test_the_script_writes_the_snapshot_and_importing_it_does_not(snapshot, tmp_path):
    """Run as `npm run api:refresh` runs it - a script - from a copy, so its SNAPSHOT_PATH (which
    is relative to the file) lands in tmp_path rather than over the committed snapshot."""
    ran = tmp_path / "ran"
    imported = tmp_path / "imported"
    for root in (ran, imported):
        (root / "scripts").mkdir(parents=True)
        shutil.copy(_SCRIPT, root / "scripts" / _SCRIPT.name)

    _load(imported / "scripts" / _SCRIPT.name)
    assert not (imported / "openapi.json").exists()

    subprocess.run([sys.executable, str(ran / "scripts" / _SCRIPT.name)], check=True)
    written = (ran / "openapi.json").read_text(encoding="utf-8")
    assert written == snapshot.render(snapshot.live_schema())
