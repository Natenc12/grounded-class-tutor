"""The request-body bound (issue #125): the scanner, the middleware, and the two acceptance pins.

Three layers, and the middle one is the reason this file is not just route tests. `scan_depth` is
pure and gets the adversarial inputs - brackets inside strings, escapes, a leading close bracket -
because getting it wrong is silent in both directions: too eager and an ordinary question
containing `[[[` is refused, too lax and the 500 comes back.

THE MULTIPART PIN IS THE NET AND IT IS LOAD-BEARING. Nothing else in this repo uploads a
MB-scale file over HTTP: before this file existed the largest UPLOAD payload in the api suite was
`PDF_BYTES`, 54 bytes of file content inside a 368-byte multipart body, and all three paid smokes
are library-level with no HTTP client in them. (Not the largest payload of any kind -
`test_ask_router.py` posts a 2,066-byte question body - but the upload path is the one this pin is
about, and 54 bytes is what it had.) So a bound that accidentally
applied to the streamed upload would leave every check on the board green while `POST /files`
refused every real course file. `test_a_corpus_scale_multipart_upload_is_still_accepted` is what
catches that, and it is why the payload is SYNTHETIC: the dogfood corpus is gitignored, and a pin
that skips in CI is not a net (CLAUDE.md).

Read-back for "it was NOT stored" goes through `db_other`, a second connection, for the reason
CLAUDE.md gives - `db`'s own connection sees uncommitted work, so a one-connection read-back is
true either way - and its positive arm sits directly beneath it, because a refusal test alone
passes against a route that never writes at all.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from starlette.responses import PlainTextResponse

from gct.api import errors, limits
from gct.api.errors import ApiError
from gct.api.limits import (
    BodyLimit,
    _to_utf8,
    is_multipart,
    may_be_parsed_as_json,
    scan_depth,
)
from gct.api.routers import files as files_router
from gct.api.routers.ask import MAX_QUESTION_CHARS
from gct.config import MAX_JSON_BODY_BYTES, MAX_JSON_BODY_DEPTH
from gct.staging import stage

JSON = {"content-type": "application/json"}

# The largest file in the dogfood corpus - `Lecture 20 Evolutionist-Creationist Debate.pptx`,
# 7,114,312 bytes - rounded UP. The number is what the multipart pin has to clear; the corpus
# itself cannot be read here (gitignored), so the size travels and the file does not.
CORPUS_SCALE_BYTES = 7_200_000


def nested_body(arrays: int) -> bytes:
    """`{"name": [[[...]]]}` - an object holding `arrays` nested arrays, so the body's own
    nesting is `arrays + 1`. Stated because every boundary assertion below turns on that +1."""
    return b'{"name": ' + b"[" * arrays + b"]" * arrays + b"}"


# The encodings `json.loads` accepts that are not UTF-8. Both BOM forms are here because
# `json.detect_encoding` reaches them by a different branch than the BOM-less ones (a byte-order
# mark, versus sniffing null bytes), and a fix that handled only one branch would look right.
NON_UTF8_ENCODINGS = ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"]


def nested_body_in(encoding: str, arrays: int = 1_200) -> bytes:
    """The deep body again, in an encoding `json.loads` accepts but a raw-byte scan cannot read.

    The `Ģ` is the whole attack, not decoration. In UTF-16LE that character (U+0122) is the bytes
    `22 01` - a bare `"` sitting INSIDE a string literal - so a scanner walking raw bytes reads
    the string as closing there, leaves `in_string` inverted for the rest of the body, and skips
    every structural bracket that follows. The body scans as depth 1 and the 500 comes back.
    """
    text = '{"pad": "Ģ", "name": ' + "[" * arrays + "]" * arrays + "}"
    return text.encode(encoding)


def escaped_quote_body() -> bytes:
    """A valid JSON body carrying an ESCAPED QUOTE before its nesting.

    The escape is the whole point. Cut this body immediately after the backslash and rejoin the
    halves with any byte between them, and that byte becomes the escaped character - so the real
    `"` closes the string, everything after it reads as string data, and the nesting is skipped.
    Byte-exact it scans past the bound; one separator and it scans as depth 1.

    Sized off the bound (`+ 9`, so the wrapping object makes it `MAX_JSON_BODY_DEPTH + 10`) rather
    than typed, for the reason every other body here is: a literal stops being over the bound the
    day the bound moves.
    """
    arrays = MAX_JSON_BODY_DEPTH + 9
    return ('{"pad": "he said \\"hi\\"", "name": ' + "[" * arrays + "]" * arrays + "}").encode()


# --------------------------------------------------------------------------------------------
# scan_depth - the part whose bugs are silent
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"{}", 1),
        (b"[]", 1),
        (b'{"a": 1}', 1),
        (b'{"a": [1]}', 2),
        (b'{"a": {"b": [1]}}', 3),
        (b'{"a": 1, "b": 2}', 1),  # siblings are not depth
        (b"", 0),
    ],
)
def test_scan_depth_counts_container_nesting(body: bytes, expected: int) -> None:
    assert scan_depth(body, limit=100) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # A bracket inside a string literal is DATA. A student asking about notation is the
        # realistic form of this, and refusing it would be the fix causing a worse bug than
        # the one it fixed.
        (b'{"question": "what does [[[ mean?"}', 1),
        (b'{"a": "]]]]]]"}', 1),
        # An escaped quote does NOT end the string, so the brackets after it are still data.
        (rb'{"a": "he said \"[[[\" ok"}', 1),
        # An escaped BACKSLASH does end it: the `\\` consumes itself, so the following `"`
        # closes the string and the brackets after it are structural. This is the case a
        # naive "skip the character after every backslash" scanner gets backwards.
        (rb'{"a": "trailing slash \\"}[[[', 3),
        # A close bracket clamps at zero rather than going negative, so a leading `]]]]`
        # cannot subtract from a genuine nesting that follows it.
        (b"]]]][[[[", 4),
    ],
)
def test_scan_depth_reads_string_literals_and_escapes(body: bytes, expected: int) -> None:
    assert scan_depth(body, limit=100) == expected


def test_scan_depth_stops_once_it_passes_the_limit() -> None:
    """Bounded work: a body far deeper than the limit is not walked to the end. The value is
    'at least limit+1', which is the only thing the caller asks it."""
    assert scan_depth(nested_body(10_000), limit=MAX_JSON_BODY_DEPTH) == MAX_JSON_BODY_DEPTH + 1


def test_multibyte_characters_cannot_forge_a_bracket() -> None:
    """The scanner walks raw BYTES, before anything decodes them. Within UTF-8 that is safe,
    because every continuation byte is >= 0x80 and every character it counts is ASCII - so a body
    full of astral-plane text scans as the depth its structure actually has. The three tests
    below are why that sentence needs "within UTF-8" in it."""
    body = '{"question": "' + "\U0001f600" * 500 + '"}'
    assert scan_depth(body.encode("utf-8"), limit=100) == 1


@pytest.mark.parametrize("encoding", NON_UTF8_ENCODINGS)
def test_a_non_utf8_body_is_normalised_before_it_is_scanned(encoding: str) -> None:
    """The three facts that together make this a bug rather than a curiosity.

    `scan_depth` on the raw bytes reports 1 - it is blind. `scan_depth` on the normalised bytes
    reports the nesting that is really there. And `json.loads` accepts the raw bytes, which is
    what makes the blindness reachable: the body the scanner waved through is a body the parser
    builds and the error renderer then walks recursively.
    """
    raw = nested_body_in(encoding)

    assert scan_depth(raw, limit=MAX_JSON_BODY_DEPTH) == 1
    assert scan_depth(_to_utf8(raw), limit=MAX_JSON_BODY_DEPTH) > MAX_JSON_BODY_DEPTH
    assert isinstance(json.loads(raw), dict)


def test_a_utf8_body_reaches_the_scanner_as_the_very_same_object() -> None:
    """The common path decodes nothing and copies nothing - object identity, so a future edit
    that normalises unconditionally is caught here rather than in a profile. A UTF-8 BOM needs no
    conversion either: its three bytes are all >= 0x80, so the scanner already ignores them."""
    body = nested_body(3)
    assert _to_utf8(body) is body

    with_bom = b"\xef\xbb\xbf" + body
    assert _to_utf8(with_bom) is with_bom
    assert scan_depth(with_bom, limit=100) == 4


def test_normalising_cannot_invent_nesting_that_is_not_there() -> None:
    """The other direction, so the fix is not one-sided: a legitimate non-UTF-8 body - brackets
    inside a string, exactly the case `scan_depth` exists to get right - still scans as depth 1
    after normalisation, rather than being refused as 'too deeply nested'."""
    text = '{"question": "what does [[[ mean in Gödel\'s notation?"}'
    for encoding in NON_UTF8_ENCODINGS:
        assert scan_depth(_to_utf8(text.encode(encoding)), limit=100) == 1, encoding


# --------------------------------------------------------------------------------------------
# Which bodies get looked at
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/json", True),
        ("application/json; charset=utf-8", True),
        ("APPLICATION/JSON", True),  # measured to reach the JSON parser
        ("application/vnd.api+json", True),
        (None, True),  # superset of FastAPI's rule - see `may_be_parsed_as_json`
        ("text/plain", False),
        ("application/octet-stream", False),
        ("multipart/form-data; boundary=x", False),
        ("application/x-www-form-urlencoded", False),
    ],
)
def test_may_be_parsed_as_json(content_type: str | None, expected: bool) -> None:
    assert may_be_parsed_as_json(content_type) is expected


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("multipart/form-data; boundary=x", True),
        ("MULTIPART/FORM-DATA; boundary=x", True),
        ("multipart/mixed", False),
        ("application/json", False),
        (None, False),
    ],
)
def test_is_multipart(content_type: str | None, expected: bool) -> None:
    assert is_multipart(content_type) is expected


# --------------------------------------------------------------------------------------------
# The middleware, over a probe route - no database, no providers
# --------------------------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


@pytest.fixture
def probe() -> TestClient:
    """An app wired exactly as `create_app` wires one - the real `errors.install` and the real
    `limits.install` - around one route with a JSON body. A probe rather than a real route so
    these tests pin the BOUND and not any route's own validation, and so they need no database.
    """
    app = FastAPI()
    errors.install(app)
    limits.install(app)

    @app.post("/probe")
    def _probe(body: _Body) -> dict:
        return {"length": len(body.name)}

    return TestClient(app, raise_server_exceptions=False)


def test_a_body_at_the_depth_bound_reaches_the_parser(probe: TestClient) -> None:
    """One under: the middleware lets it through, so the answer is pydantic's 422 about the
    field - which is what proves the request was PARSED rather than refused at the boundary."""
    response = probe.post("/probe", content=nested_body(MAX_JSON_BODY_DEPTH - 1), headers=JSON)
    assert response.status_code == 422
    assert response.json()["error"]["kind"] == "validation"


def test_a_body_one_past_the_depth_bound_is_refused(probe: TestClient) -> None:
    response = probe.post("/probe", content=nested_body(MAX_JSON_BODY_DEPTH), headers=JSON)
    assert response.status_code == 400
    assert response.json()["error"]["kind"] == "body_too_nested"


@pytest.mark.parametrize("encoding", NON_UTF8_ENCODINGS)
def test_a_deep_body_in_a_non_utf8_encoding_is_refused(probe: TestClient, encoding: str) -> None:
    """The bound covers every encoding the parser downstream of it accepts, not just UTF-8.

    Before `_to_utf8` this was a **500 `internal`** on all six - the exact answer this module
    exists to remove, reachable by re-encoding the same attack body.
    """
    response = probe.post("/probe", content=nested_body_in(encoding), headers=JSON)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["kind"] == "body_too_nested"


@pytest.mark.parametrize("encoding", NON_UTF8_ENCODINGS)
def test_a_legitimate_non_utf8_body_still_reaches_the_route(
    probe: TestClient, encoding: str
) -> None:
    """The positive arm. Normalising for the scan must not change what the app is handed: a real
    UTF-16 request is parsed and answered, and the non-ASCII characters survive intact - which is
    also the statement that `_replay` still hands on the client's own bytes, not the scanner's
    normalised copy."""
    name = "Ģrundlagen der Biologie \U0001f600"
    response = probe.post("/probe", content=f'{{"name": "{name}"}}'.encode(encoding), headers=JSON)
    assert response.status_code == 200, response.text
    assert response.json()["length"] == len(name)


def test_the_depth_refusal_names_the_bound(probe: TestClient) -> None:
    """Acceptance: the refusal NAMES the bound. A 4xx that does not say what the limit is
    leaves the client guessing at the retry."""
    response = probe.post("/probe", content=nested_body(1_000), headers=JSON)
    assert str(MAX_JSON_BODY_DEPTH) in response.json()["error"]["message"]


@pytest.mark.parametrize("arrays", [40, 480, 970, 1_200, 5_000])
def test_no_nesting_depth_produces_a_500(probe: TestClient, arrays: int) -> None:
    """The live bug, swept rather than pinned to a number.

    The depth at which rendering used to recurse is NOT a constant - it moved between 480 and
    970 on one interpreter depending only on how much stack the caller had already spent - so a
    test asserting a specific threshold would pass on the machine that wrote it and prove
    nothing anywhere else. What must hold at every depth is that the server never blames itself.
    """
    response = probe.post("/probe", content=nested_body(arrays), headers=JSON)
    assert response.status_code < 500, response.text
    assert response.json()["error"]["kind"] != "internal"


def test_a_body_at_the_byte_bound_is_accepted(probe: TestClient) -> None:
    """Exactly at the bound, and it goes through - the refusal is for bodies OVER it."""
    filler = MAX_JSON_BODY_BYTES - len(b'{"name": ""}')
    body = b'{"name": "' + b"A" * filler + b'"}'
    assert len(body) == MAX_JSON_BODY_BYTES
    response = probe.post("/probe", content=body, headers=JSON)
    assert response.status_code == 200
    assert response.json()["length"] == filler


def test_a_body_one_byte_past_the_bound_is_refused(probe: TestClient) -> None:
    filler = MAX_JSON_BODY_BYTES - len(b'{"name": ""}') + 1
    body = b'{"name": "' + b"A" * filler + b'"}'
    assert len(body) == MAX_JSON_BODY_BYTES + 1
    response = probe.post("/probe", content=body, headers=JSON)
    assert response.status_code == 413
    assert response.json()["error"]["kind"] == "body_too_large"
    assert str(MAX_JSON_BODY_BYTES) in response.json()["error"]["message"]


def test_the_largest_legitimate_ask_body_is_admitted(probe: TestClient) -> None:
    """What the byte bound MUST admit, asserted rather than left in a comment.

    The worst legitimate case is not `MAX_QUESTION_CHARS` bytes: `max_length` counts CHARACTERS,
    and a client may send an astral-plane character as a `\\uXXXX\\uXXXX` surrogate pair - twelve
    bytes each. If anyone lowers `MAX_JSON_BODY_BYTES` below that, this fails instead of `/ask`
    starting to refuse full-length questions in production.
    """
    question = "\\ud83d\\ude00" * MAX_QUESTION_CHARS
    body = '{"class_id": "123e4567-e89b-12d3-a456-426614174000", "question": "' + question + '"}'
    assert len(body.encode("utf-8")) > 24_000
    response = probe.post("/probe", content=body.encode("utf-8"), headers=JSON)
    # The probe route has no `question` field, so pydantic answers - which is the point: the
    # body reached it rather than being refused for its size.
    assert response.status_code == 422


@pytest.mark.parametrize(
    "content_type",
    ["text/plain", "application/octet-stream", "application/x-www-form-urlencoded"],
)
def test_a_declared_non_json_body_is_not_depth_scanned(
    probe: TestClient, content_type: str
) -> None:
    """Measured, not assumed: these reach the route as raw bytes, so no nesting in them can
    recurse the render, and scanning them would refuse binary that merely contains bracket
    bytes as 'too deeply nested'."""
    response = probe.post(
        "/probe", content=nested_body(1_200), headers={"content-type": content_type}
    )
    assert response.status_code == 422
    assert response.json()["error"]["kind"] == "validation"


def test_a_body_with_no_content_type_is_still_depth_scanned(probe: TestClient) -> None:
    """The superset. FastAPI parses a type-less body as JSON when a route sets
    `strict_content_type=False`; scanning it regardless means flipping that flag cannot quietly
    reopen the 500."""
    response = probe.post("/probe", content=nested_body(1_200))
    assert response.status_code == 400
    assert response.json()["error"]["kind"] == "body_too_nested"


def test_an_oversized_body_labelled_multipart_is_not_parsed_as_json(probe: TestClient) -> None:
    """The exemption opens no hole. A huge body wearing a multipart label and aimed at a
    JSON-body route is refused 422 - not a 500, and nothing stored.

    By PYDANTIC, not by a form parser: FastAPI picks its parser from the route's declared
    parameter, so a route taking a model is handed raw bytes and `MultiPartParser` is never
    constructed. Asserted below rather than described, because the refuser is the whole point of
    the test - the exemption is safe because the body still meets a validator, and which
    validator that is decides what happens if the route's signature ever changes.
    """
    body = b'{"name": "' + b"A" * (2 * MAX_JSON_BODY_BYTES) + b'"}'
    response = probe.post(
        "/probe", content=body, headers={"content-type": "multipart/form-data; boundary=x"}
    )
    assert response.status_code == 422
    detail = response.json()["error"]["detail"]
    assert detail[0]["type"] == "model_attributes_type"
    assert detail[0]["loc"] == ["body"]


# --------------------------------------------------------------------------------------------
# The two mechanisms a later refactor would break silently
# --------------------------------------------------------------------------------------------


def _run(app, scope, messages) -> list[dict]:
    """Drive an ASGI app once at the protocol level and collect what it sent."""
    sent: list[dict] = []

    async def receive() -> dict:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def _scope(content_type: str) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/probe",
        "headers": [(b"content-type", content_type.encode())],
    }


def test_a_multipart_request_keeps_the_very_same_receive_callable() -> None:
    """The upload stream is handed on UNTOUCHED - the same object, not a copy or a wrapper.

    `gct.staging.stage` reads the upload in 1 MiB reads through this callable, so anything that
    buffered or re-wrapped it would put a 100 MiB file in memory. Object identity is the
    strongest available statement that it did not, and it is what a refactor to
    `BaseHTTPMiddleware` would break instantly.
    """
    captured: dict = {}

    async def inner(scope, receive, send) -> None:
        captured["receive"] = receive
        await PlainTextResponse("ok")(scope, receive, send)

    middleware = BodyLimit(inner)
    scope = _scope("multipart/form-data; boundary=x")
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    assert captured["receive"] is receive

    # And the contrast, so the assertion above is about multipart rather than about everything:
    # a JSON body IS buffered, so what reaches the app is the replay and not the original.
    captured.clear()
    _run(
        BodyLimit(inner),
        _scope("application/json"),
        [{"type": "http.request", "body": b"{}", "more_body": False}],
    )
    assert captured["receive"] is not receive


def test_a_buffered_body_is_replayed_with_its_chunk_boundaries_intact() -> None:
    """What the app receives is what the server sent - same chunks, same `more_body` flags -
    so nothing downstream can tell the bound is installed."""
    seen: list[dict] = []

    async def inner(scope, receive, send) -> None:
        while True:
            message = await receive()
            seen.append(message)
            if not message.get("more_body", False):
                break
        await PlainTextResponse("ok")(scope, receive, send)

    chunks = [
        {"type": "http.request", "body": b'{"a"', "more_body": True},
        {"type": "http.request", "body": b": 1}", "more_body": False},
    ]
    _run(BodyLimit(inner), _scope("application/json"), list(chunks))
    assert seen == chunks


def test_the_depth_scan_reads_a_body_that_arrives_across_several_chunks() -> None:
    """The scan JOINS the buffered chunks; it does not read one of them, or each in turn.

    A client chooses where its chunk boundaries fall, so any scan that judges a body by a single
    message is a scan the client picks its way around. That is not hypothetical: a mutant
    replacing the join at `src/gct/api/limits.py` with `messages[0].get("body", b"")` left the
    entire suite green - nothing else here drives the depth scan with a chunked body. The byte
    counter has its own running total and its own pins; this is the depth scan's.

    FOUR CASES, AND EACH KILLS A MUTANT THE OTHER THREE LEAVE ALIVE. The first two split the body
    in two and move the nesting from one side to the other, which kills `messages[0]` and
    `messages[-1]` respectively. Neither can kill the third mutant - scan every message but take
    the deepest, never assembling - because in both of them the carrier chunk is over the bound
    ON ITS OWN, which is exactly what the `carrier` assertion states. So the third case cuts the
    same body into many chunks each shorter than the bound: no chunk is deep enough to be
    refused by itself, the join is, and only code that assembles can answer 400.

    That third mutant is not equivalent, which is why it earns a case rather than a note. Driven
    over a real uvicorn server by a socket client sending the body in 20-byte writes, it answers
    500 `internal` - the exact defect #125 removes - while passing every test in this repo as the
    suite stood BEFORE the third case was written. (Stated in the past tense on purpose: the third
    case is what changed that, so at this commit the mutant fails exactly one test - this one.)

    THE FOURTH CASE IS ABOUT THE JOIN BEING BYTE-EXACT, which the other three do not check: their
    chunks are solid brackets, so a join that inserted a separator would produce the same depth
    and every one of them would still pass. `b"".join` -> `b" ".join` is a one-character typo that
    survives all three, and it is not equivalent - it reintroduces #125's 500. The mechanism is
    the one `nested_body_in` attacks from the other direction: a chunk boundary immediately after
    the backslash of an escaped quote, so the inserted byte becomes the escaped character and the
    real quote then CLOSES the string. Everything after it reads as string data, the nesting is
    skipped, and a body that scans 33 byte-exact scans 1 with a separator.

    The shallow-chunk assertions are what make the status assertions mean something: they state
    that the chunks the nesting is NOT in really are shallow on their own, so a refusal can only
    have come from putting them together.
    """

    async def inner(scope, receive, send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    deep = nested_body(1_200)
    for label, head, tail in (
        ("nesting in the last chunk", deep[:12], deep[12:]),
        ("nesting in the first chunk", deep[:-12], deep[-12:]),
    ):
        carrier, shallow = (tail, head) if label.endswith("last chunk") else (head, tail)
        assert scan_depth(shallow, limit=MAX_JSON_BODY_DEPTH) <= MAX_JSON_BODY_DEPTH, label
        assert scan_depth(carrier, limit=MAX_JSON_BODY_DEPTH) > MAX_JSON_BODY_DEPTH, label

        sent = _run(
            BodyLimit(inner),
            _scope("application/json"),
            [
                {"type": "http.request", "body": head, "more_body": True},
                {"type": "http.request", "body": tail, "more_body": False},
            ],
        )
        assert sent[0]["status"] == 400, label
        rendered = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        assert json.loads(rendered)["error"]["kind"] == "body_too_nested", label

    # The third case: the SAME body, cut so that no chunk carries the nesting on its own. The
    # chunk size is derived from the bound rather than typed, so it cannot drift out from under
    # the assertion below - a chunk of solid `[` is only as deep as it is long, so half the
    # bound leaves every chunk comfortably under it whatever the bound becomes.
    pieces = [
        deep[start : start + MAX_JSON_BODY_DEPTH // 2]
        for start in range(0, len(deep), MAX_JSON_BODY_DEPTH // 2)
    ]
    assert len(pieces) > 2, "the point of this case is MANY chunks, not the two above"
    deepest_alone = max(scan_depth(piece, limit=MAX_JSON_BODY_DEPTH) for piece in pieces)
    assert deepest_alone <= MAX_JSON_BODY_DEPTH, deepest_alone
    sent = _run(
        BodyLimit(inner),
        _scope("application/json"),
        [
            {"type": "http.request", "body": piece, "more_body": index < len(pieces) - 1}
            for index, piece in enumerate(pieces)
        ],
    )
    assert sent[0]["status"] == 400, "many small chunks"
    rendered = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert json.loads(rendered)["error"]["kind"] == "body_too_nested"

    # The fourth case: the join has to be BYTE-EXACT, not merely complete. The cut is located
    # rather than typed - immediately after the backslash that escapes the closing quote - so it
    # stays on the escape wherever the literal moves.
    escaped = escaped_quote_body()
    cut = escaped.index(b'\\"", ') + 1
    assert escaped[cut - 1 : cut] == b"\\", "the cut must land directly after the escape"
    assert json.loads(escaped), "the body is real JSON, so the parser really would recurse on it"
    assert scan_depth(escaped, limit=MAX_JSON_BODY_DEPTH) > MAX_JSON_BODY_DEPTH
    assert scan_depth(b" ".join([escaped[:cut], escaped[cut:]]), limit=MAX_JSON_BODY_DEPTH) == 1
    sent = _run(
        BodyLimit(inner),
        _scope("application/json"),
        [
            {"type": "http.request", "body": escaped[:cut], "more_body": True},
            {"type": "http.request", "body": escaped[cut:], "more_body": False},
        ],
    )
    assert sent[0]["status"] == 400, "split on an escape"
    rendered = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert json.loads(rendered)["error"]["kind"] == "body_too_nested"

    # And the contrast, so this is about DEPTH and not about chunked bodies being refused: a
    # shallow body split the same way goes through to the app.
    ok = _run(
        BodyLimit(inner),
        _scope("application/json"),
        [
            {"type": "http.request", "body": b'{"a"', "more_body": True},
            {"type": "http.request", "body": b": 1}", "more_body": False},
        ],
    )
    assert ok[0]["status"] == 200


def test_an_api_error_raised_in_middleware_would_arrive_as_a_500() -> None:
    """WHY `limits` renders its refusals instead of raising them - pinned, because the tidier
    shape is the broken one and nothing else would say so.

    Starlette's stack is `ServerErrorMiddleware` -> user middleware -> `ExceptionMiddleware` ->
    route, and `add_exception_handler(ApiError, ...)` registers on the INNERMOST. So an
    `ApiError` raised in user middleware travels outward past its own handler and is caught only
    by the `Exception` handler on the outermost - reaching the client as exactly the 500
    `internal` the bound exists to remove. This test is the executable form of that claim; if a
    future starlette makes it false, this goes red and `limits` can be simplified.
    """
    app = FastAPI()
    errors.install(app)

    @app.post("/probe")
    def _probe(body: _Body) -> dict:
        return {"ok": True}

    class Raising:
        def __init__(self, app) -> None:
            self.app = app

        async def __call__(self, scope, receive, send) -> None:
            if scope["type"] == "http":
                raise ApiError(413, "body_too_large", "refused")
            await self.app(scope, receive, send)

    app.add_middleware(Raising)
    response = TestClient(app, raise_server_exceptions=False).post("/probe", json={"name": "x"})
    assert response.status_code == 500
    assert response.json()["error"]["kind"] == "internal"


# --------------------------------------------------------------------------------------------
# Acceptance, on the real routes
# --------------------------------------------------------------------------------------------


def test_a_deeply_nested_body_is_a_4xx_on_ask(api) -> None:
    response = api.client.post("/ask", content=nested_body(1_200), headers=JSON)
    assert response.status_code == 400
    assert response.json()["error"]["kind"] == "body_too_nested"


def test_a_deeply_nested_body_is_a_4xx_on_classes(api) -> None:
    response = api.client.post("/classes", content=nested_body(1_200), headers=JSON)
    assert response.status_code == 400
    assert response.json()["error"]["kind"] == "body_too_nested"


def test_a_deeply_nested_utf16_body_is_a_4xx_on_classes(api) -> None:
    """The encoding hole, on a real route rather than a probe.

    `starlette.requests.Request.json` hands raw bytes to `json.loads`, which sniffs them with
    `json.detect_encoding` - so UTF-16 reaches the render exactly like UTF-8 does. This body was
    a 500 `internal` while the depth scan understood only UTF-8.
    """
    response = api.client.post("/classes", content=nested_body_in("utf-16-le"), headers=JSON)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["kind"] == "body_too_nested"


def test_a_multi_megabyte_class_name_is_refused_and_not_stored(api, db_other) -> None:
    """Both halves. The 413 is what the client sees; the empty read-back on a SECOND connection
    is what proves nothing was published - `db`'s own connection would see uncommitted work and
    report the same thing either way (ADR 0025)."""
    name = "A" * (8 * 1024 * 1024)
    response = api.client.post("/classes", json={"name": name})
    assert response.status_code == 413
    assert response.json()["error"]["kind"] == "body_too_large"

    rows = db_other.execute(
        "select count(*) from classes where owner_id = %(owner)s and length(name) > 1000",
        {"owner": api.owner_id},
    ).fetchone()
    assert rows[0] == 0


def test_an_ordinary_class_name_is_still_created_and_stored(api, db_other) -> None:
    """The positive arm the refusal above needs: without it, that test passes against a route
    that stopped writing anything at all."""
    response = api.client.post("/classes", json={"name": "Biology 101"})
    assert response.status_code == 201

    stored = db_other.execute(
        "select name from classes where class_id = %(id)s",
        {"id": response.json()["class_id"]},
    ).fetchone()
    assert stored[0] == "Biology 101"


def test_a_corpus_scale_multipart_upload_is_still_accepted(
    api, db_other, monkeypatch, tmp_path
) -> None:
    """THE NET (see the module docstring). A 7.2 MB upload - past the largest file in the
    dogfood corpus - must still be a 202.

    The payload is synthetic because the corpus is gitignored and this has to run in CI, which
    is the only place the whole board is green enough for a broken upload path to hide. What the
    bound could get wrong is a byte count, and a byte count does not care what the bytes say.

    `stage` is repointed at `tmp_path` the way `test_files_router.py` does it - the REAL stager
    with a different directory - so the shipped code runs and only its output moves.
    """
    monkeypatch.setattr(files_router, "stage", partial(stage, staging_dir=tmp_path))
    payload = b"%PDF-1.4\n" + b"A" * CORPUS_SCALE_BYTES

    response = api.client.post(
        "/files",
        data={"class_id": api.class_id},
        files={"file": ("lecture-20.pdf", payload, "application/pdf")},
    )
    assert response.status_code == 202, response.text

    staged = db_other.execute(
        "select staging_ref from files where file_id = %(id)s",
        {"id": response.json()["file_id"]},
    ).fetchone()
    # The bytes reached disk intact: the bound neither truncated the stream nor refused it.
    assert len(open(staged[0], "rb").read()) == len(payload)
