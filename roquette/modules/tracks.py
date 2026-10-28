# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The track list: every track of the library in a sortable table."""

from array import array
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple

from roquette.library.index import Track


class TrackRowText(NamedTuple):
    number: str
    title: str
    artist: str
    album: str
    length: str


def format_length(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}"
    return f"{minutes}:{secs:02}"


# Sort keys by column: 0 the library's order, 1 title, 2 artist, 3 album,
# 4 length.
_KEYS: tuple[Callable[[Track], Any] | None, ...] = (
    None,
    lambda t: t.title.casefold(),
    lambda t: t.artist.casefold(),
    lambda t: t.album.casefold(),
    lambda t: t.length,
)


class TrackList:
    """The rows of the table, built when the view asks for them.

    `scale` repeats the library that many times, to try the views with a
    larger library than the one at hand.
    """

    def __init__(self, tracks: Sequence[Track], scale: int = 1) -> None:
        self._tracks = tracks
        self._count = len(tracks) * scale
        self._order = array("I", range(self._count))
        self.sort_column = 0
        self.ascending = True
        self.current = -1

    def __len__(self) -> int:
        return self._count

    def track(self, row: int) -> Track:
        return self._tracks[self._order[row] % len(self._tracks)]

    def row(self, row: int) -> TrackRowText:
        track = self.track(row)
        number = (
            str(track.track) if track.disc <= 1 else f"{track.disc}.{track.track:02}"
        )
        return TrackRowText(
            number, track.title, track.artist, track.album, format_length(track.length)
        )

    def sort(self, column: int) -> None:
        """Sort by a column; again by the same column reverses the order."""
        if column == self.sort_column:
            self.ascending = not self.ascending
        else:
            self.sort_column = column
            self.ascending = True
        key = _KEYS[column]
        tracks = self._tracks
        n = len(tracks)
        order: Sequence[int] = range(self._count)
        if key is not None:
            order = sorted(order, key=lambda i: key(tracks[i % n]))
        self._order = array("I", order if self.ascending else reversed(order))
        self.current = -1

    def navigate(self, key: str, page: int) -> int:
        """The row a navigation key moves the selection to."""
        last = self._count - 1
        current = self.current
        moves = {
            "up": current - 1,
            "down": current + 1,
            "page-up": current - page,
            "page-down": current + page,
            "home": 0,
            "end": last,
        }
        self.current = max(0, min(last, moves.get(key, current)))
        return self.current
