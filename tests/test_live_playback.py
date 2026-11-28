# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The live playlist played by the real engine, sample-exactly: gapless
with the ReplayGain mode it decides, changes while playing, repeat and
"stop after the current track"."""

from collections.abc import Sequence

import pytest

from roquette.library.index import Playable, PlaylistItem, TrackText
from roquette.playback.engine import Engine
from roquette.playback.gain import AppliedGain, ReplayGain
from roquette.playback.source import Source
from roquette.queue.live import LivePlaylist
from roquette.queue.playlist import Repeat
from tests.audio import RATE, Audio, Capture, Recorder, check, db, make_flac, run, until

GAINS = [ReplayGain(gain, 0.25, -2.0, 0.5) for gain in (-4.0, -1.0, -7.0)]


@pytest.fixture(scope="module")
def tracks(tmp_path_factory: pytest.TempPathFactory) -> list[Audio]:
    """Three tracks of a release, 3 s, 0.6 s and 0.9 s: before the first
    one ends, the next one is queued for about two seconds (see
    test_engine.py)."""
    directory = tmp_path_factory.mktemp("live")
    lengths = [RATE * 3 + 321, RATE * 6 // 10 + 17, RATE * 9 // 10 + 5]
    return [
        make_flac(directory / f"{n}.flac", frames, 20 + n)
        for n, frames in enumerate(lengths)
    ]


class Library:
    def __init__(self, tracks: list[Audio]) -> None:
        self.tracks = tracks

    async def playables(self, ids: Sequence[int]) -> list[Playable]:
        text = TrackText("t", "a", "r", "a", 1, 1, 1.0)
        return [Playable(i, 7, self.tracks[i].path, 1.0, GAINS[i], text) for i in ids]

    async def playlist_items(self, ids: Sequence[int]) -> list[PlaylistItem]:
        return [self.item(i) for i in ids]

    def item(self, n: int) -> PlaylistItem:
        return PlaylistItem(n, 7, 1, n + 1, bytes(self.tracks[n].path))


def play_live(
    tracks: list[Audio],
    order: Sequence[int],
    during: object = None,
    *,
    sync: bool = False,
) -> tuple[Sequence[float], LivePlaylist]:
    """Play the tracks `order` through a live playlist; `during(live,
    engine)` starts playing, instead of Play. With `sync` in real time."""
    capture = Capture(sync=sync)
    library = Library(tracks)
    live = LivePlaylist(library)
    live.append([library.item(n) for n in order])

    async def scenario(engine: Engine, recorder: Recorder) -> None:
        finished = recorder.finished
        live.attach(engine)
        on_finished = engine.on_finished

        def both() -> None:
            assert on_finished is not None
            on_finished()
            recorder._finished()

        engine.on_finished = both
        if callable(during):
            await during(live, engine)
        else:
            live.play()
        await finished

    run(scenario, capture.sink)
    return capture.samples, live


def test_gapless_with_album_gain(tracks: list[Audio]) -> None:
    """Tracks of a release in its order: album gain."""
    output, live = play_live(tracks, [0, 1, 2])
    check(output, [(t, db(-2)) for t in tracks])
    assert live.current is None


def test_track_gain_out_of_order(tracks: list[Audio]) -> None:
    output, _ = play_live(tracks, [2, 0])
    check(output, [(tracks[2], db(-7)), (tracks[0], db(-4))])


def test_the_next_entry_removed_while_playing(tracks: list[Audio]) -> None:
    async def during(live: LivePlaylist, engine: Engine) -> None:
        live.play()
        await until(lambda: len(engine._entries) == 2)
        live.remove([1])

    output, _ = play_live(tracks, [0, 1, 2], during, sync=True)
    # Still the release in its order, a track left out: album gain.
    check(output, [(tracks[0], db(-2)), (tracks[2], db(-2))])


def test_stop_after_the_current_track(tracks: list[Audio]) -> None:
    async def during(live: LivePlaylist, engine: Engine) -> None:
        live.play()
        await until(lambda: len(engine._entries) == 2)
        live.stop_after = True

    output, live = play_live(tracks, [0, 1, 2], during, sync=True)
    check(output, [(tracks[0], db(-2))])
    assert live.current is live.playlist[1] and not live.stop_after


def test_repeat_one_entry(tracks: list[Audio]) -> None:
    """The same file after itself, gapless."""

    async def during(live: LivePlaylist, engine: Engine) -> None:
        live.repeat = Repeat.PLAYLIST
        on_started = engine.on_started
        assert on_started is not None
        starts = 0

        def started(source: Source, gain: AppliedGain) -> None:
            nonlocal starts
            starts += 1
            on_started(source, gain)
            # The third one isn't queued yet.
            if starts == 2:
                live.stop_after = True

        engine.on_started = started
        live.play()

    output, _ = play_live(tracks, [0], during, sync=True)
    check(output, [(tracks[0], db(-4))] * 2)
