# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The collection's grouping: album artist, release, medium, track.

The index's tracks are in the collection's order (album artist,
release, disc, track), so a release is a run of tracks, and so is each
of its media. A group is therefore a range of the track list, kept as
columns too: where each group starts, its `album_id` and album artist.
Releases are beets albums (`album_id`), so a same-named album and
single stay apart; an album artist's singletons are a group of their
own (`album_id` 0), after the artist's releases, whatever their `album`
tag (the index's order, `roquette.library.index`). Media are found when
asked for, from the tracks' discs.
"""

from array import array
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import NamedTuple

from roquette.library.index import Tracks


class Medium(NamedTuple):
    """Tracks `start` to `stop` (exclusive) of the index."""

    disc: int
    start: int
    stop: int


@dataclass(frozen=True, slots=True)
class Groups:
    """Group `n` is tracks `starts[n]` to `starts[n + 1]` of the index."""

    starts: array[int] = field(default_factory=lambda: array("I", [0]))
    album_ids: array[int] = field(default_factory=lambda: array("q"))
    artists: array[int] = field(default_factory=lambda: array("I"))

    def __len__(self) -> int:
        return len(self.album_ids)

    def tracks(self, group: int) -> range:
        return range(self.starts[group], self.starts[group + 1])

    def of_track(self, position: int) -> int:
        """The group of the track at `position` in the index."""
        return bisect_right(self.starts, position, hi=len(self)) - 1

    def of_album(self, album_id: int) -> int | None:
        """The group of a release, or None if it has no tracks."""
        if not album_id:
            return None
        try:
            return self.album_ids.index(album_id)
        except ValueError:
            return None


def group(tracks: Tracks) -> Groups:
    groups = Groups()
    album_ids, artists = tracks.album_ids, tracks.artists
    for position in range(len(tracks)):
        album_id, artist = album_ids[position], artists[position]
        # The same release, or singletons of the same album artist.
        if (
            position
            and album_id == album_ids[position - 1]
            and (album_id or artist == artists[position - 1])
        ):
            continue
        if position:
            groups.starts.append(position)
        groups.album_ids.append(album_id)
        groups.artists.append(artist)
    if len(tracks):
        groups.starts.append(len(tracks))
    return groups


def media(tracks: Tracks, groups: Groups, group: int) -> list[Medium]:
    """A release's media; none for singletons."""
    if not groups.album_ids[group]:
        return []
    found: list[Medium] = []
    span = groups.tracks(group)
    begin = span.start
    discs = tracks.discs
    for position in range(span.start + 1, span.stop + 1):
        if position == span.stop or discs[position] != discs[begin]:
            found.append(Medium(discs[begin], begin, position))
            begin = position
    return found
