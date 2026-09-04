"""`POST /files` and `GET /files/{file_id}` over HTTP (issue #110): what an upload publishes,
what each refusal leaves behind, and what the status surface tells a student to do next.

Every "it was written" assertion goes through `db_other` — a THIRD connection here, held by
neither the handler (which built its own per request and closed it) nor `db` (which can see
uncommitted work). A one-connection read-back is true whether or not anything was published
(CLAUDE.md; ADR 0025), and with the API holding a connection of its own this is the easy mistake.

Every "it was NOT written" assertion has a positive arm somewhere in the file, because a refusal
test alone passes against a route that never writes at all.

Staging is repointed at `tmp_path` for the refusal tests — `_repoint_stage` swaps the router's
`stage` for the REAL function with a different directory, not for a stub, so what runs is the
shipped stager and only its output moves. Exactly one test leaves it alone, so the production
wiring (the default `gct.config.STAGING_DIR`) is pinned rather than assumed.
"""

from __future__ import annotations

import re
import shutil
import uuid
from functools import partial
from pathlib import Path

import pytest

from gct.api.routers import files as files_router
from gct.config import MAX_INGEST_WORDS, STAGING_DIR
from gct.staging import stage

PDF_BYTES = b"%PDF-1.4\nnot a real pdf, and this route does not care\n"


def _repoint_stage(monkeypatch, tmp_path: Path, **overrides) -> Path:
    """Run the real `stage` into `tmp_path` (and with `overrides`, e.g. a small `max_bytes`).

    `stage`'s directory and byte cap are DEFAULTS BOUND AT IMPORT (`def stage(..., max_bytes=
    MAX_STAGE_BYTES, staging_dir=STAGING_DIR)`), so monkeypatching `gct.config` after the fact
    changes nothing. Binding them onto the function the router calls is what actually moves them,
    and keeps the code under test the shipped one.
    """
    monkeypatch.setattr(files_router, "stage", partial(stage, staging_dir=tmp_path, **overrides))
    return tmp_path


def _upload(api, class_id: str, *, filename: str = "lecture-3.pdf", content: bytes = PDF_BYTES):
    return api.client.post(
        "/files",
        data={"class_id": class_id},
        files={"file": (filename, content, "application/pdf")},
    )


def _files_rows(db_other, owner_id: str) -> list[tuple]:
    return db_other.execute(
        "select file_id::text, class_id::text, filename, status, staging_ref "
        "from files where owner_id = %s",
        (owner_id,),
    ).fetchall()


def _staged_files(root: Path) -> list[Path]:
    """Every complete file under a staging root (`.part` never survives a successful `stage`)."""
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_an_upload_publishes_a_queued_file_and_its_job_and_stages_the_bytes(api, db_other):
    """The whole write path over HTTP, read back on a connection the handler never held.

    Deliberately UNPATCHED: this is the one test that proves the shipped default is where bytes
    actually land, so it cleans up the slot it created rather than borrowing `tmp_path`.
    """
    response = _upload(api, api.class_id)

    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {"file_id", "filename"}
    assert body["filename"] == "lecture-3.pdf"

    rows = _files_rows(db_other, api.owner_id)
    assert len(rows) == 1, "the upload published exactly one files row"
    file_id, class_id, filename, status, staging_ref = rows[0]
    staged = Path(staging_ref)
    try:
        assert (file_id, class_id, filename, status) == (
            body["file_id"],
            api.class_id,
            "lecture-3.pdf",
            "queued",
        )
        assert staged.is_absolute() and staged.is_relative_to(STAGING_DIR)
        assert staged.read_bytes() == PDF_BYTES, "the staged file is not the uploaded bytes"

        job = db_other.execute(
            "select state, owner_id, class_id::text from jobs where file_id = %s::uuid", (file_id,)
        ).fetchall()
        assert job == [("queued", api.owner_id, api.class_id)], (
            "a files row with no jobs row is a file no worker will ever claim"
        )
    finally:
        shutil.rmtree(staged.parent, ignore_errors=True)


def test_an_unknown_class_is_404_and_stages_nothing(api, db_other, monkeypatch, tmp_path):
    """The ownership check runs BEFORE `stage`, which is why this refusal leaves no orphan: an
    empty staging root is the observable difference between checking first and checking last."""
    root = _repoint_stage(monkeypatch, tmp_path)

    response = _upload(api, str(uuid.uuid4()))

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "kind": "class_not_found",
            "message": "No such class. Check the class id, or create the class before "
            "uploading to it.",
            "detail": None,
        }
    }
    assert _staged_files(root) == [], "a refused upload staged bytes nothing will ever collect"
    assert _files_rows(db_other, api.owner_id) == []


def test_another_owners_class_is_404_with_the_identical_body_and_no_row_crosses_owners(
    api, db_other, foreign_class, monkeypatch, tmp_path
):
    """F12: someone else's class must be indistinguishable from no class at all — same status,
    same body, byte for byte. A 403 (or any different message) tells an unauthenticated caller
    that the id is real, which is the leak.

    The second assertion is the one `test_the_foreign_key_alone_admits_the_cross_owner_write`
    (tests/gct/test_classes.py) shows is NOT free: the FK would have accepted this write.
    """
    root = _repoint_stage(monkeypatch, tmp_path)
    _, other_class_id = foreign_class

    theirs = _upload(api, other_class_id)
    nobodys = _upload(api, str(uuid.uuid4()))

    assert theirs.status_code == nobodys.status_code == 404
    assert theirs.json() == nobodys.json()
    assert _staged_files(root) == []
    assert (
        db_other.execute(
            "select count(*) from files where class_id = %s::uuid", (other_class_id,)
        ).fetchone()[0]
        == 0
    ), "an upload was queued into a class its uploader does not own"


def test_a_non_uuid_class_id_is_400_in_the_routes_own_words(api, db_other, monkeypatch, tmp_path):
    """`class_exists` raises `ValueError` here and its message is developer-facing — it explains a
    connection-abort concern a client cannot act on. The route substitutes its own sentence, and
    the internal one must not reach the wire."""
    root = _repoint_stage(monkeypatch, tmp_path)

    response = _upload(api, "not-a-uuid")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["kind"] == "bad_class_id"
    assert (
        error["message"]
        == "class_id must be a uuid. Use the id returned when the class was created."
    )
    assert "connection" not in error["message"], "the library's internal message reached a client"
    assert _staged_files(root) == []
    assert _files_rows(db_other, api.owner_id) == []


@pytest.mark.parametrize("spelling", ["plain", "braced", "hex32", "urn"])
def test_every_uuid_spelling_python_accepts_publishes_one_canonical_row(
    api, db_other, monkeypatch, tmp_path, spelling
):
    """The route parses `class_id` ONCE and hands the canonical form to both library calls.

    `uuid.UUID` accepts four spellings of the same id; Postgres's `::uuid` cast accepts three of
    them. `class_exists` normalises before it binds, so it answers True for all four - and
    `enqueue` binds `class_id` RAW into a cast, so the fourth (`urn:uuid:...`) used to reach it
    unchanged and abort the INSERT *after* `stage` had written the bytes: a 500, an orphaned
    staged file, and no id to poll. Every spelling must land on ONE row shape, which is what the
    canonical read-back asserts - not that Postgres renders uuids canonically (it always does),
    but that there is a row for it to render at all.
    """
    root = _repoint_stage(monkeypatch, tmp_path)
    canonical = uuid.UUID(api.class_id)
    written = {
        "plain": str(canonical),
        "braced": f"{{{canonical}}}",
        "hex32": canonical.hex,
        "urn": canonical.urn,
    }[spelling]
    assert uuid.UUID(written) == canonical, "the spelling under test names a different class"

    response = _upload(api, written)

    assert response.status_code == 202, response.text
    rows = _files_rows(db_other, api.owner_id)
    assert len(rows) == 1, f"the {spelling} spelling published no files row"
    file_id, class_id, _, status, _ = rows[0]
    assert (file_id, class_id, status) == (response.json()["file_id"], str(canonical), "queued")
    assert len(_staged_files(root)) == 1, "the row is published but the bytes are not staged"


def test_a_bad_filename_is_400_bad_filename_and_leaves_nothing_behind(
    api, db_other, monkeypatch, tmp_path
):
    """The real `validate_filename` refusal over HTTP: a path separator in the multipart filename.
    `kind` is the library's own reason token and `message` is `str(exc)` — the sentence plus the
    remedy — while `detail` stays null, because the envelope documents `detail` as STRUCTURE."""
    root = _repoint_stage(monkeypatch, tmp_path)

    response = _upload(api, api.class_id, filename="../evil.pdf")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["kind"] == "bad_filename"
    assert "path separator" in error["message"]
    assert "basename" in error["message"], "the refusal does not tell the student what to change"
    assert error["detail"] is None
    assert _staged_files(root) == [], "a refused name still wrote a file"
    assert _files_rows(db_other, api.owner_id) == []


def test_an_oversized_upload_is_413_too_large_and_leaves_nothing_behind(
    api, db_other, monkeypatch, tmp_path
):
    """The real byte bound, lowered rather than faked: `stage` refuses the moment the count
    EXCEEDS the cap, and 413 is the status for a well-formed request refused for its size."""
    root = _repoint_stage(monkeypatch, tmp_path, max_bytes=8)

    response = _upload(api, api.class_id, content=b"x" * 9)

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["kind"] == "too_large"
    assert "8-byte upload limit" in error["message"]
    assert "split the file" in error["message"]
    assert error["detail"] is None
    assert _staged_files(root) == [], "a truncated file survived a too_large refusal"
    assert _files_rows(db_other, api.owner_id) == []


def test_the_byte_bound_admits_a_file_exactly_at_the_cap(api, db_other, monkeypatch, tmp_path):
    """The other side of the same bound, so `too_large` above is not passing because the route
    refuses everything: `max_bytes` itself is admitted and publishes a row."""
    root = _repoint_stage(monkeypatch, tmp_path, max_bytes=8)

    response = _upload(api, api.class_id, content=b"x" * 8)

    assert response.status_code == 202, response.text
    assert [p.read_bytes() for p in _staged_files(root)] == [b"x" * 8]
    assert len(_files_rows(db_other, api.owner_id)) == 1


def test_a_missing_class_id_is_the_shared_422_envelope(api, monkeypatch, tmp_path):
    """The multipart parser is installed and wired: without `python-multipart` this module would
    not import at all, and with it a missing form field is FastAPI's validation failure, wearing
    the same envelope as everything else (`detail` here IS structure — pydantic's field list)."""
    _repoint_stage(monkeypatch, tmp_path)

    response = api.client.post("/files", files={"file": ("lecture-3.pdf", PDF_BYTES)})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["kind"] == "validation"
    assert isinstance(error["detail"], list)
    assert any("class_id" in str(entry) for entry in error["detail"])


@pytest.mark.parametrize("reason", sorted(files_router._STAGING_STATUS))
def test_every_staging_reason_has_a_status(reason):
    """`STAGING_REASONS` is a closed set and the route's map must cover it exactly — a reason
    added to the library without one here would raise `KeyError` inside the handler and ship as
    a 500 for what is a bad request."""
    from gct.staging import STAGING_REASONS

    assert set(files_router._STAGING_STATUS) == set(STAGING_REASONS)
    assert files_router._STAGING_STATUS[reason] in (400, 413)


def _constraint_members(db_other, conname: str) -> set[str]:
    """The member set of a `col in (...)` CHECK, read off `pg_catalog` — the schema's own answer.

    `pg_get_constraintdef` deparses it as `CHECK ((col = ANY (ARRAY['a'::text, ...])))`. The same
    technique `tests/gct/test_files.py` uses, for the same reason: a migration that widens the set
    widens this test with it, so the router's message maps cannot fall quietly behind the values
    the worker is allowed to write.
    """
    (definition,) = db_other.execute(
        "select pg_get_constraintdef(oid) from pg_constraint where conname = %s", (conname,)
    ).fetchone()
    members = set(re.findall(r"'([a-z_]+)'::text", definition))
    assert members, f"could not read the member set out of {definition!r}"
    return members


def _stamp(db_other, file_id: str, status: str, failed_reason: str | None = None) -> None:
    """Put the row in the state the worker would have left it in — the only honest way to reach
    `failed` here, since running the real worker is Slice 2's suite and a paid path besides."""
    db_other.execute(
        "update files set status = %s, failed_reason = %s where file_id = %s::uuid",
        (status, failed_reason, file_id),
    )


def _upload_one(api, monkeypatch, tmp_path) -> str:
    """A queued file over the real POST path, staged into `tmp_path`; returns its file_id."""
    _repoint_stage(monkeypatch, tmp_path)
    response = _upload(api, api.class_id)
    assert response.status_code == 202, response.text
    return response.json()["file_id"]


def test_the_status_of_a_fresh_upload_is_queued_and_says_so_in_words(
    api, db_other, monkeypatch, tmp_path
):
    """The two routes joined: POST hands back an id, GET answers about it. `message` is the field
    that makes this a status surface rather than a token dump."""
    file_id = _upload_one(api, monkeypatch, tmp_path)

    response = api.client.get(f"/files/{file_id}")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "filename": "lecture-3.pdf",
        "status": "queued",
        "failed_reason": None,
        "message": files_router._STATUS_MESSAGE["queued"],
    }


def test_every_status_the_constraint_admits_is_rendered(api, db_other, monkeypatch, tmp_path):
    """`files.status` has four legal values and every one of them must produce a sentence. Read
    off the CHECK rather than listed here: `failed` is the one the OTHER map answers, so the two
    maps together must cover the column exactly — no status can reach a `KeyError`."""
    statuses = _constraint_members(db_other, "files_status_check")
    assert set(files_router._STATUS_MESSAGE) | {"failed"} == statuses

    file_id = _upload_one(api, monkeypatch, tmp_path)
    for status in sorted(statuses - {"failed"}):
        _stamp(db_other, file_id, status)
        body = api.client.get(f"/files/{file_id}").json()
        assert (body["status"], body["failed_reason"]) == (status, None)
        assert body["message"] == files_router._STATUS_MESSAGE[status]


def test_every_terminal_reason_renders_actionably_and_is_a_200(
    api, db_other, monkeypatch, tmp_path
):
    """THE SLICE'S EXIT CRITERION. All six reasons the CHECK admits — five from the parser plus
    `transient_exhausted`, which the worker mints itself and `parse.TERMINAL_REASONS` does NOT
    contain — reach a student as their own distinct sentence naming what to do next.

    A failed file is a 200: it is a correctly recorded outcome, not a client error. Rendering it
    as a 4xx would be a lie about the student's materials, the same way rendering a grounder
    refusal as one would be (ADR 0014-0016).
    """
    reasons = _constraint_members(db_other, "files_failed_reason_check")
    assert len(reasons) == 6, reasons
    assert set(files_router._FAILED_MESSAGE) == reasons, (
        "a reason the database admits has no sentence, or a sentence names a reason it cannot"
    )

    file_id = _upload_one(api, monkeypatch, tmp_path)
    seen: set[str] = set()
    for reason in sorted(reasons):
        _stamp(db_other, file_id, "failed", reason)
        response = api.client.get(f"/files/{file_id}")

        assert response.status_code == 200, f"{reason} rendered as an error"
        body = response.json()
        assert (body["status"], body["failed_reason"]) == ("failed", reason)
        assert body["message"] == files_router._FAILED_MESSAGE[reason]
        assert body["message"] != files_router._UNKNOWN_FAILURE, f"{reason} fell back"
        assert len(body["message"]) > 60, f"{reason} has no advice in it"
        seen.add(body["message"])

    assert len(seen) == len(reasons), "two reasons render the same advice, so one is not specific"


def test_the_too_long_advice_names_the_bound_it_was_refused_by():
    """`too_long` is the one reason a student cannot act on without a number, and the number is
    ADR 0029's config constant — never a literal retyped in the router."""
    assert f"{MAX_INGEST_WORDS:,}-word" in files_router._FAILED_MESSAGE["too_long"]


def test_a_failed_row_with_no_reason_falls_back_instead_of_500(
    api, db_other, monkeypatch, tmp_path
):
    """The CHECK admits NULL, so `failed` with no reason is reachable, and a `KeyError` there
    would turn a real outcome into a 500 that tells the student nothing. The raw columns are
    returned beside the fallback, so nothing is hidden."""
    file_id = _upload_one(api, monkeypatch, tmp_path)
    _stamp(db_other, file_id, "failed", None)

    body = api.client.get(f"/files/{file_id}").json()

    assert (body["status"], body["failed_reason"]) == ("failed", None)
    assert body["message"] == files_router._UNKNOWN_FAILURE


def test_an_unknown_file_and_another_owners_file_are_the_same_404(
    api, db_other, foreign_class, monkeypatch, tmp_path
):
    """F12 on the read side. `get_file_status` returns one `None` for both cases, so this route
    cannot tell them apart even if it wanted to — and the bodies must be identical, or the
    difference itself is the leak. 404, never 403.
    """
    other_owner, other_class_id = foreign_class
    (theirs,) = db_other.execute(
        "insert into files (owner_id, class_id, filename, staging_ref, status) "
        "values (%s, %s::uuid, %s, %s, 'ready') returning file_id",
        (other_owner, other_class_id, "their-lecture.pdf", "/nowhere/their-lecture.pdf"),
    ).fetchone()

    someone_elses = api.client.get(f"/files/{theirs}")
    nobodys = api.client.get(f"/files/{uuid.uuid4()}")

    assert someone_elses.status_code == nobodys.status_code == 404
    assert someone_elses.json() == nobodys.json()
    assert someone_elses.json()["error"]["kind"] == "file_not_found"
    assert "their-lecture" not in someone_elses.text, "the other owner's filename leaked"


def test_a_non_uuid_file_id_is_400_in_the_routes_own_words(api):
    """Not 404: a malformed id names no file that could exist, so "that is not an id" is the
    actionable answer and it leaks nothing. The library's developer-facing message stays put."""
    response = api.client.get("/files/not-a-uuid")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["kind"] == "bad_file_id"
    assert error["message"] == (
        "file_id must be a uuid. Use the file_id returned when the file was uploaded."
    )
    assert "transaction" not in error["message"]
