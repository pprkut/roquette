# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""A small beets library with what the library layer has to handle
(docs/PLAN.md, "Designing for testability"), and a beets config for it."""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import beets.library
import pytest
from beets import config, plugins
from beets.util import cached_classproperty


@dataclass(frozen=True)
class Release:
    album: str
    albumartist: str
    albumartist_sort: str
    year: int
    albumtype: str
    # (name, disc, track, fields); the name is the file's and the title.
    tracks: list[tuple[str, int, int, dict[str, Any]]]
    comp: bool = False


FLAC = {"format": "FLAC", "bitdepth": 16, "samplerate": 44100, "bitrate": 900_000}
HIRES = {"format": "FLAC", "bitdepth": 24, "samplerate": 96000, "bitrate": 2_800_000}
MP3 = {"format": "MP3", "bitdepth": 0, "samplerate": 44100, "bitrate": 320_000}

# "Wish You Were Here" is one recording in three versions: on the album
# (CD), on the single (hi-res) and on a compilation (MP3).
WYWH = "wywh-recording"

RELEASES = [
    Release(
        "Abbey Road",
        "The Beatles",
        "Beatles, The",
        1969,
        "album",
        [
            ("Come Together", 1, 1, FLAC),
            ("Something", 1, 2, FLAC),
            ("Octopus's Garden", 1, 5, FLAC),
        ],
    ),
    Release(
        "The Wall",
        "Pink Floyd",
        "",
        1979,
        "album",
        [
            ("In the Flesh?", 1, 1, FLAC),
            ("The Thin Ice", 1, 2, FLAC),
            ("Hey You", 2, 1, FLAC),
            ("Is There Anybody Out There?", 2, 2, FLAC),
        ],
    ),
    Release(
        "Wish You Were Here",
        "Pink Floyd",
        "",
        1975,
        "album",
        [
            ("Have a Cigar", 1, 3, FLAC),
            ("Wish You Were Here (album)", 1, 4, {**FLAC, "mb_trackid": WYWH}),
        ],
    ),
    # A single with the album's name: a release of its own.
    Release(
        "Wish You Were Here",
        "Pink Floyd",
        "",
        1975,
        "single",
        [("Wish You Were Here (single)", 1, 1, {**HIRES, "mb_trackid": WYWH})],
    ),
    Release(
        "Hits",
        "Various Artists",
        "",
        2001,
        "compilation",
        [
            (
                "Wish You Were Here (hits)",
                1,
                1,
                {**MP3, "mb_trackid": WYWH, "artist": "Pink Floyd"},
            ),
            ("Windowlicker", 1, 2, {**FLAC, "artist": "Aphex Twin"}),
        ],
        comp=True,
    ),
]

# Singletons as `beet import -s` leaves them: with their files' album
# tags, and mostly without an album artist.
SINGLETONS: list[tuple[str, dict[str, Any]]] = [
    ("Avril 14th", {"artist": "Aphex Twin", "album": "Drukqs", "albumartist": ""}),
    ("Xtal", {"artist": "Aphex Twin", "album": "", "albumartist": ""}),
    (
        "Echoes (live)",
        {"artist": "Pink Floyd", "album": "Pompeii", "albumartist": "Pink Floyd"},
    ),
]


@dataclass(frozen=True)
class Library:
    path: Path
    music: Path
    # Item ids by name; album ids by (album, albumtype).
    items: dict[str, int]
    albums: dict[tuple[str, str], int]


def build_library(path: Path, music: Path) -> Library:
    backups = config["create_backup_before_migrations"].get()
    config["create_backup_before_migrations"] = False
    library = beets.library.Library(str(path), str(music))
    config["create_backup_before_migrations"] = backups
    items: dict[str, int] = {}
    albums: dict[tuple[str, str], int] = {}
    length = 100.0
    for release in RELEASES:
        made = []
        for name, disc, track, fields in release.tracks:
            length += 1
            values = {
                "artist": release.albumartist,
                **fields,
                "title": name,
                "album": release.album,
                "albumartist": release.albumartist,
                "albumartist_sort": release.albumartist_sort,
                "artist_sort": release.albumartist_sort,
                "year": release.year,
                "albumtype": release.albumtype,
                "comp": release.comp,
                "disc": disc,
                "disctotal": max(d for _, d, _, _ in release.tracks),
                "track": track,
                "length": length,
            }
            made.append(item(music, name, **values))
        album = library.add_album(made)
        album.artpath = os.fsencode(music / release.album / "cover.jpg")
        album.store()
        assert album.id is not None
        albums[(release.album, release.albumtype)] = album.id
        for added in made:
            assert added.id is not None
            items[added.title] = added.id
    for name, fields in SINGLETONS:
        length += 1
        added = item(music, name, title=name, length=length, **FLAC, **fields)
        library.add(added)
        assert added.id is not None
        items[name] = added.id
    library._close()
    return Library(path, music, items, albums)


def item(music: Path, name: str, **fields: Any) -> beets.library.Item:
    return beets.library.Item(path=os.fsencode(music / f"{name}.flac"), **fields)


@pytest.fixture
def beets_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No plugins loaded, and beets' global state reset afterwards."""
    monkeypatch.setattr(plugins, "_instances", [])
    cached_classproperty.cache.clear()
    yield
    config.clear()
    config.read(user=False)
    cached_classproperty.cache.clear()


@pytest.fixture
def beets_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, beets_state: None
) -> Path:
    """A beets config directory in `$BEETSDIR` for the test library, with
    the roquette plugin; the library isn't made (see `library`)."""
    directory = tmp_path / "beets"
    directory.mkdir()
    repository = Path(__file__).resolve().parent.parent
    (directory / "config.yaml").write_text(
        f"directory: {tmp_path / 'music'}\n"
        "library: library.db\n"
        f"pluginpath: [{repository / 'beetsplug'}]\n"
        "plugins: [roquette, info]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BEETSDIR", str(directory))
    return directory


@pytest.fixture
def library(beets_dir: Path, tmp_path: Path) -> Library:
    """The test library, in the config of `beets_dir`."""
    return build_library(beets_dir / "library.db", tmp_path / "music")
