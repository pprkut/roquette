# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The collection's grouping: release, medium, track.

The index's tracks are sorted in the collection's order (album artist,
release, disc, track), so a release is a run of tracks, and so is each of
its media. A group is therefore a range of the track list, not a list of
its own: a few integers per release. Releases are beets albums
(`album_id`); an album artist's singletons are a group of their own,
after the artist's releases, whatever their `album` tag (the index's
order, `roquette.library.index`).

Prototype of S3, which measures it (scripts/measure-library.py).
"""

from collections.abc import Sequence
from typing import NamedTuple

from roquette.library.index import Track


class Medium(NamedTuple):
    disc: int
    start: int
    stop: int


class Group(NamedTuple):
    """Tracks `start` to `stop` (exclusive) of the index.

    `album_id` is 0 for an album artist's singletons.
    """

    album_id: int
    albumartist: str
    start: int
    stop: int
    media: tuple[Medium, ...]


def group(tracks: Sequence[Track]) -> list[Group]:
    groups: list[Group] = []
    start = 0
    count = len(tracks)
    while start < count:
        first = tracks[start]
        stop = start + 1
        while stop < count and _same_group(first, tracks[stop]):
            stop += 1
        groups.append(
            Group(
                first.album_id,
                first.albumartist,
                start,
                stop,
                _media(tracks, start, stop) if first.album_id else (),
            )
        )
        start = stop
    return groups


def _same_group(first: Track, track: Track) -> bool:
    if first.album_id:
        return track.album_id == first.album_id
    return not track.album_id and track.albumartist == first.albumartist


def _media(tracks: Sequence[Track], start: int, stop: int) -> tuple[Medium, ...]:
    media: list[Medium] = []
    begin = start
    for i in range(start + 1, stop + 1):
        if i == stop or tracks[i].disc != tracks[begin].disc:
            media.append(Medium(tracks[begin].disc, begin, i))
            begin = i
    return tuple(media)
