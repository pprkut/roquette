# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The cover grid: every release of the library with its cover."""

from collections.abc import Sequence
from typing import NamedTuple

from roquette.library.index import Release


class CoverText(NamedTuple):
    title: str
    artist: str
    year: str


class CoverGrid:
    """The cells of the grid; `scale` repeats the library, as in TrackList."""

    def __init__(self, releases: Sequence[Release], scale: int = 1) -> None:
        self._releases = releases
        self._count = len(releases) * scale
        self.current = -1

    def __len__(self) -> int:
        return self._count

    def release(self, index: int) -> Release:
        return self._releases[index % len(self._releases)]

    def text(self, index: int) -> CoverText:
        release = self.release(index)
        return CoverText(
            release.title,
            release.albumartist,
            str(release.year) if release.year else "",
        )

    def navigate(self, key: str, columns: int) -> int:
        """The cell a navigation key moves the selection to."""
        last = self._count - 1
        current = max(0, self.current)
        moves = {
            "left": current - 1,
            "right": current + 1,
            "up": current - columns,
            "down": current + columns,
            "home": 0,
            "end": last,
        }
        target = moves.get(key, current)
        # Up from the first row and down from the last stay where they are.
        if 0 <= target <= last or key in ("left", "right"):
            self.current = max(0, min(last, target))
        return self.current
