"""`POST /classes` over HTTP (issue #107): what a create publishes, and what each refusal does not.

Every "it was written" assertion reads back through `db_other` — a connection neither the handler
(which built its own per request and closed it) nor `db` holds. A connection always sees its own
uncommitted work, so a one-connection read-back is true whether or not anything was published
(CLAUDE.md; ADR 0025). Every "it was NOT written" assertion has a positive arm in the same test or
the one beside it, because a refusal test alone passes against a route that never writes at all.

The blank-name arms are the pair that matters: `'   '` must be refused and `'  Physics 101  '`
must be created AND stored with its padding. One direction alone is satisfied by a route that
trims — which is the behaviour `create_class` explicitly does not have.
"""

from __future__ import annotations

import inspect
import uuid
from functools import partial

import pytest

from gct.api.routers import classes as classes_router
from gct.api.routers import files as files_router
from gct.classes import CLASS_NAME_REASONS
from gct.staging import stage

CREATED_NAME = "Philosophy of Religion"


def _create(api, body):
    return api.client.post("/classes", json=body)


def _class_rows(db_other, owner_id: str) -> list[tuple]:
    return db_other.execute(
        "select class_id::text, name from classes where owner_id = %s", (owner_id,)
    ).fetchall()


def test_creating_a_class_publishes_a_row_another_connection_can_see(api, db_other):
    """The whole route in one pass: 201, the two-field body, and a row visible off-connection.

    `db`'s fixture seeds one class of its own under this owner, so the assertion is on the row
    this request added rather than on the table being empty beforehand.
    """
    before = {row[0] for row in _class_rows(db_other, api.owner_id)}

    response = _create(api, {"name": CREATED_NAME})

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"class_id", "name"}, "the response shape #109 reads changed"
    assert body["name"] == CREATED_NAME

    added = [row for row in _class_rows(db_other, api.owner_id) if row[0] not in before]
    assert added == [(body["class_id"], CREATED_NAME)], (
        "the returned class_id names no row a second connection can see"
    )
    # The id is a canonical uuid string, not psycopg's UUID rendered by pydantic: `create_class`
    # documents `str` as the shape the next call takes, and `POST /files` binds it into a cast.
    assert str(uuid.UUID(body["class_id"])) == body["class_id"]


def test_the_row_is_scoped_to_the_dependencys_owner_not_the_v1_constant(api, db_other):
    """F12 starts at the insert. `api`'s app overrides `owner_id` with `db`'s unique per-test
    owner, so a route that ignored the dependency and read `V1_OWNER_ID` (or anything else) would
    file the row somewhere this query cannot see it — and `db`'s owner-scoped teardown would then
    leak it besides."""
    response = _create(api, {"name": CREATED_NAME})

    assert response.status_code == 201, response.text
    owners = db_other.execute(
        "select owner_id from classes where class_id = %s::uuid",
        (response.json()["class_id"],),
    ).fetchall()
    assert owners == [(api.owner_id,)]


def test_an_owner_id_in_the_body_is_refused_and_files_nothing_under_it(api, db_other):
    """`NewClass` forbids extra fields, and `owner_id` is the field that makes that worth doing:
    dropped silently it would hand a caller who believes they chose an owner a row under someone
    else's, with no signal (F12; ADR 0004).

    Both directions, on ONE name: the same body without the field is a 201, so this is not
    passing because the route refuses everything.
    """
    intruder = f"{api.owner_id}-not-me"
    before = len(_class_rows(db_other, api.owner_id))

    refused = _create(api, {"name": CREATED_NAME, "owner_id": intruder})
    accepted = _create(api, {"name": CREATED_NAME})

    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["kind"] == "validation"
    assert any("owner_id" in str(entry) for entry in error["detail"]), error["detail"]
    assert accepted.status_code == 201, accepted.text

    assert _class_rows(db_other, intruder) == [], "a body field chose whose rows this created"
    assert len(_class_rows(db_other, api.owner_id)) == before + 1, (
        "the accepted create published nothing, or the refused one published a row"
    )


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", "\n", " \t\n "])
def test_a_blank_name_is_400_blank_name_and_publishes_nothing(api, db_other, blank):
    """`create_class` refuses `''` AND whitespace-only, and this route re-renders that one rule as
    one status. A pydantic `min_length=1` would split the set: 422 for `''`, 400 for `'   '` —
    two bodies behind one condition, which is what makes a client parse by trial.

    The library's sentence must NOT reach the wire: it names a Python function and a schema
    constraint, which is developer-facing (the same substitution `bad_class_id` makes in
    `files.py`).
    """
    before = len(_class_rows(db_other, api.owner_id))

    response = _create(api, {"name": blank})

    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["kind"] == "blank_name"
    assert "blank" in error["message"]
    assert "displayed" in error["message"], "the refusal does not say what to send instead"
    assert error["detail"] is None
    for leak in ("create_class", "NOT NULL", "classes.name", "schema"):
        assert leak not in error["message"], f"the library's own {leak!r} reached a client"
    assert len(_class_rows(db_other, api.owner_id)) == before, "a refused name published a row"


def test_a_name_with_surrounding_whitespace_is_created_and_stored_verbatim(api, db_other):
    """THE OTHER SIDE OF THE BLANK RULE, and the pin under the response's `name` echo.

    `create_class` refuses a name that is ONLY whitespace and stores an accepted one exactly as
    given — it does not trim. So a name that merely has padding must be a 201, the stored value
    must still carry the padding, and the echoed `name` must equal the stored one. A route (or a
    library) that started trimming would still pass the refusal test above and fail here, and
    the echo in `ClassCreated` would have quietly become a lie about what was saved.
    """
    padded = f"  {CREATED_NAME}  "

    response = _create(api, {"name": padded})

    assert response.status_code == 201, response.text
    assert response.json()["name"] == padded
    stored = db_other.execute(
        "select name from classes where class_id = %s::uuid", (response.json()["class_id"],)
    ).fetchall()
    assert stored == [(padded,)], "the stored name is not the name the client sent"


def test_a_missing_name_is_the_shared_422_envelope(api, db_other):
    """Shape is pydantic's job, not this route's: an absent `name` never reaches `create_class`,
    so it wears the framework's 422 with the field list in `detail` — and `detail` here IS
    structure, unlike the `blank_name` refusal, whose `detail` is null."""
    before = len(_class_rows(db_other, api.owner_id))

    response = _create(api, {})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["kind"] == "validation"
    assert isinstance(error["detail"], list)
    assert any("name" in str(entry) for entry in error["detail"])
    assert len(_class_rows(db_other, api.owner_id)) == before


def test_a_non_string_name_is_422_not_a_500(api, db_other):
    """`create_class` calls `name.strip()` with no type check of its own, so an `int` would raise
    `AttributeError` inside the handler and ship as a 500 — a bad request reported as a server
    fault. The request model is what keeps that unreachable, and this is the arm that says so."""
    before = len(_class_rows(db_other, api.owner_id))

    response = _create(api, {"name": 17})

    assert response.status_code == 422, response.text
    assert response.json()["error"]["kind"] == "validation"
    assert len(_class_rows(db_other, api.owner_id)) == before


def test_the_returned_class_id_is_one_post_files_accepts(api, db_other, monkeypatch, tmp_path):
    """THE SEAM #109 READS. The exit smoke creates a class over HTTP and uploads into it, so the
    id this route returns has to be the spelling `class_exists` and `enqueue` both take —
    `POST /files` binds it into a `::uuid` cast, and `uuid.UUID` accepts spellings that cast does
    not. Joining the two routes here is what proves the handle is usable rather than merely
    well-formed.

    `stage` is repointed at `tmp_path` — the REAL stager with a different directory, not a stub —
    so this test does not litter the shipped staging root. `test_files_router.py` owns the one
    test that pins the default location.
    """
    monkeypatch.setattr(files_router, "stage", partial(stage, staging_dir=tmp_path))

    created = _create(api, {"name": CREATED_NAME})
    assert created.status_code == 201, created.text
    class_id = created.json()["class_id"]

    upload = api.client.post(
        "/files",
        data={"class_id": class_id},
        files={"file": ("lecture-3.pdf", b"%PDF-1.4\nnever parsed here\n", "application/pdf")},
    )

    assert upload.status_code == 202, upload.text
    filed = db_other.execute(
        "select class_id::text, status from files where file_id = %s::uuid",
        (upload.json()["file_id"],),
    ).fetchall()
    assert filed == [(class_id, "queued")], "the created class did not accept an upload"


# --- Names PostgreSQL cannot store (issue #107's fix round) -----------------------------------

# THE BODIES ARE RAW BYTES, and that is the point of the whole section. `client.post(json=...)`
# CANNOT send these: httpx encodes the body as UTF-8 and a lone surrogate has no UTF-8 encoding,
# so the helper raises before a request exists. A real client does not have that problem, because
# JSON carries these as `\uXXXX` ESCAPES — the bytes below are pure ASCII, and `json.loads` turns
# them back into a lone surrogate and a NUL. Testing through `json=` would therefore have proved
# the defect unreachable when it is reachable from curl.
UNSTORABLE_BODIES = {
    "unpaired surrogate": rb'{"name": "Bio \ud800 101"}',
    "nul byte": rb'{"name": "Bio \u0000 101"}',
}
CONTROL_BODY = rb'{"name": "Bio 101"}'


def _post_raw(api, body: bytes):
    return api.client.post("/classes", content=body, headers={"content-type": "application/json"})


@pytest.mark.parametrize("label", sorted(UNSTORABLE_BODIES))
def test_a_name_postgres_cannot_store_is_400_unstorable_name_and_publishes_nothing(
    api, db_other, label
):
    """The two shipped defects, over the wire, both directions.

    Before the fix the surrogate body came back **400 `blank_name`** — a name that is plainly not
    blank, answered with the one remedy that could not fix it — and the NUL body came back **500
    `internal`**, a caller's bad request reported as a server fault. Both had the same cause: the
    handler caught `ValueError` to mean "blank", which is BROADER than the rule (it annexes
    `UnicodeEncodeError`) and NARROWER than the failure surface (it misses `psycopg.DataError`).

    The control arm is what makes this a pin rather than a coincidence: the same body with the bad
    character removed is a 201 and publishes a row, so the refusal cannot be passing because the
    route refuses everything. `db_other` counts rows, because a name Postgres cannot store is one
    you cannot bind into a SELECT either.
    """
    before = len(_class_rows(db_other, api.owner_id))

    refused = _post_raw(api, UNSTORABLE_BODIES[label])
    accepted = _post_raw(api, CONTROL_BODY)

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert error["kind"] == "unstorable_name"
    assert error["kind"] != "blank_name", "a name that is not blank was answered as blank"
    assert "blank" not in error["message"], "the remedy names a fix that cannot fix this request"
    assert "UTF-8" in error["message"], "the refusal does not say what to send instead"
    assert error["detail"] is None
    for leak in ("psycopg", "DataError", "validate_name", "codec"):
        assert leak not in error["message"], f"the library's own {leak!r} reached a client"

    assert accepted.status_code == 201, accepted.text
    assert len(_class_rows(db_other, api.owner_id)) == before + 1, (
        "the refused name published a row, or the control published none"
    )


def test_a_blank_name_and_an_unstorable_one_get_different_answers(api, db_other):
    """The distinction the fix exists to make, asserted as a difference rather than twice.

    Both are 400 and both publish nothing, so a test that only checked status and row count would
    pass against the broken route. What changed is that they no longer share a `kind` or a
    sentence — which is the difference between telling a client something true and something
    false. Reordering or merging the two rendering entries flips this.
    """
    before = len(_class_rows(db_other, api.owner_id))

    blank = _post_raw(api, rb'{"name": "   "}')
    unstorable = _post_raw(api, UNSTORABLE_BODIES["nul byte"])

    assert blank.status_code == unstorable.status_code == 400
    assert blank.json()["error"]["kind"] == "blank_name"
    assert unstorable.json()["error"]["kind"] == "unstorable_name"
    assert blank.json()["error"]["kind"] != unstorable.json()["error"]["kind"]
    assert blank.json()["error"]["message"] != unstorable.json()["error"]["message"]
    assert len(_class_rows(db_other, api.owner_id)) == before, "a refused name published a row"


def test_a_name_with_an_astral_character_is_created_over_http(api, db_other):
    """The direction that stops `unstorable_name` from becoming "anything unusual".

    An emoji is four bytes of perfectly storable UTF-8 and reaches the route the same way the
    surrogate does — as a `\\uXXXX` escape, a SURROGATE PAIR this time. A validator that refused
    surrogates without checking whether they PAIR would refuse this too, pass every test above,
    and break real class names. Read back through `db_other`, verbatim.
    """
    # The SURROGATE PAIR for U+1F9EC, which is exactly how a JSON client sends an astral
    # character - the same escape mechanism as the lone `\ud800` above, correctly paired.
    body = rb'{"name": "Bio 101 \ud83e\uddec"}'

    response = _post_raw(api, body)

    assert response.status_code == 201, response.text
    expected = "Bio 101 \N{DNA DOUBLE HELIX}"
    assert response.json()["name"] == expected
    stored = db_other.execute(
        "select name from classes where class_id = %s::uuid", (response.json()["class_id"],)
    ).fetchall()
    assert stored == [(expected,)]


def test_every_class_name_reason_has_a_rendering(api):
    """`CLASS_NAME_REASONS` is a closed set and `_NAME_REFUSAL` must cover it exactly.

    A reason the library adds without a sentence here would raise `KeyError` INSIDE the handler
    and ship as a 500 for what is a bad request — which is defect R2 all over again, in a new
    place. The map cannot guard itself, so this is the guard (the same one
    `test_every_staging_reason_has_a_status` provides for `files.py`).
    """
    assert set(classes_router._NAME_REFUSAL) == set(CLASS_NAME_REASONS)
    kinds = [kind for kind, _ in classes_router._NAME_REFUSAL.values()]
    assert len(set(kinds)) == len(kinds), "two reasons render as one kind, so a client cannot tell"
    for _, message in classes_router._NAME_REFUSAL.values():
        assert message.strip() and message[-1] == ".", "a rendering that is not a sentence"


def test_the_handler_does_not_catch_value_error_broadly(api):
    """WHY THE CATCH IS `ClassNameError`, pinned as source rather than as behaviour.

    Every behavioural arm above passes equally against `except (ValueError, DataError)` placed in
    the right order — the shape this fix considered and rejected. What that shape cannot survive
    is the NEXT `ValueError` from anywhere inside the call, which would again be rendered as a
    statement about the name. There is no input that demonstrates a bug that has not been written
    yet, so the pin is on the code: the handler catches the one class the validator raises.
    """
    source = inspect.getsource(classes_router.create)
    assert "except ClassNameError" in source
    assert "except ValueError" not in source, "the broad catch that shipped R1 is back"
    assert "DataError" not in source, "the adapter is catching psycopg's exceptions again"
