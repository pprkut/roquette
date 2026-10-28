# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The compact index of a beets library: what the views list, not Items.

A beets `Item` carries every field and its flexible attributes, so the
index keeps tuples of the few fields it shows instead, with the strings
that repeat (artists, albums) interned (docs/PLAN.md, "Design rules").
For S1 it's read with one narrow query per table; S3 measures this
against beets' own API.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import beets.library
from beets import config


class Track(NamedTuple):
    id: int
    album_id: int
    disc: int
    track: int
    title: str
    artist: str
    album: str
    albumartist: str
    length: float


class Release(NamedTuple):
    id: int
    title: str
    albumartist: str
    year: int
    cover: Path | None


@dataclass(frozen=True, slots=True)
class LibraryIndex:
    tracks: list[Track]
    releases: list[Release]


def open_library() -> beets.library.Library:
    """The library of the beets config (`$BEETSDIR`)."""
    return beets.library.Library(
        config["library"].as_filename(), config["directory"].as_filename()
    )


# Sort by album artist (sort name first), release, disc and track: the
# collection's order.
_ALBUMARTIST_SORT = "COALESCE(NULLIF(albumartist_sort, ''), albumartist) COLLATE NOCASE"

_TRACKS = f"""
    SELECT id, album_id, disc, track, title, artist, album, albumartist, length
    FROM items
    ORDER BY {_ALBUMARTIST_SORT}, album COLLATE NOCASE, album_id, disc, track
"""

_RELEASES = f"""
    SELECT id, album, albumartist, year, artpath
    FROM albums
    ORDER BY {_ALBUMARTIST_SORT}, COALESCE(NULLIF(original_year, 0), year), album
"""


def load_index(library: beets.library.Library) -> LibraryIndex:
    """Read the index; blocking, so for a worker thread."""
    intern = sys.intern
    # Paths are stored relative to the music directory, which beets keeps
    # in a context variable that other threads don't inherit.
    artpath = beets.library.Album._fields["artpath"]
    with library.music_dir_context(), library.transaction() as tx:
        tracks = [
            Track(
                row[0],
                row[1] or 0,
                row[2] or 0,
                row[3] or 0,
                row[4] or "",
                intern(row[5] or ""),
                intern(row[6] or ""),
                intern(row[7] or ""),
                row[8] or 0.0,
            )
            for row in tx.query(_TRACKS)
        ]
        releases = [
            Release(
                row[0],
                row[1] or "",
                intern(row[2] or ""),
                row[3] or 0,
                _path(artpath.from_sql(row[4])),
            )
            for row in tx.query(_RELEASES)
        ]
    return LibraryIndex(tracks, releases)


def _path(value: bytes | None) -> Path | None:
    return Path(os.fsdecode(value)) if value else None
