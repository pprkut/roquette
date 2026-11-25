# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The track list: every track of the library in a sortable table.

The rows are track ids, in the collection's order or a query's; their
text is read by id for the rows the view shows (`roquette.modules.rows`).
Sorting by a column is a beets query with a sort, run in SQL.
"""

from array import array
from collections.abc import Callable, Iterable
from typing import NamedTuple

from roquette.library.index import TrackText
from roquette.modules.rows import Ahead, Load, RowText


class TrackRowText(NamedTuple):
    number: str
    title: str
    artist: str
    album: str
    length: str


EMPTY_ROW = TrackRowText("", "", "", "", "")


def format_length(seconds: float) -> str:
    total = round(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02}:{secs:02}"
    return f"{minutes}:{secs:02}"


def row_text(text: TrackText) -> TrackRowText:
    number = str(text.track) if text.disc <= 1 else f"{text.disc}.{text.track:02}"
    return TrackRowText(
        number, text.title, text.artist, text.album, format_length(text.length)
    )


# beets sorts by column: 0 the library's order, 1 title, 2 artist (by its
# sort name), 3 album, 4 length. The first field is the column's.
SORTS = ("", "title", "artist", "album disc track", "length")


def sort_query(column: int, ascending: bool) -> str:
    """The beets sort of a column, or "" for the library's order."""
    fields = SORTS[column].split()
    if not fields:
        return ""
    first, *rest = fields
    return " ".join([f"{first}{'+' if ascending else '-'}", *(f"{f}+" for f in rest)])


class TrackList:
    """The rows of the table.

    `scale` repeats the library that many times, to try the views with a
    larger library than the one at hand.
    """

    def __init__(
        self,
        ids: array[int],
        load: Load[TrackText],
        rows_loaded: Callable[[list[int]], None],
        scale: int = 1,
    ) -> None:
        self._ids = ids
        self._scale = scale
        self.text: RowText[TrackText] = RowText(load, self._text_loaded)
        self._ahead = Ahead(self.text)
        self._rows_loaded = rows_loaded
        # Rows shown without their text, by id.
        self._waiting: dict[int, set[int]] = {}
        self.sort_column = 0
        self.ascending = True
        self.current = -1

    def __len__(self) -> int:
        return len(self._ids) * self._scale

    def id(self, row: int) -> int:
        return self._ids[row % len(self._ids)]

    def row(self, row: int) -> TrackRowText:
        """The row's text; empty until it's read (or as it was before the
        library changed), then `rows_loaded` is called with the rows to
        show again."""
        item_id = self.id(row)
        text = self.text.get(item_id)
        self._ahead.shown(row, len(self), self.id)
        if text is None or self.text.is_stale(item_id):
            self._waiting.setdefault(item_id, set()).add(row)
        return EMPTY_ROW if text is None else row_text(text)

    def position(self, row: int) -> int | None:
        """The row's position in the index, if the rows are in its order."""
        if self.sort_column or not self.ascending:
            return None
        return row % len(self._ids)

    def track(self, row: int) -> TrackText | None:
        return self.text.get(self.id(row))

    def set_ids(self, ids: array[int]) -> None:
        """New rows: the library changed, or they're in the order of
        `sort`'s query (the library's order reversed, if so sorted)."""
        if self.sort_column == 0 and not self.ascending:
            ids = array(ids.typecode, reversed(ids))
        self._ids = ids
        self._waiting.clear()
        self._ahead.reset()
        self.current = -1

    def sort(self, column: int) -> str:
        """Sort by a column, again by the same column reverses the order;
        returns the beets query whose ids are the new rows (`set_ids`)."""
        if column == self.sort_column:
            self.ascending = not self.ascending
        else:
            self.sort_column = column
            self.ascending = True
        return sort_query(column, self.ascending)

    def navigate(self, key: str, page: int) -> int:
        """The row a navigation key moves the selection to."""
        last = len(self) - 1
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

    def close(self) -> None:
        self.text.close()

    def _text_loaded(self, ids: Iterable[int]) -> None:
        rows: list[int] = []
        for item_id in ids:
            rows.extend(self._waiting.pop(item_id, ()))
        if rows:
            self._rows_loaded(sorted(rows))
