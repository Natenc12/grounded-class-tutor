"""PROTOTYPE property tests for the request-body bound, checked against `json.loads` as the reference."""
import json

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, assume, given, settings, strategies as st

from gct.api.app import create_app
from gct.api.limits import _to_utf8, scan_depth
from gct.config import MAX_JSON_BODY_BYTES, MAX_JSON_BODY_DEPTH

tricky = st.text(alphabet=st.sampled_from(list('[]{}"\\ ,:aé') + ["\U0001f600"]), max_size=12)
leaves = st.none() | st.booleans() | st.integers() | tricky
values = st.recursive(leaves, lambda kids: st.lists(kids, max_size=3) | st.dictionaries(tricky, kids, max_size=3), max_leaves=12)
docs = st.builds(lambda v, n, as_list: __import__("functools").reduce(lambda acc, _: [acc] if as_list else {"k": acc}, range(n), v),
                 values, st.integers(0, 40), st.booleans())
ENCODINGS = ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"]


def depth(value):
    if isinstance(value, dict):
        return 1 + max(map(depth, value.values()), default=0)
    if isinstance(value, list):
        return 1 + max(map(depth, value), default=0)
    return 0


@given(docs, st.sampled_from(ENCODINGS), st.booleans(), st.sampled_from([None, 1]), st.integers(1, 45))
def test_the_scanner_agrees_with_the_parser(doc, encoding, ascii_only, indent, limit):
    body = json.dumps(doc, ensure_ascii=ascii_only, indent=indent).encode(encoding)
    try:
        assume(json.loads(body) == doc)  # only bodies the real parser reads as this document
    except ValueError:
        assume(False)
    scanned, true = scan_depth(_to_utf8(body), limit=limit), depth(doc)
    assert scanned == true if true <= limit else scanned > limit


class Stub:
    model_id, dim = "stub", 1536
    embed = generate = lambda self, *_: []


client = TestClient(create_app(embedder=Stub(), generator=Stub()), raise_server_exceptions=False)
client.__enter__()  # run the lifespan once for the module (prototype shortcut for a module fixture)
CONTENT_TYPES = ["application/json", "application/problem+json", None, "text/plain", "application/JSON; charset=utf-8"]


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(docs | st.binary(max_size=200), st.sampled_from(ENCODINGS), st.sampled_from(CONTENT_TYPES), st.integers(0, 2))
def test_no_body_reaches_a_parser_unbounded_and_none_is_a_500(doc, encoding, content_type, pad):
    body = doc if isinstance(doc, bytes) else json.dumps(doc).encode(encoding)
    body += b" " * (pad * MAX_JSON_BODY_BYTES // 2)
    headers = {"content-type": content_type} if content_type else {}
    response = client.post("/ask", content=body, headers=headers)
    kind = response.json()["error"]["kind"] if response.status_code >= 400 else None
    assert response.status_code < 500, response.text
    if len(body) > MAX_JSON_BODY_BYTES:
        assert (response.status_code, kind) == (413, "body_too_large")
    elif not isinstance(doc, bytes) and depth(doc) > MAX_JSON_BODY_DEPTH and content_type != "text/plain":
        assert (response.status_code, kind) == (400, "body_too_nested")
    else:
        assert kind not in ("body_too_large", "body_too_nested") or isinstance(doc, bytes)
