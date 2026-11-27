# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The live playlist with a stand-in for the engine: the plan of what
plays next, the current entry, Stop and "stop after the current track"."""

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

import pytest

from roquette.library.index import Playable, PlaylistItem, TrackText
from roquette.playback.gain import AppliedGain, Mode, Origin, ReplayGain
from roquette.playback.source import Source, State, Upcoming
from roquette.queue.live import RESTART_AFTER, LivePlaylist, QueuedSource
from roquette.queue.playlist import Repeat, Shuffle

GAIN = AppliedGain(1.0, Mode.TRACK, Mode.TRACK, Origin.BEETS, 0.0, 0.0, None, False)


def item(id: int, album: int = 0, track: int = 0) -> PlaylistItem:
    return PlaylistItem(id, album, 1, track, f"/m/{id}.flac".encode())


class FakeLibrary:
    def __init__(self) -> None:
        # Not in the library.
        self.gone: set[int] = set()
        self.asked: list[list[int]] = []
        self.fail = False

    async def playables(self, ids: Sequence[int]) -> list[Playable]:
        self.asked.append(list(ids))
        await asyncio.sleep(0)
        if self.fail:
            raise OSError("the database is gone")
        return [
            Playable(
                i,
                0,
                Path(f"/m/{i}.flac"),
                60.0,
                ReplayGain(-float(i), 0.5, -1.0, 0.5),
                TrackText(f"t{i}", "a", "r", "a", 1, i, 60.0),
            )
            for i in ids
            if i not in self.gone
        ]

    async def playlist_items(self, ids: Sequence[int]) -> list[PlaylistItem]:
        return [item(i) for i in ids if i not in self.gone]


class FakePlayer:
    def __init__(self) -> None:
        self.upcoming: Upcoming | None = None
        self.on_started: Callable[[Source, AppliedGain], None] | None = None
        self.on_finished: Callable[[], None] | None = None
        self.target = State.STOPPED
        self.played: list[tuple[Source, bool, float]] = []
        self.calls: list[str] = []
        self.at = 0.0
        self.changes = 0

    def play(
        self, source: Source, *, paused: bool = False, position: float = 0.0
    ) -> None:
        self.played.append((source, paused, position))
        self.target = State.PAUSED if paused else State.PLAYING

    def pause(self) -> None:
        self.calls.append("pause")
        self.target = State.PAUSED

    def resume(self) -> None:
        self.calls.append("resume")
        self.target = State.PLAYING

    def stop(self) -> None:
        self.calls.append("stop")
        self.target = State.STOPPED

    def seek(self, seconds: float) -> None:
        self.calls.append(f"seek {seconds}")

    def position(self) -> float | None:
        return self.at

    def next_changed(self) -> None:
        self.changes += 1

    # What the engine does

    def start(self, source: Source) -> None:
        """`source` became audible."""
        assert self.on_started is not None
        self.on_started(source, GAIN)

    def finish(self) -> None:
        self.target = State.STOPPED
        assert self.on_finished is not None
        self.on_finished()


Scenario = Callable[[LivePlaylist, FakePlayer, FakeLibrary], Coroutine[Any, Any, None]]


def run(scenario: Scenario, **options: Any) -> None:
    async def main() -> None:
        library = FakeLibrary()
        live = LivePlaylist(library, **options)
        player = FakePlayer()
        live.attach(player)
        try:
            await scenario(live, player, library)
        finally:
            live.close()

    asyncio.run(main())


def chain(live: LivePlaylist, source: Source | None, count: int = 5) -> list[int]:
    """The item ids of the plan from `source` on."""
    ids = []
    for _ in range(count):
        source = live.next_source(source) if source is not None else None
        if not isinstance(source, QueuedSource) or source.entry is None:
            break
        ids.append(source.entry.item.id)
    return ids


def playing(player: FakePlayer) -> QueuedSource:
    source = player.played[-1][0]
    assert isinstance(source, QueuedSource)
    return source


async def started(live: LivePlaylist, player: FakePlayer, **play: Any) -> QueuedSource:
    live.play(**play)
    await live.settle()
    return playing(player)


def test_the_plan() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(n) for n in range(1, 6)])
        first = await started(live, player)
        assert first.entry is live.current and first.path == Path("/m/1.flac")
        assert first.replaygain == ReplayGain(-1.0, 0.5, -1.0, 0.5)
        await live.settle()
        # Two entries ahead.
        assert chain(live, first) == [2, 3]
        second = live.next_source(first)
        assert second is not None
        changes = player.changes
        # A track change: the plan goes on, the next source stays.
        player.start(second)
        await live.settle()
        assert live.current is live.playlist[1]
        assert live.next_source(first) is None
        assert chain(live, second) == [3, 4]
        third = live.next_source(second)
        assert third is not None
        player.start(third)
        await live.settle()
        assert live.next_source(second) is None and chain(live, third) == [4, 5]
        assert player.changes > changes

    run(scenario)


def test_changes_after_the_current_entry() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(n) for n in range(1, 5)])
        first = await started(live, player)
        await live.settle()
        second = live.next_source(first)
        changes = player.changes
        # Further on: what's next stays, and the engine is told.
        live.move([3], 2)
        await live.settle()
        assert chain(live, first) == [2, 4]
        assert live.next_source(first) is second
        assert player.changes > changes
        live.remove([1])
        await live.settle()
        assert chain(live, first) == [4, 3]
        live.play_next([item(9)])
        await live.settle()
        assert chain(live, first) == [9, 4]
        # Nothing that changes the plan, nothing to tell.
        changes = player.changes
        live.append([item(10)])
        assert player.changes == changes

    run(scenario)


def test_the_end() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1), item(2)])
        first = await started(live, player)
        await live.settle()
        second = live.next_source(first)
        assert second is not None and live.next_source(second) is None
        player.start(second)
        player.finish()
        assert live.current is None and live.playing is None
        # Play starts from the first entry again.
        again = await started(live, player)
        assert again.entry is live.playlist[0]

    run(scenario)


def test_repeat() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1), item(2)])
        first = await started(live, player)
        live.repeat = Repeat.TRACK
        await live.settle()
        again = live.next_source(first)
        # Another source of the same entry, and back.
        assert isinstance(again, QueuedSource) and again is not first
        assert again.entry is first.entry and live.next_source(again) is first
        player.start(again)
        await live.settle()
        assert live.next_source(again) is first
        live.repeat = Repeat.PLAYLIST
        await live.settle()
        assert chain(live, again) == [2, 1]
        # Next goes on whatever repeats.
        live.repeat = Repeat.TRACK
        live.next()
        await live.settle()
        assert playing(player).entry is live.playlist[1]

    run(scenario)


def test_one_entry_repeated() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1)])
        live.repeat = Repeat.PLAYLIST
        first = await started(live, player)
        await live.settle()
        second = live.next_source(first)
        assert second is not None and second is not first
        assert live.next_source(second) is first

    run(scenario)


def test_stop_keeps_the_current_entry() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1), item(2)])
        live.play(live.playlist[1])
        await live.settle()
        live.stop()
        assert player.calls == ["stop"]
        assert live.current is live.playlist[1] and live.playing is None
        assert live.next_source(player.played[-1][0]) is None
        player.played.clear()
        live.play_pause()
        await live.settle()
        source, paused, position = player.played[0]
        assert isinstance(source, QueuedSource)
        assert (source.entry, paused, position) == (live.playlist[1], False, 0.0)
        live.play_pause()
        live.play_pause()
        assert player.calls == ["stop", "pause", "resume"]

    run(scenario)


def test_stop_after_the_current_track() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1), item(2), item(3)])
        first = await started(live, player)
        await live.settle()
        changes = player.changes
        live.stop_after = True
        assert live.next_source(first) is None and player.changes == changes + 1
        player.finish()
        # Stopped; the track that would have played is current.
        assert not live.stop_after
        assert live.current is live.playlist[1] and live.playing is None
        assert player.played[-1][0] is first
        # The last track: nothing after it.
        live.play(live.playlist[2])
        await live.settle()
        live.stop_after = True
        player.finish()
        assert live.current is None

    run(scenario)


def test_stop_after_with_repeat_track() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1), item(2)])
        await started(live, player)
        live.repeat = Repeat.TRACK
        live.stop_after = True
        player.finish()
        assert live.current is live.playlist[0]

    run(scenario)


def test_next_and_previous() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1), item(2), item(3)])
        # Stopped: they move the current entry.
        live.next()
        assert live.current is live.playlist[0] and not player.played
        live.next()
        assert live.current is live.playlist[1]
        live.previous()
        assert live.current is live.playlist[0] and not player.played
        # Playing (or paused): they play.
        await started(live, player)
        live.next()
        await live.settle()
        assert playing(player).entry is live.playlist[1]
        player.pause()
        live.next()
        await live.settle()
        assert playing(player).entry is live.playlist[2] and player.played[-1][1]
        live.next()
        assert playing(player).entry is live.playlist[2]
        # Back to the start after a few seconds, else the one before.
        player.at = RESTART_AFTER + 1
        live.previous()
        assert player.calls[-1] == "seek 0.0"
        player.at = 1.0
        live.previous()
        await live.settle()
        assert playing(player).entry is live.playlist[1]
        live.play(live.playlist[0])
        await live.settle()
        live.previous()
        assert player.calls[-1] == "seek 0.0"

    run(scenario)


def test_removing_the_playing_entry() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1), item(2), item(3)])
        live.play(live.playlist[1])
        await live.settle()
        source = playing(player)
        live.remove([1])
        await live.settle()
        # It plays on, what followed it next.
        assert live.current is source.entry and chain(live, source) == [3]
        live.stop()
        assert live.current is live.playlist[1]
        assert live.current.item.id == 3

    run(scenario)


def test_removing_the_current_entry_while_stopped() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1), item(2), item(3)])
        live.playlist.current = live.playlist[1]
        live.remove([1])
        current = live.current
        assert current is not None and current.item.id == 3

    run(scenario)


def test_replaygain_modes() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1, 7, 1), item(2, 7, 2), item(3, 0, 1)])
        first = await started(live, player)
        await live.settle()
        assert first.mode is Mode.ALBUM
        second = live.next_source(first)
        assert second is not None and second.mode is Mode.ALBUM
        third = live.next_source(second)
        assert third is not None and third.mode is Mode.TRACK
        # A track in between: the next one plays with track gain, a new
        # source for it.
        live.insert(1, [item(4, 8, 1)])
        await live.settle()
        inserted = live.next_source(first)
        assert inserted is not None and inserted is not second
        assert inserted.mode is Mode.TRACK

    run(scenario)


def test_fixed_mode_and_tags() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1, 7, 1), item(2, 7, 2)])
        first = await started(live, player)
        assert (first.mode, first.replaygain) == (Mode.OFF, None)

    run(scenario, replaygain="off", gain_source="tags")


def test_shuffle() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(n) for n in range(1, 30)])
        first = await started(live, player)
        live.shuffle = Shuffle.TRACKS
        await live.settle()
        order = [e.item.id for e in live.playlist.order]
        assert order[0] == 1 and chain(live, first) == order[1:3]

    run(scenario)


def test_tracks_not_in_the_library() -> None:
    async def scenario(
        live: LivePlaylist, player: FakePlayer, library: FakeLibrary
    ) -> None:
        library.gone = {2, 4}
        live.append([item(n) for n in range(1, 6)])
        first = await started(live, player)
        await live.settle()
        assert chain(live, first) == [3, 5]
        # Playing one that's gone plays the next one there is.
        live.play(live.playlist[3])
        await live.settle()
        assert playing(player).entry is live.playlist[4]

    run(scenario)


def test_the_library_changed() -> None:
    async def scenario(
        live: LivePlaylist, player: FakePlayer, library: FakeLibrary
    ) -> None:
        live.append([item(n) for n in range(1, 5)])
        first = await started(live, player)
        await live.settle()
        library.gone = {2}
        await live.reload()
        await live.settle()
        assert chain(live, first) == [3, 4]
        library.gone = set()
        await live.reload()
        await live.settle()
        assert chain(live, first) == [2, 3]

    run(scenario)


def test_a_failed_look_up() -> None:
    """Isn't tried again and again; the next change asks again."""

    async def scenario(
        live: LivePlaylist, player: FakePlayer, library: FakeLibrary
    ) -> None:
        live.append([item(1)])
        first = await started(live, player)
        library.fail = True
        live.append([item(2)])
        await live.settle()
        assert live.next_source(first) is None
        asked = len(library.asked)
        await asyncio.sleep(0.01)
        assert len(library.asked) == asked
        library.fail = False
        live.append([item(3)])
        await live.settle()
        assert chain(live, first) == [2, 3]

    run(scenario)


def test_the_latest_play_wins() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1), item(2)])
        live.play(live.playlist[0])
        live.play(live.playlist[1])
        await live.settle()
        assert len(player.played) == 1
        assert playing(player).entry is live.playlist[1]

    run(scenario)


def test_restore() -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        from roquette.queue.playlist import Entry

        entries = [Entry(item(1)), Entry(item(2), pinned=True)]
        live.restore(entries, 1)
        assert live.current is entries[1] and live.playing is None
        live.restore(entries, None)
        assert live.current is None

    run(scenario)


@pytest.mark.parametrize("pinned", [False, True])
def test_pinned(pinned: bool) -> None:
    async def scenario(live: LivePlaylist, player: FakePlayer, _: object) -> None:
        live.append([item(1)], pinned=pinned)
        live.play_next([item(2)], pinned=pinned)
        assert [e.pinned for e in live.playlist] == [pinned, pinned]

    run(scenario)
