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

from roquette.library.grouping import Groups
from roquette.library.index import Playable, TrackText
from roquette.measure import Stopwatch
from roquette.modules.tracks import TrackList
from roquette.playback.gain import ReplayGain
from roquette.playback.source import Source, State
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
        self.played: list[Path] = []
        self.sought: list[float] = []
        self.current: Source | None = None
        self.state = State.PLAYING
        self.target = State.PLAYING
        self.at = 0.0
        self.calls: list[str] = []

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def play(self, source: Source, *, paused: bool = False) -> None:
        self.played.append(source.path)
        self.current = source

    def position(self) -> float:
        return self.at

    def seek(self, position: float) -> None:
        self.sought.append(position)


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
    assert engine.played == [Path("2.flac")]


def test_previous_and_next() -> None:
    """Until the live playlist (M3), the window plays its list."""
    compiled = SimpleNamespace(
        MainWindow=lambda: SimpleNamespace(volume=80.0),
        PlaylistRowKind=Enum("PlaylistRowKind", "release track version"),
    )
    engine = FakeEngine()

    async def main() -> None:
        inbox = Inbox(asyncio.get_running_loop())
        window = ui.MainWindow(compiled, WindowOptions(), Stopwatch(False), inbox)
        window.engine = cast(Any, engine)
        a, b, c = (Source(Path(f"{n}.flac")) for n in "abc")
        window._sources.sources = (a, b, c)
        engine.current = b
        window._next()
        assert engine.played == [Path("c.flac")]
        window._next()
        assert engine.played == [Path("c.flac")]
        # Back to the start of the track after a few seconds, else the
        # one before.
        engine.at = 5.0
        window._previous()
        assert engine.sought == [0.0]
        engine.at = 1.0
        window._previous()
        assert engine.played[-1] == Path("b.flac")
        engine.current = a
        window._previous()
        assert engine.sought == [0.0, 0.0]
        inbox.close()

    asyncio.run(main())


def test_play_pause_while_a_track_starts() -> None:
    """The state asked for counts, not the one the pipeline reached: a
    start on its way is paused, not replaced by the list's first track."""
    compiled = SimpleNamespace(
        MainWindow=lambda: SimpleNamespace(volume=80.0),
        PlaylistRowKind=Enum("PlaylistRowKind", "release track version"),
    )
    engine = FakeEngine()

    async def main() -> None:
        inbox = Inbox(asyncio.get_running_loop())
        window = ui.MainWindow(compiled, WindowOptions(), Stopwatch(False), inbox)
        window.engine = cast(Any, engine)
        a, b = (Source(Path(f"{n}.flac")) for n in "ab")
        window._sources.sources = (a, b)
        engine.state = State.STOPPED
        window._play_pause()
        engine.target = State.PAUSED
        window._play_pause()
        assert (engine.calls, engine.played) == (["pause", "resume"], [])
        engine.target = State.STOPPED
        window._play_pause()
        assert engine.played == [Path("a.flac")]
        inbox.close()

    asyncio.run(main())


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


def test_release_of_track() -> None:
    compiled = SimpleNamespace(
        MainWindow=lambda: SimpleNamespace(volume=80.0),
        PlaylistRowKind=Enum("PlaylistRowKind", "release track version"),
    )

    async def main() -> None:
        inbox = Inbox(asyncio.get_running_loop())
        window = ui.MainWindow(compiled, WindowOptions(), Stopwatch(False), inbox)
        # A release of tracks 5 and 6, and a singleton, 7.
        groups = Groups(array("I", [0, 2, 3]), array("q", [9, 0]), array("I", [1, 1]))
        tracks = SimpleNamespace(ids=array("q", [5, 6, 7]))
        window.collection = cast(
            Any, SimpleNamespace(index=SimpleNamespace(tracks=tracks), groups=groups)
        )
        assert window._release_of_track(6, None) == [5, 6]
        assert window._release_of_track(6, 1) == [5, 6]
        assert window._release_of_track(7, None) == [7]
        assert window._release_of_track(8, None) == [8]
        inbox.close()

    asyncio.run(main())
