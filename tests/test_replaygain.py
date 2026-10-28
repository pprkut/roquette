# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

from pathlib import Path

from roquette.library.index import Playable
from roquette.playback.gain import Mode, ReplayGain
from roquette.queue.replaygain import auto_modes, make_sources


def test_auto_modes() -> None:
    T, A = Mode.TRACK, Mode.ALBUM
    assert auto_modes([]) == []
    assert auto_modes([5]) == [T]
    assert auto_modes([5, 5, 5]) == [A, A, A]
    # A track between two releases, singletons (album id 0) next to each
    # other.
    assert auto_modes([5, 5, 7, 3, 3, 0, 0]) == [A, A, T, A, A, T, T]


def playable(n: int, album_id: int) -> Playable:
    return Playable(n, album_id, Path(f"/m/{n}.flac"), 60.0, ReplayGain(-n, 0.9))


def test_make_sources() -> None:
    playables = [playable(1, 5), playable(2, 5), playable(3, 0)]
    sources = make_sources(playables)
    assert [s.mode for s in sources] == [Mode.ALBUM, Mode.ALBUM, Mode.TRACK]
    assert sources[0].replaygain == ReplayGain(-1, 0.9)
    assert sources[2].path == Path("/m/3.flac")
    assert sources[2].length == 60.0

    tags = make_sources(playables, "track", "tags")
    assert [s.mode for s in tags] == [Mode.TRACK] * 3
    assert all(s.replaygain is None for s in tags)
    assert [s.mode for s in make_sources(playables, "off")] == [Mode.OFF] * 3
