# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

import asyncio
from array import array
from collections.abc import Callable, Coroutine
from typing import Any

from roquette.library.index import ReleaseText, TrackText
from roquette.modules.covers import CoverGrid, cover_text
from roquette.modules.rows import RowText
from roquette.modules.tracks import (
    EMPTY_ROW,
    TrackList,
    format_length,
    row_text,
    sort_query,
)

TRACKS = {
    1: TrackText("Speak to Me", "Pink Floyd", "Dark Side", "Pink Floyd", 1, 1, 67),
    2: TrackText("Breathe", "Pink Floyd", "Dark Side", "Pink Floyd", 1, 2, 169),
    3: TrackText("avril 14th", "Aphex Twin", "Drukqs", "Aphex Twin", 2, 1, 121.6),
}


class Reader:
    """The reader worker's `track_text`: what was asked for, and when."""

    def __init__(self) -> None:
        self.asked: list[list[int]] = []

    async def __call__(self, ids: list[int]) -> dict[int, TrackText]:
        self.asked.append(ids)
        await asyncio.sleep(0)
        return {i: TRACKS[i] for i in ids if i in TRACKS}


def run(main: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(main())


def test_format_length() -> None:
    assert format_length(0) == "0:00"
    assert format_length(67.4) == "1:07"
    assert format_length(3723) == "1:02:03"


def test_row_text() -> None:
    assert row_text(TRACKS[1]) == (
        "1",
        "Speak to Me",
        "Pink Floyd",
        "Dark Side",
        "1:07",
    )
    # Discs after the first are shown.
    assert row_text(TRACKS[3]).number == "2.01"


def test_rows_are_read_when_shown() -> None:
    async def main() -> None:
        reader = Reader()
        loaded: list[list[int]] = []
        tracks = TrackList(array("q", [1, 2, 3]), reader, loaded.append)
        assert len(tracks) == 3
        # Empty until read; the rows asked for in one turn are read together.
        assert tracks.row(0) == EMPTY_ROW
        assert tracks.row(2) == EMPTY_ROW
        assert tracks.track(2) is None
        await asyncio.sleep(0.01)
        assert reader.asked == [[1, 3]]
        assert loaded == [[0, 2]]
        assert tracks.row(0).title == "Speak to Me"
        assert tracks.row(2).title == "avril 14th"
        assert reader.asked == [[1, 3]]
        tracks.close()

    run(main)


def test_scale_repeats_the_library() -> None:
    async def main() -> None:
        loaded: list[list[int]] = []
        tracks = TrackList(array("q", [1, 2, 3]), Reader(), loaded.append, scale=10)
        assert len(tracks) == 30
        assert tracks.id(29) == 3
        tracks.row(2)
        tracks.row(29)
        await asyncio.sleep(0.01)
        # Every row waiting for the id is shown again.
        assert loaded == [[2, 29]]

    run(main)


def test_position_in_the_index() -> None:
    tracks = TrackList(array("q", [1, 2, 3]), Reader(), lambda rows: None, scale=2)
    assert [tracks.position(row) for row in (0, 2, 4)] == [0, 2, 1]
    tracks.sort(0)
    assert tracks.position(0) is None
    tracks.sort(1)
    assert tracks.position(0) is None


def test_sort() -> None:
    async def main() -> None:
        tracks = TrackList(array("q", [1, 2, 3]), Reader(), lambda rows: None)
        assert tracks.sort(1) == "title+"
        assert tracks.sort(1) == "title-"
        assert not tracks.ascending
        assert tracks.sort(3) == "album+ disc+ track+"
        assert tracks.sort(0) == ""
        # The library's order, reversed.
        assert tracks.sort(0) == ""
        tracks.set_ids(array("q", [1, 2, 3]))
        assert [tracks.id(r) for r in range(3)] == [3, 2, 1]

    run(main)
    assert sort_query(4, False) == "length-"


def test_navigate() -> None:
    async def main() -> None:
        tracks = TrackList(array("q", [1, 2, 3]), Reader(), lambda rows: None)
        assert tracks.navigate("down", 10) == 0
        assert tracks.navigate("page-down", 10) == 2
        assert tracks.navigate("up", 10) == 1
        assert tracks.navigate("home", 10) == 0
        assert tracks.navigate("end", 10) == 2

    run(main)


def test_row_text_is_bounded() -> None:
    async def main() -> None:
        reader = Reader()
        text: RowText[TrackText] = RowText(reader, lambda ids: None, limit=2)
        for item_id in (1, 2, 3):
            text.get(item_id)
        await asyncio.sleep(0.01)
        assert len(text) == 2
        # The least recently asked for is gone, and read again.
        assert text.get(1) is None
        assert text.get(3) is not None
        await asyncio.sleep(0.01)
        assert reader.asked == [[1, 2, 3], [1]]

    run(main)


def test_row_text_missing_ids() -> None:
    """An id the reader doesn't find (deleted) isn't read again until
    the library changes."""

    async def main() -> None:
        reader = Reader()
        loaded: list[list[int]] = []
        text: RowText[TrackText] = RowText(reader, loaded.append)
        text.get(1)
        text.get(9)
        await asyncio.sleep(0.01)
        assert loaded == [[1]]
        assert text.get(9) is None
        await asyncio.sleep(0.01)
        assert reader.asked == [[1, 9]]
        text.clear()
        text.get(9)
        await asyncio.sleep(0.01)
        assert reader.asked == [[1, 9], [9]]

    run(main)


def test_row_text_cleared_while_reading() -> None:
    """Text read before the library changed isn't kept."""

    async def main() -> None:
        loaded: list[list[int]] = []
        text: RowText[TrackText] = RowText(Reader(), loaded.append)
        text.get(1)
        await asyncio.sleep(0)
        text.clear()
        await asyncio.sleep(0.01)
        assert loaded == []
        assert len(text) == 0

    run(main)


def test_cover_grid() -> None:
    release = ReleaseText("Drukqs", "Aphex Twin", 2001, None)

    async def load(ids: list[int]) -> dict[int, ReleaseText]:
        return {i: release for i in ids}

    async def main() -> None:
        loaded: list[list[int]] = []
        grid = CoverGrid(array("q", [7, 8]), load, loaded.append)
        assert grid.release(1) is None
        await asyncio.sleep(0.01)
        assert loaded == [[1]]
        assert grid.release(1) == release
        assert grid.navigate("right", 2) == 1
        assert grid.navigate("down", 2) == 1

    run(main)
    assert cover_text(release) == ("Drukqs", "Aphex Twin", "2001")
