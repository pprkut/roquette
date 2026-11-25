# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The cover grid: every release of the library with its cover.

The cells are release ids in the collection's order; their text and
cover paths are read by id for the cells the view shows, as the track
list's (`roquette.modules.rows`).
"""

from array import array
from collections.abc import Callable, Iterable
from typing import NamedTuple

from roquette.library.index import ReleaseText
from roquette.modules.rows import Ahead, Load, RowText


class CoverText(NamedTuple):
    title: str
    artist: str
    year: str


def cover_text(text: ReleaseText) -> CoverText:
    return CoverText(text.title, text.albumartist, str(text.year) if text.year else "")


class CoverGrid:
    """The cells of the grid; `scale` repeats the library, as in TrackList."""

    def __init__(
        self,
        ids: array[int],
        load: Load[ReleaseText],
        cells_loaded: Callable[[list[int]], None],
        scale: int = 1,
    ) -> None:
        self._ids = ids
        self._scale = scale
        self.text: RowText[ReleaseText] = RowText(load, self._text_loaded)
        self._ahead = Ahead(self.text)
        self._cells_loaded = cells_loaded
        self._waiting: dict[int, set[int]] = {}
        self.current = -1

    def __len__(self) -> int:
        return len(self._ids) * self._scale

    def id(self, index: int) -> int:
        return self._ids[index % len(self._ids)]

    def release(self, index: int) -> ReleaseText | None:
        """The cell's release; None until it's read, then `cells_loaded`
        is called with the cells to show again."""
        release_id = self.id(index)
        text = self.text.get(release_id)
        self._ahead.shown(index, len(self), self.id)
        if text is None:
            self._waiting.setdefault(release_id, set()).add(index)
        return text

    def set_ids(self, ids: array[int]) -> None:
        self._ids = ids
        self._waiting.clear()
        self._ahead.reset()
        self.current = -1

    def navigate(self, key: str, columns: int) -> int:
        """The cell a navigation key moves the selection to."""
        last = len(self) - 1
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

    def close(self) -> None:
        self.text.close()

    def _text_loaded(self, ids: Iterable[int]) -> None:
        cells: list[int] = []
        for release_id in ids:
            cells.extend(self._waiting.pop(release_id, ()))
        if cells:
            self._cells_loaded(sorted(cells))
