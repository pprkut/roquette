# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The columnar index, the grouping, the recording index, text by id."""

import threading
from pathlib import Path

import pytest

from roquette.library import grouping, recordings
from roquette.library.config import read_config
from roquette.library.database import ReadLibrary, open_reader
from roquette.library.index import (
    LibraryIndex,
    TrackText,
    load_index,
    load_playables,
    release_text,
    track_text,
)
from roquette.playback.gain import ReplayGain
from tests.conftest import WYWH, Library

COLLECTION = [
    # Singletons after an artist's releases (here none), by title.
    ("Aphex Twin", ["Avril 14th", "Xtal"]),
    # By the album artist's sort name.
    ("The Beatles", ["Come Together", "Something", "Octopus's Garden"]),
    # Releases by year, then title; the album and the single apart.
    ("Pink Floyd", ["Have a Cigar", "Wish You Were Here (album)"]),
    ("Pink Floyd", ["Wish You Were Here (single)"]),
    (
        "Pink Floyd",
        ["In the Flesh?", "The Thin Ice", "Hey You", "Is There Anybody Out There?"],
    ),
    ("Pink Floyd", ["Echoes (live)"]),
    ("Various Artists", ["Wish You Were Here (hits)", "Windowlicker"]),
]


@pytest.fixture
def reader(library: Library) -> ReadLibrary:
    return open_reader(read_config())


@pytest.fixture
def index(reader: ReadLibrary) -> LibraryIndex:
    return load_index(reader)


def titles(library: Library, ids: object) -> list[str]:
    names = {item_id: name for name, item_id in library.items.items()}
    return [names[i] for i in ids]  # type: ignore[attr-defined]


def test_index_in_the_collections_order(library: Library, index: LibraryIndex) -> None:
    tracks = index.tracks
    assert titles(library, tracks.ids) == [t for _, ts in COLLECTION for t in ts]
    expected_artists = [a for a, ts in COLLECTION for _ in ts]
    assert [index.artists[a] for a in tracks.artists] == expected_artists
    wall = library.albums[("The Wall", "album")]
    positions = [n for n, a in enumerate(tracks.album_ids) if a == wall]
    assert [tracks.discs[n] for n in positions] == [1, 1, 2, 2]
    assert [tracks.numbers[n] for n in positions] == [1, 2, 1, 2]
    # Singletons have no album.
    assert tracks.album_ids[0] == 0
    assert all(length > 100 for length in tracks.lengths)


def test_releases(library: Library, index: LibraryIndex) -> None:
    releases = index.releases
    by_id = {v: k for k, v in library.albums.items()}
    assert [by_id[i] for i in releases.ids] == [
        ("Abbey Road", "album"),
        ("Wish You Were Here", "album"),
        ("Wish You Were Here", "single"),
        ("The Wall", "album"),
        ("Hits", "compilation"),
    ]
    assert [index.artists[a] for a in releases.artists][:2] == [
        "The Beatles",
        "Pink Floyd",
    ]
    assert list(releases.years) == [1969, 1975, 1975, 1979, 2001]


def test_grouping(library: Library, index: LibraryIndex) -> None:
    groups = grouping.group(index.tracks)
    assert len(groups) == len(COLLECTION)
    for number, (artist, names) in enumerate(COLLECTION):
        span = groups.tracks(number)
        assert titles(library, index.tracks.ids[span.start : span.stop]) == names
        assert index.artists[groups.artists[number]] == artist
    wall = library.albums[("The Wall", "album")]
    group = groups.of_album(wall)
    assert group == 4
    start = groups.starts[group]
    assert grouping.media(index.tracks, groups, group) == [
        grouping.Medium(1, start, start + 2),
        grouping.Medium(2, start + 2, start + 4),
    ]
    assert grouping.media(index.tracks, groups, 0) == []
    assert groups.of_track(start + 3) == group
    assert groups.of_track(0) == 0
    assert groups.of_track(len(index.tracks) - 1) == len(groups) - 1
    assert groups.of_album(0) is None
    assert groups.of_album(9999) is None


def test_grouping_of_nothing() -> None:
    from roquette.library.index import Tracks

    groups = grouping.group(Tracks())
    assert len(groups) == 0


def test_recording_versions(library: Library, reader: ReadLibrary) -> None:
    versions = recordings.load_versions(reader)
    assert {v.recording for v in versions} == {WYWH}
    index = recordings.index_versions(versions)
    best_first = titles(library, index[library.items["Wish You Were Here (hits)"]])
    # Lossless first, then bit depth: the hi-res single, the CD, the MP3.
    assert best_first == [
        "Wish You Were Here (single)",
        "Wish You Were Here (album)",
        "Wish You Were Here (hits)",
    ]
    # One tuple shared by the versions; other tracks aren't indexed.
    assert len(index) == 3
    assert len({id(v) for v in index.values()}) == 1


def test_versions_by_quality_then_length() -> None:
    index = recordings.index_versions(
        [
            recordings.Version(1, "r", "MP3", 0, 44100, 320_000, 200.0),
            recordings.Version(2, "r", "FLAC", 16, 44100, 900_000, 200.0),
            recordings.Version(3, "r", "FLAC", 24, 96000, 2_800_000, 199.0),
            recordings.Version(4, "r", "FLAC", 16, 44100, 900_000, 203.0),
            recordings.Version(5, "alone", "FLAC", 16, 44100, 900_000, 203.0),
        ]
    )
    assert index[1] == (3, 4, 2, 1)
    assert 5 not in index


def test_text_by_id(library: Library, reader: ReadLibrary) -> None:
    ids = [library.items["Hey You"], library.items["Windowlicker"], 9999]
    text = track_text(reader, ids)
    assert text == {
        ids[0]: TrackText("Hey You", "Pink Floyd", "The Wall", "Pink Floyd", 2, 1, 106),
        ids[1]: TrackText(
            "Windowlicker", "Aphex Twin", "Hits", "Various Artists", 1, 2, 112
        ),
    }
    wall = library.albums[("The Wall", "album")]
    releases = release_text(reader, [wall])
    assert releases[wall].title == "The Wall"
    assert releases[wall].albumartist == "Pink Floyd"
    assert releases[wall].year == 1979
    # Stored relative to the music directory, read back absolute.
    assert releases[wall].cover == library.music / "The Wall" / "cover.jpg"


def test_text_by_id_on_another_thread(library: Library, reader: ReadLibrary) -> None:
    """The music directory is a context variable threads don't inherit."""
    wall = library.albums[("The Wall", "album")]
    result: list[Path | None] = []
    thread = threading.Thread(
        target=lambda: result.append(release_text(reader, [wall])[wall].cover)
    )
    thread.start()
    thread.join()
    assert result == [library.music / "The Wall" / "cover.jpg"]


def test_text_of_many(library: Library, reader: ReadLibrary) -> None:
    """More ids than SQLite takes in one statement."""
    ids = [*library.items.values(), *range(10_000, 12_000)]
    assert len(track_text(reader, ids)) == len(library.items)


def test_playables(library: Library, beets_dir: Path) -> None:
    from roquette.library.database import open_writer

    with (beets_dir / "config.yaml").open("a", encoding="utf-8") as file:
        file.write("roquette:\n  library:\n    writable: yes\n")
    settings = read_config()
    writer = open_writer(settings)
    assert writer is not None
    first, second = library.items["Come Together"], library.items["Something"]
    item = writer.get_item(first)
    assert item is not None
    item.update({"rg_track_gain": -6.5, "rg_track_peak": 0.98, "rg_album_gain": -7.0})
    item.store()
    reader = open_reader(settings)
    # In the order asked for; ids not in the library are left out.
    playables = load_playables(reader, [second, 9999, first])
    assert [p.id for p in playables] == [second, first]
    assert playables[1].path == library.music / "Come Together.flac"
    assert playables[1].album_id == library.albums[("Abbey Road", "album")]
    assert playables[1].replaygain == ReplayGain(-6.5, 0.98, -7.0, None)
    assert playables[1].text[:4] == (
        "Come Together",
        "The Beatles",
        "Abbey Road",
        "The Beatles",
    )
    assert playables[0].replaygain.empty
