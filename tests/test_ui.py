# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The .slint files compile; showing them needs a display (from M5 on).

Creating the window without one isn't safe either (Slint then renders to
the screen through KMS), so the window's logic is tested with a stand-in
for the Slint component.
"""

import asyncio
import sqlite3
from array import array
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from roquette.library.index import Playable, TrackText
from roquette.measure import Stopwatch
from roquette.modules.tracks import TrackList
from roquette.playback.gain import ReplayGain
from roquette.playback.source import Source
from roquette.threads import Inbox
from roquette.ui import window as ui
from roquette.ui.window import WindowOptions, load_ui, volume_factor


@pytest.mark.parametrize("name", ["main_window", "empty_window"])
def test_compiles(name: str) -> None:
    load_ui(name)


def test_volume_factor() -> None:
    assert volume_factor(0) == 0.0
    assert volume_factor(100) == 1.0
    assert volume_factor(50) == 0.125
    assert volume_factor(150) == 1.0


class FakeEngine:
    def __init__(self) -> None:
        self.played: list[list[Path]] = []

    def play(self, sources: Sequence[Source], index: int = 0) -> None:
        self.played.append([source.path for source in sources])


class FakeLibrary:
    """The library service's `playables`; the look-up of track 1 is slow."""

    def __init__(self) -> None:
        self.slow = asyncio.Event()

    async def playables(self, ids: Sequence[int]) -> list[Playable]:
        if list(ids) == [1]:
            await self.slow.wait()
        text = TrackText("Track", "Artist", "", "Artist", 1, 1, 60.0)
        return [
            Playable(i, 0, Path(f"{i}.flac"), 60.0, ReplayGain(), text) for i in ids
        ]


def test_the_latest_play_wins() -> None:
    """A look-up that finishes after a later one's doesn't play."""
    compiled = SimpleNamespace(
        MainWindow=lambda: SimpleNamespace(volume=80.0),
        PlaylistRowKind=Enum("PlaylistRowKind", "release track version"),
    )
    engine = FakeEngine()

    async def main() -> None:
        loop = asyncio.get_running_loop()
        inbox = Inbox(loop)
        window = ui.MainWindow(compiled, WindowOptions(), Stopwatch(False), inbox)
        library = FakeLibrary()
        window.engine = cast(Any, engine)
        window.library = cast(Any, library)
        window.play([1])
        window.play([2])
        while not engine.played:
            await asyncio.sleep(0.01)
        library.slow.set()
        # Time for the first look-up to come back, too.
        await asyncio.sleep(0.1)
        inbox.close()

    asyncio.run(main())
    assert engine.played == [[Path("2.flac")]]


class LockedLibrary:
    """The library service's `query`, failing while another program
    holds the lock."""

    def __init__(self) -> None:
        self.locked = False

    async def query(self, query: str) -> array[int]:
        if self.locked:
            raise sqlite3.OperationalError("database is locked")
        return array("q", [3, 2, 1])


def test_a_failed_sort_keeps_the_header() -> None:
    """The header shows the order of the rows, not of a failed sort."""
    compiled = SimpleNamespace(
        MainWindow=lambda: SimpleNamespace(volume=80.0),
        PlaylistRowKind=Enum("PlaylistRowKind", "release track version"),
    )

    async def main() -> None:
        inbox = Inbox(asyncio.get_running_loop())
        window = ui.MainWindow(compiled, WindowOptions(), Stopwatch(False), inbox)
        library = LockedLibrary()
        tracks = TrackList(
            array("q", [1, 2, 3]), lambda ids: asyncio.sleep(0, {}), lambda rows: None
        )
        window.library = cast(Any, library)
        window.collection = cast(Any, SimpleNamespace())
        window.track_list = tracks
        window._track_sort(1)
        while window._tasks:
            await asyncio.sleep(0)
        assert (window.window.track_sort_column, tracks.ascending) == (1, True)
        library.locked = True
        window._track_sort(2)
        assert window.window.track_sort_column == 2
        while window._tasks:
            await asyncio.sleep(0)
        assert (window.window.track_sort_column, tracks.sort_column) == (1, 1)
        assert window.window.track_sort_ascending
        assert list(tracks._ids) == [3, 2, 1]
        inbox.close()

    asyncio.run(main())
