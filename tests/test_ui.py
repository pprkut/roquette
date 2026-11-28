# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The .slint files compile; showing them needs a display (from M5 on).

Creating the window without one isn't safe either (Slint then renders to
the screen through KMS), so the window's logic is tested with a stand-in
for the Slint component.
"""

import asyncio
import logging
import sqlite3
from array import array
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from roquette.library.grouping import Groups
from roquette.library.index import Playable, PlaylistItem, TrackText
from roquette.measure import Stopwatch
from roquette.modules.tracks import TrackList
from roquette.playback.gain import ReplayGain
from roquette.queue import m3u
from roquette.queue.live import LivePlaylist
from roquette.queue.m3u import Saved
from roquette.threads import Inbox
from roquette.ui import window as ui
from roquette.ui.window import WindowOptions, load_ui, volume_factor
from tests.test_live import FakePlayer


@pytest.mark.parametrize("name", ["main_window", "empty_window"])
def test_compiles(name: str, caplog: pytest.LogCaptureFixture) -> None:
    """Without warnings either, which Slint logs."""
    caplog.set_level(logging.WARNING)
    load_ui(name)
    assert [r.getMessage() for r in caplog.records] == []


def test_volume_factor() -> None:
    assert volume_factor(0) == 0.0
    assert volume_factor(100) == 1.0
    assert volume_factor(50) == 0.125
    assert volume_factor(150) == 1.0


class FakeLibrary:
    """The library service, as the window's live playlist uses it; the
    look-up of track 1 is slow."""

    def __init__(self) -> None:
        self.slow = asyncio.Event()

    async def playlist_items(self, ids: Sequence[int]) -> list[PlaylistItem]:
        if list(ids) == [1]:
            await self.slow.wait()
        return [PlaylistItem(i, 3, 1, i, f"/m/{i}.flac".encode()) for i in ids]

    async def playlist_items_by_path(
        self, paths: Sequence[bytes]
    ) -> dict[bytes, PlaylistItem]:
        found = {}
        for path in paths:
            if path.startswith(b"/m/"):
                n = int(path[3:].split(b".")[0])
                found[path] = PlaylistItem(n, 3, 1, n, path)
        return found

    async def playables(self, ids: Sequence[int]) -> list[Playable]:
        return [
            Playable(i, 3, Path(f"/m/{i}.flac"), 60.0, ReplayGain(), text(i))
            for i in ids
        ]

    async def track_text(self, ids: Sequence[int]) -> dict[int, TrackText]:
        return {i: text(i) for i in ids}


def text(n: int) -> TrackText:
    return TrackText(f"Track {n}", "Artist", "Album", "Artist", 1, n, 60.0)


COMPILED = SimpleNamespace(
    MainWindow=lambda: SimpleNamespace(volume=80.0),
    PlaylistRowKind=Enum("PlaylistRowKind", "release track version"),
)


def window_with_playlist(
    options: WindowOptions | None = None,
) -> tuple[ui.MainWindow, FakeLibrary, FakePlayer, Inbox]:
    """A window with a live playlist on the fake library, played by a
    stand-in for the engine; on the running event loop."""
    inbox = Inbox(asyncio.get_running_loop())
    window = ui.MainWindow(
        COMPILED, options or WindowOptions(), Stopwatch(False), inbox
    )
    library = FakeLibrary()
    player = FakePlayer()
    window.library = cast(Any, library)
    window.live = LivePlaylist(library)
    window.live.on_started = window._started
    window.live.on_changed = window._playlist_changed
    window.live.attach(player)
    return window, library, player, inbox


async def settle(window: ui.MainWindow) -> None:
    """Wait until the window and its live playlist have done their work."""
    live = window.live
    assert live is not None
    async with asyncio.timeout(10):
        while window._tasks or live._tasks or live._loading is not None:
            await asyncio.sleep(0.001)


def test_the_latest_play_wins() -> None:
    """A look-up that finishes after a later one's doesn't replace it."""

    async def main() -> None:
        window, library, player, inbox = window_with_playlist()
        window.play([1])
        window.play([2])
        async with asyncio.timeout(10):
            while not player.played:
                await asyncio.sleep(0.01)
        library.slow.set()
        await settle(window)
        assert window.live is not None
        assert [e.item.id for e in window.live.playlist] == [2]
        assert [s.path for s, _, _ in player.played] == [Path("/m/2.flac")]
        inbox.close()

    asyncio.run(main())


def test_the_pane_shows_the_live_playlist() -> None:
    async def main() -> None:
        window, library, player, inbox = window_with_playlist()
        library.slow.set()
        window._add([2, 3])
        window._add([4], how="next")
        await settle(window)
        assert [window.pane.row(i).title for i in range(len(window.pane))] == [
            "Album",
            "Track 4",
            "Track 2",
            "Track 3",
        ]
        assert window.playlist_model.row_count() == 4
        # Playing: the marker moves, and so does the now-playing display.
        window._add([5], play=0)
        await settle(window)
        assert window.pane.row(4).playing
        player.start(player.played[-1][0])
        await settle(window)
        assert window.window.now_title == "Track 5"
        window._next()
        inbox.close()

    asyncio.run(main())


def test_the_live_playlist_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "state" / "playlist.m3u8"
    m3u.write(path, [Saved(b"/m/7.flac", 7, current=True), Saved(b"/m/8.flac", 8)])

    async def main() -> None:
        options = WindowOptions(playlist_file=path)
        window, library, _, inbox = window_with_playlist(options)
        library.slow.set()
        await window.load_playlist()
        live = window.live
        assert live is not None
        assert [e.item.id for e in live.playlist] == [7, 8]
        assert live.current is live.playlist[0]
        assert window.playlist_file is not None
        window.playlist_file.delay = 0.0
        window._add([9], pinned=True)
        await settle(window)
        window.close()
        inbox.close()

    asyncio.run(main())
    assert m3u.read(path) == [
        Saved(b"/m/7.flac", 7, current=True),
        Saved(b"/m/8.flac", 8),
        Saved(b"/m/9.flac", 9, pinned=True),
    ]


def test_a_playlist_that_cant_be_read_isnt_saved_over(tmp_path: Path) -> None:
    path = tmp_path / "playlist.m3u8"
    path.mkdir()

    async def main() -> None:
        window, library, _, inbox = window_with_playlist(
            WindowOptions(playlist_file=path)
        )
        library.slow.set()
        await window.load_playlist()
        window._add([9])
        await settle(window)
        window.close()
        inbox.close()

    asyncio.run(main())
    assert path.is_dir()


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
