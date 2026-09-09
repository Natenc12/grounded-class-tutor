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
from gct.api.errors import (
    _DETAIL_TRUNCATED,
    _bounded,
    _capped_detail,
    _json_safe,
    _serialise,
    envelope,
)
from gct.config import MAX_ERROR_DETAIL_BYTES, MAX_ERROR_ECHO_CHARS

ENTRY_KEYS = {"type", "loc", "msg", "input"}
MARKER = "...[truncated from "


def _fake_entry(index: int) -> dict:
    """One pydantic-shaped `extra_forbidden` entry, of a length that does not depend on `index`
    (six padded digits), so a list of them has an exactly computable serialised size."""
    return {
        "type": "extra_forbidden",
        "loc": ["body", f"k{index:06d}"],
        "msg": "Extra inputs are not permitted",
        "input": 1,
    }


def _post_probe(body: dict) -> list:
    """One `POST /probe` with a JSON body, returning the 422's `detail`. Its own app rather than
    the `probe` fixture so a module-level test can use it too; wired exactly as `create_app`
    wires one, and `test_errors`'s probe route model."""
    app = FastAPI()
    errors.install(app)
    limits.install(app)

    @app.post("/probe")
    def _probe(body: _Body) -> dict:
        return {"length": len(body.name)}

    response = TestClient(app, raise_server_exceptions=False).post("/probe", json=body)
    assert response.status_code == 422, response.text
    return response.json()["error"]["detail"]


def _amplifier_body(keys: int) -> bytes:
    """The ticket's own request: a VALID `name`, plus `keys` unexpected keys. Every entry it
    produces is ~100 bytes, so the list is long rather than any one entry being large - measured
    on `main` at 5,537 keys: 65,347 bytes in, 547,133 out (issue #137)."""
    body = {"name": "x", **{f"k{i}": 1 for i in range(keys)}}
    return json.dumps(body).encode()


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


def test_the_bound_holds_even_when_the_marker_does_not_fit_inside_it(monkeypatch) -> None:
    """The degenerate bound, which the shipped value hides. Budgeting for the marker floors at 0
    below `len(marker)`, so without the final clamp `_bounded` returns the marker alone - LONGER
    than the bound it was asked for. `MAX_ERROR_ECHO_CHARS` is PROVISIONAL, so the only thing
    keeping the unclamped form correct was a number a future retune is invited to change."""
    monkeypatch.setattr(errors, "MAX_ERROR_ECHO_CHARS", 10)
    assert len(_bounded("a" * 20)) <= 10
    # The other arm: an admissible string at that bound still comes back byte-for-byte, so a
    # `lambda _: ""` cannot satisfy the assertion above.
    assert _bounded("a" * 10) == "a" * 10


def test_truncation_is_idempotent_at_a_bound_below_the_marker(monkeypatch) -> None:
    """Idempotence is a COROLLARY of the bound holding, so it fails wherever the bound does: a
    result over the bound gets truncated and marked again by the second pass."""
    monkeypatch.setattr(errors, "MAX_ERROR_ECHO_CHARS", 10)
    once = _bounded("a" * 20)
    assert _bounded(once) == once
    # Stability alone is a WEAK pin: `f(f(x)) == f(x)` is trivially true of the identity function
    # and of any constant, so the two lines above stay green against a `_bounded` that does
    # nothing at all. The fixed point has to be pinned by VALUE for this test to mean what its
    # name says.
    assert once == f"{MARKER}20 chars]"[:10]


def test_the_clamp_is_a_no_op_at_the_shipped_bound() -> None:
    """Byte-for-byte: the clamp is correctness at a bound nobody has set, never a behavior change
    at the one everybody gets. `keep + len(marker) == MAX_ERROR_ECHO_CHARS` by construction here,
    so clamping to `MAX_ERROR_ECHO_CHARS` removes nothing."""
    over = "a" * (MAX_ERROR_ECHO_CHARS * 3)
    marker = f"{MARKER}{len(over)} chars]"
    unclamped = over[: max(0, MAX_ERROR_ECHO_CHARS - len(marker))] + marker
    assert _bounded(over) == unclamped


def test_a_client_can_send_marker_shaped_text_and_the_echo_is_still_bounded() -> None:
    """What the bound does NOT promise. The surviving prefix is the client's own text, so a
    client who sends marker-shaped bytes gets them back beside the real marker and a reader
    cannot tell which is which. Authenticating the marker would take dropping `input`, the shape
    change `_json_safe`'s docstring records as rejected. Pinned so the weaker promise is the one
    on file: the SIZE is guaranteed, the marker's provenance is not."""
    forged = "X" * 400 + f"{MARKER}12345 chars]" + "Y" * 400
    bounded = _bounded(forged)
    assert len(bounded) == MAX_ERROR_ECHO_CHARS
    assert bounded.count(MARKER) == 2
    assert bounded.endswith(f"{MARKER}{len(forged)} chars]")


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


# --------------------------------------------------------------------------------------------
# The detail bound (issue #137) - the same three layers, one axis over
# --------------------------------------------------------------------------------------------
#
# `MAX_ERROR_ECHO_CHARS` bounds each VALUE and cannot see this: an `extra="forbid"` model emits
# one entry per unexpected key, the CLIENT picks how many keys to send, and every string in the
# result is far under 512 characters. So the bound here is on the SHAPE's total size, and the
# tests come in the same pairs #134's do - a size assertion is green for code that returns
# nothing, so each one is asserted beside what must NOT have been touched.


def test_a_detail_list_under_the_cap_is_the_same_list_object() -> None:
    """The direction every size assertion below is blind to. Identity, not equality: an ordinary
    422 must come back as the list pydantic built, not a copy the cap rebuilt."""
    detail = [_fake_entry(0), _fake_entry(1)]
    assert _capped_detail(detail) is detail


def test_the_longest_list_that_fits_is_untouched_and_one_entry_more_is_marked() -> None:
    """The boundary, from both sides, computed rather than guessed: `k` is the largest number of
    fixed-size entries whose serialised list fits, so `k` must survive whole and `k + 1` must
    not. A cap that never fires fails the second half; one that fires early fails the first."""
    size = len(_serialise(_fake_entry(0)))
    fits = [_fake_entry(i) for i in range((MAX_ERROR_DETAIL_BYTES - 1) // (size + 1))]
    over = [*fits, _fake_entry(len(fits))]

    assert len(_serialise(fits)) <= MAX_ERROR_DETAIL_BYTES < len(_serialise(over))
    assert _capped_detail(fits) is fits

    capped = _capped_detail(over)
    assert capped[-1]["type"] == _DETAIL_TRUNCATED
    assert len(_serialise(capped)) <= MAX_ERROR_DETAIL_BYTES


@pytest.mark.parametrize("count", [200, 1_000, 5_537, 20_000])
def test_the_cap_holds_and_the_dropped_count_is_exact(count: int) -> None:
    """The two halves of the contract, at four lengths: serialised `detail` is inside the cap,
    and `kept + dropped` accounts for every entry pydantic produced - an off-by-one in the count
    is a lie about how many fields the client got told about."""
    detail = [_fake_entry(i) for i in range(count)]
    capped = _capped_detail(detail)

    assert len(_serialise(capped)) <= MAX_ERROR_DETAIL_BYTES
    marker = capped[-1]
    assert marker["type"] == _DETAIL_TRUNCATED
    assert len(capped) - 1 + marker["ctx"]["dropped"] == count
    assert str(marker["ctx"]["dropped"]) in marker["msg"]
    assert 0 < len(capped) - 1 < count, "a cut, not a wipe and not a no-op"


def test_the_surviving_entries_are_the_head_of_the_list_unmodified() -> None:
    """The entries are pydantic's own objects, in order, with `loc` intact. A cap that rebuilt or
    reordered entries - or that trimmed the nested `loc` list to save bytes - would corrupt the
    field path, which is worse than the amplification this exists to remove."""
    detail = [_fake_entry(i) for i in range(5_000)]
    capped = _capped_detail(detail)
    kept = capped[:-1]

    assert all(kept[i] is detail[i] for i in range(len(kept)))
    assert [entry["loc"] for entry in kept] == [["body", f"k{i:06d}"] for i in range(len(kept))]


def test_an_entry_whose_loc_is_long_is_dropped_whole_rather_than_shortened() -> None:
    """`loc` is a list INSIDE an entry, and the cap must not reach it. One entry with a 4,000-part
    path is over the cap on its own: the honest outcome is that it is gone, not that it came back
    with a shorter path pointing at a field the client never sent."""
    entry = {
        "type": "missing",
        "loc": ["body", *[f"p{i}" for i in range(4_000)]],
        "msg": "",
        "input": None,
    }
    capped = _capped_detail([entry, _fake_entry(0)])

    assert capped == [_detail_marker_shape(2)]
    assert len(_serialise(capped)) <= MAX_ERROR_DETAIL_BYTES


def _detail_marker_shape(dropped: int) -> dict:
    """The marker as a test expectation - built here rather than imported from `errors`, so an
    accidental change to its shape is a failure rather than a rewritten expectation."""
    return {
        "type": "gct.detail_truncated",
        "loc": ["detail"],
        "msg": (
            f"{dropped} more validation error(s) are not shown: the detail list exceeded "
            f"{MAX_ERROR_DETAIL_BYTES} bytes. Send a smaller request to see them all."
        ),
        "input": None,
        "ctx": {"dropped": dropped},
    }


def test_the_marker_carries_the_keys_a_client_iterating_detail_reads(probe: TestClient) -> None:
    """The shape contract, both hazards at once: it must not MISS a key every entry has (a naive
    `entry["loc"]` / `entry["msg"]` must work), and it must not IMPERSONATE a pydantic entry (a
    client switching on `type` or reading a field path must not mistake it for one).

    The key set is compared against REAL entries rather than against `ENTRY_KEYS` alone, so the
    claim stays true of whatever pydantic and FastAPI actually emit here.

    TWO SAMPLES, and the second is the point. Sampling only `extra_forbidden` is what let this
    docstring say for four commits that `type`/`loc`/`msg`/`input` were "the whole set here, not a
    subset" - false, and unfalsifiable from one sample. A RAISING VALIDATOR produces `value_error`
    and pydantic attaches `ctx`; so does a `Field(max_length=...)`. Both live on the real
    `AskRequest`, so this is a shape a client meets. What the entries agree on is the FLOOR (the
    four), which is the property the marker has to match; what varies above it is why the marker
    must be discriminated on `type` and never on its keys. `url` is absent from both, because
    FastAPI builds the list with `include_url=False`."""
    forbidden = probe.post("/probe", json={"name": "ok", "k": 1}).json()["error"]["detail"][0]
    raised = probe.post("/probe", json={"name": "   "}).json()["error"]["detail"][0]
    marker = _capped_detail([_fake_entry(i) for i in range(1_000)])[-1]

    assert forbidden["type"] == "extra_forbidden" and raised["type"] == "value_error", (
        f"the two shapes this test exists to compare. got {forbidden['type']}, {raised['type']}"
    )
    assert set(forbidden) == ENTRY_KEYS, "the entry shape this marker has to match"
    assert set(raised) == ENTRY_KEYS | {"ctx"}, (
        "a raising validator's entry carries `ctx`, so the four keys are a FLOOR and not the "
        f"whole set - the sentence this test exists to keep honest. got {sorted(raised)}"
    )
    assert "url" not in forbidden and "url" not in raised, (
        "FastAPI stopped passing include_url=False; the marker now omits a key real entries have"
    )
    assert set(marker) >= set(forbidden) and set(marker) >= set(raised), (
        f"the marker must not MISS a key a real entry has. got {sorted(marker)}"
    )
    assert marker["input"] is None
    assert marker["loc"] == ["detail"], (
        "rooted at the response's own detail, never at a request part"
    )
    assert "." in marker["type"], "namespaced, so it cannot collide with a pydantic error type"


def test_only_one_marker_is_ever_added_and_it_is_last() -> None:
    """The contract a client relies on to find it. Re-capping an already-capped list is the
    adversarial case: the cap must not stack a second marker on top of the first."""
    once = _capped_detail([_fake_entry(i) for i in range(5_000)])
    twice = _capped_detail(once)

    assert twice is once, "the capped list is already inside the cap"
    assert [entry["type"] for entry in once].count(_DETAIL_TRUNCATED) == 1
    assert once[-1]["type"] == _DETAIL_TRUNCATED


def test_the_cap_degrades_to_the_marker_alone_below_the_marker_s_own_size(monkeypatch) -> None:
    """`MAX_ERROR_DETAIL_BYTES` is PROVISIONAL, so the values that make the guarantee exact are
    exactly the ones a future retune may change. Two of them: at a cap the marker fits inside,
    the bound holds; below that the result is the marker alone and exceeds the cap - a list has
    no equivalent of `_bounded`'s final clamp, because a JSON array cut mid-token is unparseable.
    Both halves are asserted, since a cap that emitted an EMPTY list would satisfy the size."""
    detail = [_fake_entry(i) for i in range(50)]

    monkeypatch.setattr(errors, "MAX_ERROR_DETAIL_BYTES", 400)
    assert len(_serialise(_capped_detail(detail))) <= 400

    monkeypatch.setattr(errors, "MAX_ERROR_DETAIL_BYTES", 10)
    floor = _capped_detail(detail)
    assert len(floor) == 1 and floor[0]["ctx"]["dropped"] == 50
    assert json.loads(_serialise(floor)) == floor, "still valid JSON, over the cap by the marker"


# --- the render, over a probe route ---------------------------------------------------------


def test_the_issue_s_own_5537_key_request_no_longer_amplifies(probe: TestClient) -> None:
    """The ticket's measurement: 65,347 bytes in bought 547,133 out. The ceiling asserted here is
    ABSOLUTE (32 KiB), not derived from the cap, so raising `MAX_ERROR_DETAIL_BYTES` to a number
    that lets the amplifier back through fails this test rather than moving with it."""
    body = _amplifier_body(5_537)
    response = probe.post("/probe", content=body, headers={"content-type": "application/json"})

    assert len(body) == 65_347, "the ticket's request, byte for byte"
    assert response.status_code == 422
    assert len(response.content) < 32 * 1024
    assert len(response.content) < len(body), "no amplification at all, let alone 8.4x"

    detail = response.json()["error"]["detail"]
    assert len(detail) - 1 + detail[-1]["ctx"]["dropped"] == 5_537
    assert 100 < len(detail) - 1 < 300, "~160 entries survive at the shipped cap"
    assert [entry["loc"] for entry in detail[:-1]] == [
        ["body", f"k{i}"] for i in range(len(detail) - 1)
    ]


def test_an_ordinary_422_is_returned_whole_with_no_marker(probe: TestClient) -> None:
    """The other direction, at an absolute size a real client actually sends: three unexpected
    keys come back as three entries, byte for byte, with nothing appended. A cap tuned small
    enough to catch the amplifier by mangling ordinary 422s fails here."""
    response = probe.post("/probe", json={"name": "ok", "owner_id": "me", "k": 1, "extra": True})

    assert response.status_code == 422
    detail = response.json()["error"]["detail"]
    assert [entry["loc"] for entry in detail] == [
        ["body", "owner_id"],
        ["body", "k"],
        ["body", "extra"],
    ]
    assert all(entry["type"] == "extra_forbidden" for entry in detail)
    assert _DETAIL_TRUNCATED not in _serialise(detail)


def test_a_routes_own_list_detail_is_bounded_by_the_same_render(probe: TestClient) -> None:
    """SCOPE: the bound is on the ENVELOPE, not on the 422. `render` is the one place that sees a
    finished envelope, so a route that grows a list-valued `detail` later is covered without
    knowing this exists - which is the reason the issue put it here rather than in `_validation`.
    """
    huge = envelope(
        400, "some_future_kind", "a message", detail=[_fake_entry(i) for i in range(5_000)]
    )
    body = json.loads(huge.body)

    assert body["error"]["kind"] == "some_future_kind"
    assert len(_serialise(body["error"]["detail"])) <= MAX_ERROR_DETAIL_BYTES
    assert body["error"]["detail"][-1]["type"] == _DETAIL_TRUNCATED


def test_a_detail_that_is_not_a_list_is_left_alone() -> None:
    """The cap matches on SHAPE, so the envelopes whose `detail` is a dict, a string or absent
    are untouched by it - including the 413 the body bound emits on every route."""
    for detail in (None, {"limit_bytes": 65_536}, "a sentence"):
        body = json.loads(envelope(400, "kind", "message", detail=detail).body)
        assert body["error"]["detail"] == detail


# --- acceptance, on the real route ----------------------------------------------------------


def test_the_amplifier_is_gone_on_classes(api) -> None:
    """The ticket measured `POST /classes`; `NewClass` is `extra="forbid"` for its own reasons
    (`routers/classes.py`), and this pins that the fix reaches the real route and not just the
    probe."""
    body = _amplifier_body(5_537)
    response = api.client.post(
        "/classes", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 422
    assert len(response.content) < 32 * 1024
    detail = response.json()["error"]["detail"]
    assert len(detail) - 1 + detail[-1]["ctx"]["dropped"] == 5_537
    assert detail[0]["loc"] == ["body", "k0"]


def test_a_real_rejected_class_body_still_comes_back_whole(api) -> None:
    """The other direction on the real route: an ordinary typo still gets every entry, and the
    marker is nowhere in the response."""
    response = api.client.post("/classes", json={"nome": "Philosophy 101", "ownerId": "me"})

    assert response.status_code == 422
    detail = response.json()["error"]["detail"]
    assert [entry["loc"] for entry in detail] == [
        ["body", "name"],
        ["body", "nome"],
        ["body", "ownerId"],
    ]
    assert _DETAIL_TRUNCATED not in response.text


def test_a_routes_list_detail_is_capped_only_after_its_leaves_are_renderable() -> None:
    """ORDER: `_capped_detail` MEASURES with `allow_nan=False`, so it has to run after
    `_json_safe` has turned the leaves it cannot serialise into strings. A route's own list
    `detail` is the only path that reaches `render` without `_validation`'s pass, so it is the
    one place the order is observable - and reversing it raises inside the handler, which is the
    500 this module exists to stop."""
    for value, rendered in (
        (float("nan"), "nan"),
        (float("inf"), "inf"),
        (b"\xff\xfe", "��"),
    ):
        body = json.loads(envelope(400, "kind", "message", detail=[{"input": value}]).body)
        assert body["error"]["detail"] == [{"input": rendered}]


@pytest.mark.parametrize("count", [500, 5_000, 12_345])
def test_the_cap_is_exact_at_every_entry_width(count: int) -> None:
    """The reserve/budget arithmetic is exact, and an off-by-one in it is only visible at the
    entry widths where the budget runs out ON an entry boundary rather than mid-entry. Sweeping
    the width walks every residue instead of landing on one by luck: dropping the `+ 1` from
    `reserve`, or a bracket from `budget`, both return 16,385 bytes at some width in here and are
    invisible at the width `_fake_entry` happens to have. (The padding floors at the entry's own
    natural width, so the low end of the sweep repeats the narrowest entry - harmless, and the
    residues that matter are all walked above it.)

    TWO AXES, because `reserve` depends on the marker and the marker spells the dropped count out
    in DECIMAL. Each count is here to walk one width of that number, against every entry width:
    500 drops a three-digit count, 5,000 a four-digit one, 12,345 a five-digit one. Sweeping width
    alone left a fourth slip alive - `reserve` computed at a hardcoded `_detail_marker(9999)` -
    which is correct up to 9,999 dropped and one byte short past it, and returned 16,385 bytes at
    count=12,345, width=117 with the rest of the file green.

    This pins that the cap is never EXCEEDED. That the space left under it is actually used is a
    separate property and a separate test - see the exact-fit one below, which is what a `>=`
    slip in the fill condition fails."""
    for width in range(34, 200):
        entry = {"type": "extra_forbidden", "loc": ["body", "k"], "msg": "", "input": ""}
        entry["msg"] = "m" * (width - len(_serialise(entry)))
        capped = _capped_detail([entry for _ in range(count)])

        assert capped[-1]["type"] == _DETAIL_TRUNCATED
        assert len(_serialise(capped)) <= MAX_ERROR_DETAIL_BYTES, f"width={width}"
        assert len(capped) > 1, f"width={width}: a cut, not a wipe"


def test_an_entry_that_exactly_fills_the_space_the_cap_left_is_kept() -> None:
    """MAXIMALITY: the cap has to be a BOUND, not an arbitrary early stop. `>` weakened to `>=`
    in the fill condition keeps one fewer entry than fits, and every size assertion in this file
    stays green under it - keeping too few is still under the cap.

    So this measures the slack a real run left and then hands the same run an entry sized to fill
    it EXACTLY, which is the one input where `>` and `>=` disagree. Nothing here re-derives the
    implementation's budget: the slack is read off a rendered list, and the only arithmetic is
    JSON's own - one byte for the comma that joins the entry to the ones before it.

    The list length is held constant across the two runs so the reserve is identical, and the
    dropped count stays four digits either side, so the marker cannot change width underneath the
    measurement."""
    base = {"type": "extra_forbidden", "loc": ["body", "k"], "msg": "", "input": ""}
    floor = len(_serialise(base))

    for width in range(floor, floor + 200):
        entries = [{**base, "msg": "m" * (width - floor)} for _ in range(2_000)]
        capped = _capped_detail(entries)
        kept = len(capped) - 1
        slack = MAX_ERROR_DETAIL_BYTES - len(_serialise(capped))
        if slack - 1 < floor:  # no room at this width to build an entry that exactly fills it
            continue

        exact = {**base, "msg": "m" * (slack - 1 - floor)}
        assert len(_serialise(exact)) == slack - 1
        assert 1_000 <= len(entries) - kept <= 9_999, "the marker stays four digits wide"

        filled = _capped_detail([*entries[:kept], exact, *entries[kept:-1]])
        assert filled[kept] == exact, f"width={width}: the space it left went unused"
        assert len(_serialise(filled)) <= MAX_ERROR_DETAIL_BYTES
        assert len(_serialise(filled)) == MAX_ERROR_DETAIL_BYTES, "and it is filled to the byte"
        return

    raise AssertionError("no width in the sweep left room for an exact-fit entry")


def test_a_detail_that_is_not_a_list_is_passed_through_even_when_it_is_huge() -> None:
    """The shape guard in `render` is load-bearing, not tidiness: `_capped_detail` ITERATES its
    argument, so an over-cap dict reaching it would come back rewritten as a list of its keys -
    every value gone, and the client told its `detail` was truncated when it was mangled.

    No route emits a non-list `detail` today (no `ApiError` raise or `envelope` call in `src/gct/`
    passes one at all), so this pins the scope decision rather than a live path. The string case
    is the same guard from the other side: it is bounded by `MAX_ERROR_ECHO_CHARS` (#134) and
    never by this cap, so what it must NOT acquire is the marker."""
    mapping = {f"k{i}": i for i in range(5_000)}
    assert len(_serialise(mapping)) > MAX_ERROR_DETAIL_BYTES, "the dict has to be over the cap"
    rendered = json.loads(envelope(400, "kind", "message", detail=mapping).body)["error"]["detail"]
    assert rendered == mapping

    long_text = "x" * (MAX_ERROR_DETAIL_BYTES * 2)
    response = envelope(400, "kind", "message", detail=long_text)
    rendered = json.loads(response.body)["error"]["detail"]
    assert rendered == _bounded(long_text)
    assert _DETAIL_TRUNCATED not in response.body.decode()


def test_a_real_entrys_loc_is_never_rooted_at_detail(probe: TestClient) -> None:
    """The marker's discriminator, executed rather than asserted in prose. Its `loc` is
    `["detail"]`, and what makes that unambiguous is the ROOT, not the length: all three requests
    below produce a real entry whose `loc` is ONE element, `["body"]`. A client told to look for a
    one-element path would read "you sent no body at all" as "some of your errors were
    truncated"."""
    for body, kwargs in (
        ("an absent body", {}),
        ("a bare string body", {"content": b'"hello"'}),
        ("a list body", {"content": b"[1,2]"}),
    ):
        response = probe.post("/probe", headers={"content-type": "application/json"}, **kwargs)
        detail = response.json()["error"]["detail"]

        assert response.status_code == 422, body
        assert detail, body
        assert any(entry["loc"] == ["body"] for entry in detail), f"{body}: {detail}"
        assert all(entry["loc"][0] != "detail" for entry in detail), body
        assert all(entry["loc"][0] == "body" for entry in detail), body


def test_a_surviving_entry_is_what_an_uncapped_envelope_would_have_shown() -> None:
    """The contract sentence, executed. "Every other entry is pydantic's own, UNMODIFIED" was
    false over real HTTP: `MAX_ERROR_ECHO_CHARS` may already have truncated a string inside a
    surviving entry, and a client reading that sentence would take the marked 512-character echo
    for something the cap did to it.

    What is true is the comparison this makes: a surviving entry is byte-for-byte the entry the
    SAME request produces when the cap never fires. Both halves are asserted - that the entries
    are identical across the two responses, and that the first really is truncated in both, so the
    test is not green by comparing two untouched entries.

    EVERY SURVIVOR, not `detail[0]`. Checking one entry of 160 leaves a cap that reached inside
    any of the other 159 completely invisible, which is what the second falsification pass found
    (its "166" was the PR body's estimate; the measured survivor count for this body is 160):
    the sentence was pinned for one entry and claimed for the list. The uncapped side is the SAME
    REQUEST rendered with the cap lifted, so the two lists are entry-for-entry comparable over the
    whole surviving head rather than over a shorter request that happens to share a first entry."""
    oversized = "V" * 5_000
    body = {"name": "x", "k0": oversized, **{f"k{i}": 1 for i in range(1, 900)}}
    capped = _post_probe(body)
    with pytest.MonkeyPatch.context() as lifted:
        lifted.setattr(errors, "MAX_ERROR_DETAIL_BYTES", 64 * 1024 * 1024)
        uncapped = _post_probe(body)

    assert capped[-1]["type"] == _DETAIL_TRUNCATED, "the cap has to have fired"
    assert _DETAIL_TRUNCATED not in _serialise(uncapped), "and not fired here"
    survivors = capped[:-1]
    assert len(survivors) > 100, (
        f"only {len(survivors)} entries survived, so 'every survivor' is barely a claim"
    )
    assert survivors == uncapped[: len(survivors)], (
        "a surviving entry differs from what the same request produces uncapped, so the cap "
        "reached inside one. First disagreement: "
        + next(
            (
                f"index {i}: {a!r} != {b!r}"
                for i, (a, b) in enumerate(zip(survivors, uncapped, strict=False))
                if a != b
            ),
            f"none pairwise; the uncapped list is {len(uncapped)} long against "
            f"{len(survivors)} survivors",
        )
    )
    assert capped[0]["loc"] == ["body", "k0"]
    assert capped[0]["input"] == _bounded(oversized), "#134's bound, not the cap's doing"
    assert capped[0]["input"].endswith(f"{MARKER}5000 chars]")


def test_the_cap_measures_with_the_serialiser_that_renders_not_one_of_its_own() -> None:
    """The THIRD way measurement and wire can diverge, and the one the two pins beside this miss.

    `test_the_bytes_that_ship_are_the_bytes_the_cap_measured` catches a change to `render`'s
    settings (the identity) and a change to `_serialise` itself (the ASCII oracle). Neither sees
    `_capped_detail` MEASURING with its own `json.dumps` while `render` keeps using `_serialise` -
    the exact drift `_serialise` was extracted to prevent. With `ensure_ascii=False` on the
    measuring side, 1,176 tests stayed green while a legal request shipped `detail` 2,265 bytes
    (13.8%) over the cap, because a multi-byte character counts as one there and as six on the
    wire.

    WHY THIS SHAPE. The other test carries ONE non-ASCII entry, so that drift is a few bytes and
    disappears inside its `+ 512` envelope allowance. Here EVERY entry carries non-ASCII, so the
    drift scales with the list, and the quantity asserted is the shipped `detail` itself - parsed
    back off the wire and re-measured - rather than the whole body against a slack figure. The
    guarantee is about serialised `detail`, so that is what is measured.
    """

    def _entry(index: int) -> dict:
        return {**_fake_entry(index), "msg": "Extra inputs are not permitted — naïve café ✓"}

    def _unicode_chars(value) -> int:
        """What a measurement that forgot `ensure_ascii=True` would count."""
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")))

    # TWO POPULATIONS, because `_capped_detail` measures in two places and one list cannot reach
    # both. The long list drives the per-entry `cost` inside the loop. The STRADDLING list is over
    # the cap in the bytes that ship and under it in unicode characters, which is the only shape
    # the early `return detail` can get wrong - and a mutant that changed only the early return
    # and the reserve survived the long list on its own.
    for label, detail in (("long", [_entry(i) for i in range(5_000)]), ("straddling", None)):
        if detail is None:
            detail = [_entry(0)]
            while _unicode_chars(detail) <= MAX_ERROR_DETAIL_BYTES:
                detail.append(_entry(len(detail)))
            detail.pop()
            assert _unicode_chars(detail) <= MAX_ERROR_DETAIL_BYTES < len(_serialise(detail)), (
                "this list no longer straddles the two measurements, so the early-return arm is "
                f"not being exercised: {_unicode_chars(detail)} chars, "
                f"{len(_serialise(detail))} bytes"
            )

        body = envelope(400, "kind", "message", detail=detail).body
        shipped = json.loads(body)["error"]["detail"]

        assert shipped[-1]["type"] == _DETAIL_TRUNCATED, f"[{label}] the cap has to have fired"
        assert len(_serialise(shipped)) <= MAX_ERROR_DETAIL_BYTES, (
            f"[{label}] the `detail` that reached the wire is over the cap, so the cap counted "
            f"something other than what shipped. {len(_serialise(shipped))} > "
            f"{MAX_ERROR_DETAIL_BYTES}"
        )
        assert body.decode("utf-8").isascii(), (
            f"[{label}] the wire is not ASCII, so `_capped_detail`'s character count is no "
            "longer a byte count"
        )
        assert "\\u2014" in body.decode("utf-8"), (
            f"[{label}] the non-ASCII this test turns on is really there"
        )


def test_the_reserve_holds_at_a_dropped_count_wider_than_any_request_can_reach() -> None:
    """`reserve` is `_detail_marker(len(detail))` and the count is spelled in DECIMAL, so any
    rewrite that computes it at a CAPPED count is one byte short past that count.
    `_detail_marker(9999)` was caught by the width sweep; `min(len(detail), 99_999)` was not.

    TWO THINGS HAVE TO LINE UP, which is why neither the sweep nor a bare large count sees it.
    The DROPPED count must have more digits than the reserve was computed at - dropped is
    `len(detail) - kept`, 160 entries are kept at this cap, so 100,100 entries drop 99,940, still
    five digits and still correctly reserved. (The second falsification pass named 100,100; the
    boundary is 100,160, where dropped is exactly 100,000 - 100,159 drops 99,999. Re-measured in
    `/land`, which had it 40 too high.) And the packing must be TIGHT:
    the reserve carries a spare byte for the comma, so a one-byte shortfall is invisible unless
    the kept entries fill the budget exactly.

    So the width is not swept, it is COMPUTED - the same device as
    `test_an_entry_that_exactly_fills_the_space_the_cap_left_is_kept`, one axis over. A first run
    measures the headroom a real pack leaves; widening one entry by that plus one is what makes
    `used` land on the budget rather than short of it. Under the shipped code the result is inside
    the cap; under `min(len(detail), 99_999)` it is 16,385 - one byte over, from a list no legal
    request can produce.

    LATENT, NOT LIVE. A body under `MAX_JSON_BODY_BYTES` yields at most ~6,950 entries, so no
    client reaches six digits. It is pinned because `_capped_detail`'s docstring states the
    guarantee over the list it is HANDED, not over the lists a request can build.

    HOW FAR THIS PIN REACHES, said plainly because the figures above are not constants. Every one
    of them - 160 kept, the boundary at 100,160 - is measured at `MAX_ERROR_DETAIL_BYTES` = 16 KiB
    and at this entry width. The constant is declared PROVISIONAL, and a retune moves them: 119
    kept at 12 KiB, 200 at 20 KiB. The assertions guard themselves against that (`len(str(dropped))
    == 6` fails loudly and says to raise the count); the prose cannot, so it names what it was
    measured at. And the pin is at ONE list length, so a clamp ABOVE 100,400 survives it -
    `min(len(detail), 9_999_999)` is green here and is wrong by 2 bytes at 10^8 entries, which is
    the same family one decade up. Pinning every decade of an unreachable count is not worth a
    test; knowing that is what this paragraph is for."""

    def _entry(index: int, pad: int = 0) -> dict:
        return {**_fake_entry(index), "loc": ["body", "k" + "y" * pad + f"{index:06d}"]}

    detail = [_entry(i) for i in range(100_400)]
    probe = _capped_detail(detail)
    dropped = probe[-1]["ctx"]["dropped"]

    assert len(str(dropped)) == 6, (
        f"{dropped} dropped is not the six-digit count this test exists to reserve for; the cap "
        "or the entry width moved, so raise the count until it is"
    )
    headroom = MAX_ERROR_DETAIL_BYTES - len(_serialise(probe))
    assert 0 <= headroom < 32, f"the probe left {headroom} bytes, which is not a tight pack"

    tight = _capped_detail([_entry(0, headroom + 1), *detail[1:]])

    assert tight[-1]["type"] == _DETAIL_TRUNCATED, "the cap has to have fired"
    assert len(str(tight[-1]["ctx"]["dropped"])) == 6, "still a six-digit dropped count"
    assert len(_serialise(tight)) <= MAX_ERROR_DETAIL_BYTES, (
        f"a six-digit dropped count overran the reserve: {len(_serialise(tight))} > "
        f"{MAX_ERROR_DETAIL_BYTES}"
    )


def test_the_bytes_that_ship_are_the_bytes_the_cap_measured() -> None:
    """`_serialise` exists so "the size the cap counts and the size that reaches the wire cannot
    drift apart" - its own docstring. Nothing ran that. Rendering with `json.dumps`'s DEFAULT
    separators instead of the compact ones leaves the whole suite green while the wire carries
    17,964 bytes for the ticket's own request, `detail` 1,496 bytes (9.1%) OVER the cap, with
    `_capped_detail` still counting 16,375 the whole time.

    Three assertions, because each alone is weak. The IDENTITY says the body is exactly what
    `_serialise` would produce, so no render setting can differ from the one the cap measures
    with. It cannot see a change to `_serialise` ITSELF - that mutates the measuring stick and the
    measured together - so the ASCII assertion is the independent oracle beside it: the non-ASCII
    entry ships as `\\uXXXX` escapes or the wire is not ASCII, and `_capped_detail` counts
    CHARACTERS where the cap is in BYTES, which is only the same number while that holds. The SIZE
    says the shipped bytes are inside the cap plus the envelope's own frame, measured on the wire
    rather than on the value `_capped_detail` returned - the quantity the guarantee is about."""
    detail = [{**_fake_entry(0), "msg": "Café — naïve ✓"}, *[_fake_entry(i) for i in range(5_000)]]
    body = envelope(400, "kind", "message", detail=detail).body

    assert body == _serialise(json.loads(body)).encode("utf-8")
    assert body.decode("utf-8").isascii(), "the wire is ASCII, so a character IS a byte"
    assert len(body) <= MAX_ERROR_DETAIL_BYTES + 512, "the envelope's frame is ~80 bytes"
    assert json.loads(body)["error"]["detail"][-1]["type"] == _DETAIL_TRUNCATED
