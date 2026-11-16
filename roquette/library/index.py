# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The resident index of a beets library: columns of numbers.

A beets `Item` carries every field and its flexible attributes, and
even S1's tuples of the text a view shows cost 660 bytes a track, 74 MiB
for ten times the library. The index keeps the numbers it sorts and
groups by as `array`s in the collection's order (album artist, release,
medium, track), about 30 bytes a track; the text the views show is read
by id for the rows on screen (`track_text`, `release_text`). Album
artists are the one text kept, once each, for the grouping and the
tree. Both are read with narrow SQL, never through Items
(docs/decisions/0003-library.md).
"""

import os
from array import array
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import beets.library

from roquette.library.order import ALBUMARTIST_SORT, ARTIST, COLLECTION_ORDER
from roquette.playback.gain import ReplayGain

_TRACKS = f"""
    SELECT id, album_id, {ARTIST}, disc, track, length
    FROM items
    ORDER BY {COLLECTION_ORDER}
"""

_RELEASES = f"""
    SELECT id, albumartist, year
    FROM albums
    ORDER BY {ALBUMARTIST_SORT}, albumartist,
        COALESCE(NULLIF(original_year, 0), year),
        album COLLATE NOCASE, id
"""


@dataclass(frozen=True, slots=True)
class Tracks:
    """The tracks in the collection's order; `artist` is the number of
    the album artist (or the artist of a singleton without one)."""

    ids: array[int] = field(default_factory=lambda: array("q"))
    album_ids: array[int] = field(default_factory=lambda: array("q"))
    artists: array[int] = field(default_factory=lambda: array("I"))
    discs: array[int] = field(default_factory=lambda: array("h"))
    numbers: array[int] = field(default_factory=lambda: array("h"))
    lengths: array[float] = field(default_factory=lambda: array("f"))

    def __len__(self) -> int:
        return len(self.ids)


@dataclass(frozen=True, slots=True)
class Releases:
    """The releases (beets albums) in the collection's order."""

    ids: array[int] = field(default_factory=lambda: array("q"))
    artists: array[int] = field(default_factory=lambda: array("I"))
    years: array[int] = field(default_factory=lambda: array("h"))

    def __len__(self) -> int:
        return len(self.ids)


@dataclass(frozen=True, slots=True)
class LibraryIndex:
    tracks: Tracks
    releases: Releases
    # The album artists' names, by their number.
    artists: list[str]


def rows(
    library: beets.library.Library, sql: str, subvals: Sequence[object] = ()
) -> Iterator[tuple[Any, ...]]:
    """The rows of a query as plain tuples, one at a time, within a
    transaction of the caller's."""
    cursor = library._connection().execute(sql, subvals)
    cursor.row_factory = None
    return iter(cursor)


def load_index(library: beets.library.Library) -> LibraryIndex:
    """Read the index; blocking, so for the reader worker."""
    tracks = Tracks()
    releases = Releases()
    artists: list[str] = []
    numbers: dict[str, int] = {}

    def artist(name: object) -> int:
        text = str(name or "")
        number = numbers.get(text)
        if number is None:
            number = numbers[text] = len(artists)
            artists.append(text)
        return number

    with library.transaction():
        for row in rows(library, _TRACKS):
            tracks.ids.append(_int(row[0]))
            tracks.album_ids.append(_int(row[1]))
            tracks.artists.append(artist(row[2]))
            tracks.discs.append(_short(row[3]))
            tracks.numbers.append(_short(row[4]))
            tracks.lengths.append(_float(row[5]))
        for row in rows(library, _RELEASES):
            releases.ids.append(_int(row[0]))
            releases.artists.append(artist(row[1]))
            releases.years.append(_short(row[2]))
    return LibraryIndex(tracks, releases, artists)


class TrackText(NamedTuple):
    """What the views show of a track."""

    title: str
    artist: str
    album: str
    # A singleton's artist when it has no album artist.
    albumartist: str
    disc: int
    track: int
    length: float


_TEXT = f"title, artist, album, {ARTIST}, disc, track, length"


def _text(row: tuple[Any, ...], start: int) -> TrackText:
    return TrackText(
        _str(row[start]),
        _str(row[start + 1]),
        _str(row[start + 2]),
        _str(row[start + 3]),
        _int(row[start + 4]),
        _int(row[start + 5]),
        _float(row[start + 6]),
    )


class ReleaseText(NamedTuple):
    title: str
    albumartist: str
    year: int
    cover: Path | None


# SQLite's limit of variables in a statement is 999 in older builds.
CHUNK = 900


def chunks(ids: Sequence[int], size: int = CHUNK) -> Iterator[list[int]]:
    for start in range(0, len(ids), size):
        yield list(ids[start : start + size])


def _by_id(
    library: beets.library.Library, sql: str, ids: Sequence[int]
) -> Iterator[tuple[Any, ...]]:
    with library.music_dir_context(), library.transaction():
        for chunk in chunks(ids):
            yield from rows(library, sql.format(", ".join("?" * len(chunk))), chunk)


def track_text(
    library: beets.library.Library, ids: Iterable[int]
) -> dict[int, TrackText]:
    """The text of the tracks `ids` shows; ids not in the library are
    left out. Blocking, for the reader worker."""
    sql = f"SELECT id, {_TEXT} FROM items WHERE id IN ({{}})"
    return {_int(row[0]): _text(row, 1) for row in _by_id(library, sql, list(ids))}


def release_text(
    library: beets.library.Library, ids: Iterable[int]
) -> dict[int, ReleaseText]:
    """The text and cover of the releases `ids`, as `track_text`."""
    sql = "SELECT id, album, albumartist, year, artpath FROM albums WHERE id IN ({})"
    artpath = beets.library.Album._fields["artpath"]
    found = {}
    # Paths are stored relative to the music directory.
    for row in _by_id(library, sql, list(ids)):
        cover = artpath.from_sql(row[4])
        found[_int(row[0])] = ReleaseText(
            _str(row[1]),
            _str(row[2]),
            _int(row[3]),
            Path(os.fsdecode(cover)) if cover else None,
        )
    return found


class Playable(NamedTuple):
    """What playing a track needs from beets."""

    id: int
    album_id: int
    path: Path
    length: float
    replaygain: ReplayGain
    text: TrackText


_PLAYABLE = f"""
    SELECT id, album_id, path, length,
           rg_track_gain, rg_track_peak, rg_album_gain, rg_album_peak,
           {_TEXT}
    FROM items
    WHERE id IN ({{}})
"""


def load_playables(
    library: beets.library.Library, ids: Sequence[int]
) -> list[Playable]:
    """The tracks `ids`, in that order; ids that aren't in the library
    are left out. Blocking, for the reader worker."""
    path_field = beets.library.Item._fields["path"]
    found: dict[int, Playable] = {}
    for row in _by_id(library, _PLAYABLE, ids):
        path = path_field.from_sql(row[2])
        found[_int(row[0])] = Playable(
            _int(row[0]),
            _int(row[1]),
            Path(os.fsdecode(path)),
            _float(row[3]),
            ReplayGain.from_fields(
                {
                    "rg_track_gain": row[4],
                    "rg_track_peak": row[5],
                    "rg_album_gain": row[6],
                    "rg_album_peak": row[7],
                }
            ),
            _text(row, 8),
        )
    return [found[i] for i in ids if i in found]


# beets' columns can hold NULL, or values of another type than declared.
def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _short(value: Any) -> int:
    return max(-32768, min(32767, _int(value)))


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return "" if value is None else str(value)
