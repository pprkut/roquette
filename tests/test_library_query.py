# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""beets queries as ids from SQL."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from roquette.library.config import load_plugins, read_config
from roquette.library.database import ReadLibrary, open_reader, open_writer
from roquette.library.index import load_index
from roquette.library.query import QueryError, Stopped, parse, query_ids
from tests.conftest import Library

# Flexible attributes: the roquette plugin's, and one no plugin types.
FLEXIBLE: dict[str, dict[str, object]] = {
    "Come Together": {"play_count": 12, "fmps_rating": 0.8, "mood": "happy"},
    "Something": {"play_count": 3, "mood": "sad"},
    "Hey You": {"play_count": 1, "fmps_rating": 1.0, "mood": "Happy"},
    "Xtal": {"fmps_rating": 0.6},
}


@pytest.fixture
def reader(library: Library, beets_dir: Path) -> Iterator[ReadLibrary]:
    with (beets_dir / "config.yaml").open("a", encoding="utf-8") as file:
        file.write("roquette:\n  library:\n    writable: yes\n")
    settings = read_config()
    load_plugins()
    writer = open_writer(settings)
    assert writer is not None
    for name, values in FLEXIBLE.items():
        item = writer.get_item(library.items[name])
        assert item is not None
        item.update(values)
        item.store()
    yield open_reader(settings)


def names(library: Library, ids: object) -> list[str]:
    by_id = {item_id: name for name, item_id in library.items.items()}
    return [by_id[i] for i in ids]  # type: ignore[attr-defined]


QUERIES = [
    "",
    "pink",
    "wish you",
    "year:1975",
    "year:1970..1980 floyd",
    "albumtype:single",
    "comp:true",
    "singleton:true",
    "title::^The",
    "artpath::Wall",
    "pink , beatles",
    "^pink",
    "play_count:2..",
    "fmps_rating:0.8..",
    "^play_count:1..",
    "mood:happy",
    "mood:happy play_count:5..",
    "mood:happy , xtal",
    "format:MP3",
]


@pytest.mark.parametrize("text", QUERIES)
def test_finds_what_beets_finds(
    library: Library, reader: ReadLibrary, text: str
) -> None:
    found = query_ids(reader, text)
    assert len(set(found)) == len(found)
    with reader.music_dir_context():
        assert set(found) == {item.id for item in reader.items(text)}


def test_in_the_collections_order(library: Library, reader: ReadLibrary) -> None:
    order = list(load_index(reader).tracks.ids)
    found = list(query_ids(reader, "floyd"))
    assert found == [i for i in order if i in found]
    assert list(query_ids(reader, "")) == order


def test_queries_in_sql(reader: ReadLibrary) -> None:
    assert parse(reader, "play_count:2.. fmps_rating:0.5..").slow is None
    assert parse(reader, "pink year:1975").slow is None
    # A flexible attribute no plugin roquette loaded types or queries.
    query = parse(reader, "mood:happy year:1960..")
    assert query.slow is not None
    assert "year" in query.where


def test_sort(library: Library, reader: ReadLibrary) -> None:
    found = names(library, query_ids(reader, "pink year- title+"))
    assert found == [
        "Wish You Were Here (hits)",
        "Hey You",
        "In the Flesh?",
        "Is There Anybody Out There?",
        "The Thin Ice",
        "Have a Cigar",
        "Wish You Were Here (album)",
        "Wish You Were Here (single)",
        # The singleton has no year.
        "Echoes (live)",
    ]
    assert parse(reader, "pink year-").sorted
    assert not parse(reader, "pink").sorted


def test_sort_by_artist_uses_the_sort_name(
    library: Library, reader: ReadLibrary
) -> None:
    found = names(library, query_ids(reader, "year:1969..1975 artist+"))
    assert found[:3] == ["Come Together", "Something", "Octopus's Garden"]


def test_sort_by_flexible_attributes(library: Library, reader: ReadLibrary) -> None:
    # Typed by the roquette plugin: numbers, in SQL; ties in the
    # collection's order, missing values as 0.
    query = parse(reader, "play_count-")
    assert query.slow_sort is None
    found = names(library, query_ids(reader, query))
    assert found[:3] == ["Come Together", "Something", "Hey You"]
    assert found[3:] == [
        n for n in names(library, query_ids(reader, "")) if n not in found[:3]
    ]
    # Without a type: text, case-insensitive.
    found = names(library, query_ids(reader, "mood:: mood+"))
    assert found[-1] == "Something"


def test_sort_by_computed_fields(library: Library, reader: ReadLibrary) -> None:
    """beets' getters can only sort in Python."""
    query = parse(reader, "singleton+ title-")
    assert query.slow_sort is not None
    found = names(library, query_ids(reader, query))
    assert found[-3:] == ["Xtal", "Echoes (live)", "Avril 14th"]
    assert found[0] == "Wish You Were Here (single)"


def test_slow_queries_stop(reader: ReadLibrary) -> None:
    with pytest.raises(Stopped):
        query_ids(reader, "mood:happy", stop=lambda: True)
    # Queries in SQL don't ask.
    assert query_ids(reader, "pink", stop=lambda: True)


@pytest.mark.parametrize("text", ['"unclosed', "year:abc", "title::("])
def test_invalid_queries(reader: ReadLibrary, text: str) -> None:
    with pytest.raises(QueryError):
        parse(reader, text)
