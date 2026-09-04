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

import uuid
from functools import partial

import pytest

from gct.api.routers import files as files_router
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
