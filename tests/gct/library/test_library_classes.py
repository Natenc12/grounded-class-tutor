"""Creating, listing, renaming and deleting classes (#226).

Deleting a class removes its documents, pages, passages and search-index rows through the
schema's cascades, and the library still passes `check()` afterwards.
"""

from __future__ import annotations

import pytest

from gct.library import ClassNameError, Library, LibraryClass, UnknownClassError


def test_created_classes_are_listed_by_name(library: Library):
    hydrology = library.create_class("Hydrology")
    geology = library.create_class("geology")
    climate = library.create_class("Climate")

    assert library.list_classes() == [climate, geology, hydrology]


def test_a_name_is_stored_without_surrounding_whitespace(library: Library, library_path):
    created = library.create_class("  Bio 101 — Cells 🧬 \t")

    assert created.name == "Bio 101 — Cells 🧬"
    with Library(library_path) as reopened:
        assert reopened.list_classes() == [created]


def test_a_name_another_class_has_is_refused(library: Library):
    """Two classes that differ only by surrounding spaces would look the same on screen."""
    hydrology = library.create_class("Hydrology")

    for name in ("Hydrology", " Hydrology ", "Hydrology\n"):
        with pytest.raises(ClassNameError, match="already exists"):
            library.create_class(name)

    assert library.list_classes() == [hydrology]


def test_names_that_differ_only_in_case_are_two_classes(library: Library):
    assert len({library.create_class("Biology").id, library.create_class("biology").id}) == 2


def test_rename_to_a_taken_name_is_refused_but_to_its_own_name_works(library: Library):
    hydrology = library.create_class("Hydrology")
    geology = library.create_class("Geology")

    with pytest.raises(ClassNameError, match="already exists"):
        library.rename_class(geology.id, " Hydrology")

    assert library.rename_class(hydrology.id, "Hydrology ") == hydrology
    assert library.list_classes() == [geology, hydrology]


@pytest.mark.parametrize("name", ["", "   ", "\t\n", None, 123, "Bio\x00101", "Bio\ud800"])
def test_an_unusable_name_is_refused_and_nothing_is_written(library: Library, name):
    with pytest.raises(ClassNameError):
        library.create_class(name)

    assert library.list_classes() == []


def test_rename_changes_only_that_class(library: Library):
    hydrology = library.create_class("Hydrology")
    geology = library.create_class("Geology")

    renamed = library.rename_class(hydrology.id, "Water Resources")

    assert renamed == LibraryClass(id=hydrology.id, name="Water Resources")
    assert library.list_classes() == [geology, renamed]


def test_rename_refuses_a_blank_name(library: Library):
    hydrology = library.create_class("Hydrology")

    with pytest.raises(ClassNameError):
        library.rename_class(hydrology.id, " ")

    assert library.list_classes() == [hydrology]


def test_rename_and_delete_of_an_unknown_class_are_refused(library: Library):
    with pytest.raises(UnknownClassError):
        library.rename_class("no-such-class", "Anything")
    with pytest.raises(UnknownClassError):
        library.delete_class("no-such-class")


def test_deleting_a_class_removes_everything_under_it(
    library: Library, add_document, count, raw_connection
):
    hydrology = library.create_class("Hydrology")
    geology = library.create_class("Geology")
    add_document(hydrology.id, ["Rivers carry water.", "", "Aquifers store groundwater."])
    add_document(hydrology.id, ["Floods follow storms."], filename="lecture-02.pdf")
    add_document(geology.id, ["Rocks form slowly."])

    library.delete_class(hydrology.id)

    assert library.list_classes() == [geology]
    assert (count("documents"), count("pages"), count("passages")) == (1, 1, 1)
    assert raw_connection.execute(
        "SELECT count(*) FROM passages_fts WHERE passages_fts MATCH 'rivers OR floods'"
    ).fetchone() == (0,)
    assert library.check() == []


def test_a_deleted_class_is_gone_after_reopening(library: Library, library_path, add_document):
    hydrology = library.create_class("Hydrology")
    add_document(hydrology.id, ["Rivers carry water."])
    library.delete_class(hydrology.id)

    with Library(library_path) as reopened:
        assert reopened.list_classes() == []
        assert reopened.check() == []
