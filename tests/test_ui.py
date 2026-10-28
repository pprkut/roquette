# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The .slint files compile; showing them needs a display (from M5 on).

Creating the window without one isn't safe either (Slint then renders to
the screen through KMS), so the window's logic is tested with a stand-in
for the Slint component.
"""

import asyncio
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from roquette.library.index import Playable, Track
from roquette.measure import Stopwatch
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


def track(id: int) -> Track:
    return Track(id, 0, 1, 1, f"Track {id}", "Artist", "", "Artist", 60.0)


def test_the_latest_play_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A look-up that finishes after a later one's doesn't play."""
    slow = threading.Event()

    def load_playables(library: Any, ids: Sequence[int]) -> list[Playable]:
        if ids == [1]:
            slow.wait(5)
        return [Playable(i, 0, Path(f"{i}.flac"), 60.0, ReplayGain()) for i in ids]

    monkeypatch.setattr(ui, "load_playables", load_playables)
    compiled = SimpleNamespace(
        MainWindow=lambda: SimpleNamespace(volume=80.0),
        PlaylistRowKind=Enum("PlaylistRowKind", "release track version"),
    )
    engine = FakeEngine()

    async def main() -> None:
        loop = asyncio.get_running_loop()
        inbox = Inbox(loop)
        window = ui.MainWindow(compiled, WindowOptions(), Stopwatch(False), inbox)
        with ThreadPoolExecutor(2) as pool:
            window.engine = cast(Any, engine)
            window.library = cast(Any, object())
            window.worker = pool
            window.play([track(1)])
            window.play([track(2)])
            while not engine.played:
                await asyncio.sleep(0.01)
            slow.set()
            # Time for the first look-up to come back, too.
            await asyncio.sleep(0.2)
        inbox.close()

    asyncio.run(main())
    assert engine.played == [[Path("2.flac")]]
