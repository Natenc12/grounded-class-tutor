"""The error envelope's echo bound (issue #134): what a 422 hands back about the body it refused.

Pydantic puts the client's own rejected value into every error entry, and the renderer walks it.
Two live bugs came out of that, and they are one mechanism: one undecodable byte killed the
render INSIDE the handler, so a 422 arrived as a 500 with the per-field list gone; and a
multi-megabyte body labelled `multipart/form-data` - a label #125's request bound must let
through, or no upload ever lands - came back whole, 2,000,000 bytes in and 2,000,215 out.

THREE LAYERS, and the middle one is why this file is not just route tests. `_bounded` is pure and
takes the adversarial cases, because truncation is silent in both directions: too eager and an
ordinary rejected value is unreadable, too lax and the amplifier is still there. The probe pins
the render around a route with no database. The acceptance pins run the real `POST /classes`.

THE "STILL ECHOED WHOLE" PINS ARE LOAD-BEARING, not decoration. Every assertion about a bound is
satisfied by code that returns nothing at all: a renderer that dropped `input`, or truncated every
string to zero, passes each size check in here on its own. So each bound is asserted beside the
value it must NOT have touched, and the entry's key set is asserted with it - truncating bounds a
VALUE, and the shape `api.md` documents has to survive it.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, field_validator

from gct.api import errors, limits
from gct.api.errors import _bounded, _json_safe, envelope
from gct.config import MAX_ERROR_ECHO_CHARS

ENTRY_KEYS = {"type", "loc", "msg", "input"}
MARKER = "...[truncated from "


# --------------------------------------------------------------------------------------------
# The bound itself - pure
# --------------------------------------------------------------------------------------------


def test_a_string_at_the_bound_is_untouched() -> None:
    """The direction a size assertion cannot see. `_bounded` is green for `lambda _: ""` unless
    something pins that an admissible string comes back byte-for-byte."""
    at_the_bound = "a" * MAX_ERROR_ECHO_CHARS
    assert _bounded(at_the_bound) == at_the_bound
    assert _bounded("") == ""


def test_a_string_one_past_the_bound_is_truncated_and_says_how_long_it_was() -> None:
    over = "a" * (MAX_ERROR_ECHO_CHARS + 1)
    marked = _bounded(over)
    assert len(marked) == MAX_ERROR_ECHO_CHARS
    assert marked.startswith("aaaa")
    assert marked.endswith(f"{MARKER}{MAX_ERROR_ECHO_CHARS + 1} chars]")


def test_truncation_is_idempotent() -> None:
    """`_validation` runs `_json_safe` before the encoder and `render` runs it again over the
    finished envelope, so every string on the 422 path is bounded TWICE. A marker that pushed the
    result past the bound would be appended once per pass - and the second marker would report
    the length of the first truncation, not of the body the client sent."""
    once = _json_safe({"input": "a" * 10 * MAX_ERROR_ECHO_CHARS})
    assert _json_safe(once) == once
    assert once["input"].count(MARKER) == 1


def test_undecodable_bytes_render_as_text_rather_than_killing_the_render() -> None:
    """`json.dumps` raises `TypeError` on `bytes` and `jsonable_encoder`'s handler for them is
    `lambda o: o.decode()`, which raises `UnicodeDecodeError` on `0xff`. Both happen inside the
    exception handler, which is what turned the 422 into a 500."""
    assert _json_safe(b"\xff") == "�"
    assert _json_safe(b"ok") == "ok"
    assert len(_json_safe(b"\xff" * 10_000)) == MAX_ERROR_ECHO_CHARS
    json.dumps(_json_safe({"input": b"\xff"}))  # the render's own call; raises if bytes survive


def test_the_bound_reaches_every_string_in_a_nested_structure_including_keys() -> None:
    """`input` is not the only client-controlled place: an `extra="forbid"` model echoes the
    client's own KEY into `loc`, so a dict key is a client-sized string too."""
    huge = "k" * (MAX_ERROR_ECHO_CHARS + 1)
    safe = _json_safe({"loc": ["body", huge], "input": {huge: [huge]}})
    assert safe["loc"][0] == "body"
    assert len(safe["loc"][1]) == MAX_ERROR_ECHO_CHARS
    key = next(iter(safe["input"]))
    assert len(key) == MAX_ERROR_ECHO_CHARS
    assert len(safe["input"][key][0]) == MAX_ERROR_ECHO_CHARS


def test_a_non_finite_float_still_stringifies() -> None:
    """The hazard `_json_safe` was written for (`allow_nan=False` makes `json.dumps` RAISE on
    one), re-pinned here because this issue rewrote the function around it. The finite float
    beside them is the other direction: a pass that stringified every float would be green on
    the first two alone."""
    assert _json_safe({"input": [float("nan"), float("inf"), -float("inf"), 1.5]}) == {
        "input": ["nan", "inf", "-inf", 1.5]
    }


def test_an_ordinary_envelope_is_rendered_unchanged() -> None:
    """Nothing an adapter actually emits comes anywhere near the bound: the whole envelope for a
    real refusal must survive byte-for-byte, or the bound is a lie about every other response."""
    body = envelope(413, "body_too_large", "the body exceeds the 65,536-byte limit").body
    assert json.loads(body) == {
        "error": {
            "kind": "body_too_large",
            "message": "the body exceeds the 65,536-byte limit",
            "detail": None,
        }
    }


# --------------------------------------------------------------------------------------------
# The render, over a probe route - no database, no providers
# --------------------------------------------------------------------------------------------


class _Body(BaseModel):
    """`extra="forbid"` so a client key lands in `loc`, and a validator that RAISES so a `ctx`
    exception object reaches the encoder - the two client-controlled places besides `input`.

    Module-level, and that is load-bearing for the same reason `test_skeleton.py` gives: this
    file runs under `from __future__ import annotations`, and FastAPI resolves a route's string
    annotations against the module's globals."""

    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value


@pytest.fixture
def probe() -> TestClient:
    """An app wired exactly as `create_app` wires one - the real `errors.install` and the real
    `limits.install` - around one route with a JSON body. `test_limits.py`'s probe, for its
    reasons: these tests pin the RENDER, not any route's own validation, and need no database."""
    app = FastAPI()
    errors.install(app)
    limits.install(app)

    @app.post("/probe")
    def _probe(body: _Body) -> dict:
        return {"length": len(body.name)}

    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "content_type",
    [None, "application/octet-stream", "multipart/form-data", "text/plain"],
)
def test_one_undecodable_byte_is_a_422_with_its_field_list(
    probe: TestClient, content_type: str | None
) -> None:
    """The reported 500, in all four ways a body can reach pydantic un-parsed. Both halves are
    asserted: the STATUS (it was 500) and the per-field list (it was gone) - a handler that
    returned an empty 422 would satisfy the status alone."""
    headers = {} if content_type is None else {"content-type": content_type}
    response = probe.post("/probe", content=b"\xff", headers=headers)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["kind"] == "validation"
    entry = error["detail"][0]
    assert set(entry) >= ENTRY_KEYS
    assert entry["loc"] == ["body"]
    assert entry["msg"]
    assert entry["input"] == "�"  # the byte, described - not dropped, not fatal


def test_a_multi_megabyte_body_is_refused_without_being_echoed_back(probe: TestClient) -> None:
    """The amplifier. `multipart/form-data` is exempt from #125's request bound by design (an
    upload IS the body), so this is the one label that still reaches pydantic at any size."""
    body = b"x" * 2_000_000
    response = probe.post("/probe", content=body, headers={"content-type": "multipart/form-data"})

    assert response.status_code == 422
    assert len(response.content) < 4096, "the 422 is the size of the request again"
    entry = response.json()["error"]["detail"][0]
    assert entry["loc"] == ["body"]  # the actionable part survived the bound
    assert entry["input"].startswith("xxxx")
    assert entry["input"].endswith(f"{MARKER}{len(body)} chars]")


def test_an_ordinary_rejected_value_is_echoed_whole(probe: TestClient) -> None:
    """The guard on all of the above: a small `input` is handed back exactly as sent, with the
    entry's shape intact. Every bound in this file is green against a renderer that drops
    `input`; this is the test that is not."""
    response = probe.post("/probe", json={"nope": 1, "why": "because"})

    assert response.status_code == 422
    entry = response.json()["error"]["detail"][0]
    assert set(entry) >= ENTRY_KEYS
    assert entry["input"] == {"nope": 1, "why": "because"}
    assert MARKER not in json.dumps(entry)


def test_a_client_sized_key_lands_in_loc_and_is_bounded_there(probe: TestClient) -> None:
    """`extra="forbid"` puts the client's own key into `loc`. Measured on `main`: a 5,000-character
    key produced a 5,012-character `loc` entry, which is why the bound is on every string and not
    on the `input` key alone."""
    key = "k" * 5_000
    response = probe.post("/probe", json={"name": "ok", key: 1})

    assert response.status_code == 422
    entries = response.json()["error"]["detail"]
    locs = [entry["loc"] for entry in entries if entry["loc"][:1] == ["body"]]
    assert locs, entries
    assert all(len(part) <= MAX_ERROR_ECHO_CHARS for loc in locs for part in loc)
    assert len(response.content) < 4096


def test_a_raising_validator_still_renders_after_the_reordering(probe: TestClient) -> None:
    """`_json_safe` now runs BEFORE `jsonable_encoder` in `_validation`. The encoder is still what
    makes a `ctx` EXCEPTION OBJECT serialisable - the reason it is called at all - so the new
    order must not have cost that. `test_skeleton.py` pins the same shape against the untouched
    envelope; this one pins that the shape survived the reordering."""
    response = probe.post("/probe", json={"name": "   "})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["kind"] == "validation"
    entry = error["detail"][0]
    assert entry["loc"] == ["body", "name"]
    assert "name must not be blank" in entry["msg"]


# --------------------------------------------------------------------------------------------
# Acceptance, on the real route
# --------------------------------------------------------------------------------------------


def test_an_undecodable_body_is_a_422_on_classes(api) -> None:
    """The ticket's first measurement, on the route it was measured against."""
    response = api.client.post("/classes", content=b"\xff")

    assert response.status_code == 422
    assert response.json()["error"]["kind"] == "validation"
    assert response.json()["error"]["detail"][0]["loc"] == ["body"]


def test_a_multi_megabyte_multipart_body_is_not_echoed_back_by_classes(api) -> None:
    """The ticket's second measurement: 2,000,000 bytes in, 2,000,215 bytes out."""
    body = b"x" * 2_000_000
    response = api.client.post(
        "/classes", content=body, headers={"content-type": "multipart/form-data"}
    )

    assert response.status_code == 422
    assert len(response.content) < 4096
    assert response.json()["error"]["detail"][0]["loc"] == ["body"]


def test_a_real_rejected_class_name_is_still_echoed_whole(api) -> None:
    """The other direction on the real route: an ordinary mistake still gets its value back."""
    response = api.client.post("/classes", json={"nome": "Philosophy 101"})

    assert response.status_code == 422
    entry = response.json()["error"]["detail"][0]
    assert entry["loc"] == ["body", "name"]
    assert entry["input"] == {"nome": "Philosophy 101"}
