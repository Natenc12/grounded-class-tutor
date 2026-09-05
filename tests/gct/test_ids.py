"""Pure tests for `gct.ids` - no DB, no network (#126).

The library's id boundaries are pinned at their own sites, where the connection and the rows are
there to assert against. This module pins the two things those tests can only observe indirectly:
that the LENIENT and STRICT shapes are genuinely different decisions, and that the exception set
`uuid.UUID` can raise is covered in full. That set is the reason this module exists at all -
`class_exists` and `get_file_status` each carried their own copy of it, each caught one of the
three, and the gap sat in the tree until #126.
"""

from __future__ import annotations

import uuid

import pytest

from gct.ids import canonical_uuid, require_canonical_uuid

CANONICAL = "6f1e1b4a-0f0e-4a3e-9a7d-2f0a1b2c3d4e"

# Every spelling `uuid.UUID` accepts for one id. Three of the four are spellings Postgres's
# `::uuid` cast also accepts; `urn` is the one it refuses, which is why binding the raw string is
# not the same thing as validating it.
SPELLINGS = {
    "plain": CANONICAL,
    "upper": CANONICAL.upper(),
    "braced": "{" + CANONICAL + "}",
    "hex32": CANONICAL.replace("-", ""),
    "urn": f"urn:uuid:{CANONICAL}",
}

# The three ways `uuid.UUID` reports a bad argument, and they are not interchangeable: catching
# only the first is exactly the defect this module was extracted to make un-repeatable.
REJECTED = {
    "malformed": "intro-to-religion",  # ValueError
    "none": None,  # TypeError
    "int": 12345,  # AttributeError - no `.replace`
    "list": ["not", "an", "id"],  # AttributeError
    "already_parsed": uuid.UUID(CANONICAL),  # AttributeError - the case that will happen
}


@pytest.mark.parametrize("spelling", sorted(SPELLINGS))
def test_canonical_uuid_normalises_every_spelling_the_parser_accepts(spelling):
    """Lenient: in comes any accepted spelling, out comes the single one Postgres takes."""
    assert canonical_uuid(SPELLINGS[spelling], fn="f", param="class_id", remedy="r") == CANONICAL, (
        spelling
    )


@pytest.mark.parametrize("spelling", sorted(SPELLINGS))
def test_require_canonical_uuid_accepts_only_the_canonical_spelling(spelling):
    """Strict: the opposite decision on the same inputs, which is what makes it a decision.

    `upper` is the deliberate exception, inherited from `ingest_file`'s shipped guard rather than
    re-decided here: `uuid.UUID` renders lowercase, so an all-caps hyphenated id differs in CASE
    alone and Postgres stores it as the same value.
    """
    accepted = spelling in {"plain", "upper"}
    if accepted:
        assert (
            require_canonical_uuid(SPELLINGS[spelling], fn="f", param="file_id", remedy="r")
            == SPELLINGS[spelling]
        )
    else:
        with pytest.raises(ValueError, match=r"f\(\) requires a canonical uuid file_id"):
            require_canonical_uuid(SPELLINGS[spelling], fn="f", param="file_id", remedy="r")


@pytest.mark.parametrize("kind", sorted(REJECTED))
def test_both_shapes_refuse_every_parser_rejection_with_the_remedy(kind):
    """All three exception types, both shapes, one `ValueError` carrying the caller's remedy.

    The `from exc` chain is asserted on the lenient shape because that is the half a maintainer
    would be tempted to simplify: the original cause is what tells a reader WHICH of the three
    rejections fired, and swallowing it costs nothing at the call site and everything in a
    traceback.
    """
    bad = REJECTED[kind]

    with pytest.raises(ValueError, match=r"caller\(\) requires a uuid class_id") as lenient:
        canonical_uuid(bad, fn="caller", param="class_id", remedy="pass the id create_class gave")
    assert "pass the id create_class gave" in str(lenient.value)
    assert isinstance(lenient.value.__cause__, (ValueError, AttributeError, TypeError))

    with pytest.raises(ValueError, match=r"caller\(\) requires a canonical uuid file_id") as strict:
        require_canonical_uuid(bad, fn="caller", param="file_id", remedy="pass str(id)")
    assert "pass str(id)" in str(strict.value)
