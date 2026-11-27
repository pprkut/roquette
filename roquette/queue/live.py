# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The live playlist: the model, played by the engine.

The live playlist is the engine's provider (`Upcoming`). The engine
asks it for the source after another one on GStreamer's streaming
threads, without the event loop, so what plays next is resolved ahead:
the sources of the entries after the current one, `LOOKAHEAD` of them,
with their files and ReplayGain read from beets beforehand. They're
published as a dictionary (`_plan`, from each source to the next one),
replaced whole, never changed, on the event loop; the streaming
threads only read it. After every change that can change what follows,
the engine is told with `next_changed()`, and follows it (see
`roquette.playback.engine`).

The engine compares sources by identity, so the plan keeps the sources
it published while they're still what follows: a change further on
doesn't make the engine drop the next one. An entry that comes twice
in the plan (repeat, a playlist of one) gets a second source.

The live playlist owns the current entry, not the engine, whose
`stop()` forgets its source: Stop keeps the current entry, and Play
plays it from its start. "Stop after the current track" ends the plan
at the current entry, so the engine finishes there; once it has, the
entry that would have played is current, and the flag is cleared.
"""

import asyncio
import logging
from collections.abc import Callable, Collection, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from roquette.library.index import Playable, PlaylistItem
from roquette.playback.gain import AppliedGain, Mode
from roquette.playback.source import Source, State, Upcoming
from roquette.queue.playlist import Entry, Playlist, Repeat, Shuffle

log = logging.getLogger(__name__)

# Entries after the current one whose sources are ready. The engine asks
# for the one after the current source once its file has been read, and
# for the one after that when the next file is short.
LOOKAHEAD = 2

# "Previous" goes back to the start of the track after this many seconds.
RESTART_AFTER = 3.0


@dataclass(frozen=True, slots=True, eq=False)
class QueuedSource(Source):
    """A source for an entry of the live playlist."""

    entry: Entry | None = None
    playable: Playable | None = None


class Player(Protocol):
    """The engine, as the live playlist uses it."""

    upcoming: Upcoming | None
    on_started: Callable[[Source, AppliedGain], None] | None
    on_finished: Callable[[], None] | None

    @property
    def target(self) -> State: ...

    def play(
        self, source: Source, *, paused: bool = False, position: float = 0.0
    ) -> None: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def stop(self) -> None: ...

    def seek(self, seconds: float) -> None: ...

    def position(self) -> float | None: ...

    def next_changed(self) -> None: ...


class Library(Protocol):
    """The library service, as the live playlist uses it."""

    async def playables(self, ids: Sequence[int]) -> list[Playable]: ...

    async def playlist_items(self, ids: Sequence[int]) -> list[PlaylistItem]: ...


class LivePlaylist:
    """Lives on the event loop's thread; only `next_source` is called
    from others."""

    def __init__(
        self,
        library: Library,
        playlist: Playlist | None = None,
        *,
        replaygain: str = "auto",
        gain_source: str = "beets",
    ) -> None:
        self.library = library
        self.playlist = playlist if playlist is not None else Playlist()
        # "auto" or a `Mode`'s value; "beets" or "tags".
        self.replaygain = replaygain
        self.gain_source = gain_source
        self.player: Player | None = None
        # A source of the playlist became audible.
        self.on_started: Callable[[QueuedSource, AppliedGain], None] | None = None
        # The entries, their order, the current entry or the modes changed.
        self.on_changed: Callable[[], None] | None = None

        # What follows each source; read by the streaming threads.
        self._plan: dict[Source, Source | None] = {}
        # The current entry's source, given to the engine; None while
        # nothing plays.
        self._source: QueuedSource | None = None
        # The files of the entries in the plan, and the items not in the
        # library (any more).
        self._playables: dict[int, Playable] = {}
        self._missing: set[int] = set()
        # The sources made for entries, reused while they're the same.
        self._variants: dict[Entry, list[QueuedSource]] = {}
        self._loading: asyncio.Task[None] | None = None
        # Counts the calls of `play`: only the latest one's look-up plays.
        self._requests = 0
        self._tasks: set[asyncio.Task[None]] = set()

    def attach(self, player: Player) -> None:
        self.player = player
        player.upcoming = self
        player.on_started = self._started
        player.on_finished = self._finished

    def close(self) -> None:
        for task in [*self._tasks, self._loading]:
            if task is not None:
                task.cancel()

    async def settle(self) -> None:
        """Wait for the look-ups on their way (for the tests)."""
        while self._tasks or self._loading is not None:
            await asyncio.sleep(0.001)

    # The engine's provider, on any thread

    def next_source(self, after: Source) -> Source | None:
        return self._plan.get(after)

    # State

    @property
    def current(self) -> Entry | None:
        return self.playlist.current

    @property
    def playing(self) -> QueuedSource | None:
        """The source given to the engine for the current entry."""
        return self._source

    @property
    def repeat(self) -> Repeat:
        return self.playlist.repeat

    @repeat.setter
    def repeat(self, mode: Repeat) -> None:
        self.playlist.repeat = mode
        self._changed()

    @property
    def shuffle(self) -> Shuffle:
        return self.playlist.shuffle

    @shuffle.setter
    def shuffle(self, mode: Shuffle) -> None:
        self.playlist.shuffle = mode
        self._changed()

    @property
    def stop_after(self) -> bool:
        return self.playlist.stop_after

    @stop_after.setter
    def stop_after(self, stop: bool) -> None:
        self.playlist.stop_after = stop
        self._changed()

    # Changes

    def append(self, items: Iterable[PlaylistItem], *, pinned: bool = False) -> int:
        """Add tracks at the end (`pinned` for a whole release or medium);
        returns the position of the first."""
        entries = self._entries(items, pinned)
        start = len(self.playlist)
        self.playlist.append(entries)
        self._changed()
        return start

    def insert(
        self, index: int, items: Iterable[PlaylistItem], *, pinned: bool = False
    ) -> None:
        self.playlist.insert(index, self._entries(items, pinned))
        self._changed()

    def play_next(self, items: Iterable[PlaylistItem], *, pinned: bool = False) -> int:
        position = self.playlist.play_next(self._entries(items, pinned))
        self._changed()
        return position

    def move(self, indices: Iterable[int], to: int) -> int:
        position = self.playlist.move(indices, to)
        self._changed()
        return position

    def remove(self, indices: Iterable[int]) -> None:
        self.playlist.remove(indices)
        self._changed()

    def clear(self) -> None:
        self.remove(range(len(self.playlist)))

    def replace(self, items: Iterable[PlaylistItem], *, pinned: bool = False) -> None:
        self.playlist.replace(self._entries(items, pinned))
        self._changed()

    def restore(self, entries: Sequence[Entry], current: int | None) -> None:
        """The playlist as it was saved, with nothing playing."""
        self.playlist.replace(entries)
        self.playlist.current = (
            entries[current] if current is not None and current < len(entries) else None
        )
        self._changed()

    @staticmethod
    def _entries(items: Iterable[PlaylistItem], pinned: bool) -> list[Entry]:
        return [Entry(item, pinned) for item in items]

    async def reload(self) -> None:
        """Read the entries' items again, after the library changed: the
        files may have moved, tracks may be gone (they're skipped) or
        back."""
        entries = list(self.playlist)
        found = await self.library.playlist_items([e.item.id for e in entries])
        items = {item.id: item for item in found}
        for entry in entries:
            item = items.get(entry.item.id)
            if item is not None:
                entry.item = item
        self._missing = {e.item.id for e in entries if e.item.id not in items}
        # The plan's files again, except the playing one's.
        playing = self._source.playable if self._source is not None else None
        self._playables = {playing.id: playing} if playing is not None else {}
        self._changed()

    # Playing

    def play(
        self,
        entry: Entry | None = None,
        *,
        paused: bool = False,
        position: float = 0.0,
    ) -> None:
        """Play `entry` (the current one, or the first), once its file is
        looked up; a later call wins."""
        if entry is None:
            entry = self.current
            if entry is not None and self.playlist.detached:
                entry = self.playlist.following(entry, manual=True)
            if entry is None:
                entry = self.playlist.first()
        if entry is None or self.player is None:
            return
        self._requests += 1
        request = self._requests
        item = self.playlist.version(entry)
        if item.id in self._playables:
            self._start(entry, paused, position)
            return

        async def look_up() -> None:
            await self._fetch([item.id])
            if request == self._requests:
                self._start(entry, paused, position)

        self._spawn(look_up())

    def _start(self, entry: Entry, paused: bool, position: float) -> None:
        if self.player is None:
            return
        if entry not in self.playlist and entry is not self.current:
            # Removed while its file was looked up.
            return
        if self.playlist.version(entry).id in self._missing:
            # Not in the library any more: the next one that is.
            following = self._following(entry)
            if following is None or following is entry:
                return
            entry = following
            if self.playlist.version(entry).id not in self._playables:
                self.play(entry, paused=paused)
                return
        if entry is not self.current:
            self.playlist.current = entry
        source = self._source_for(entry, None, set())
        assert source is not None
        self._source = source
        # The plan first: the engine may ask for the next source soon.
        self._update()
        self.player.play(source, paused=paused, position=position)
        self._notify()

    def play_pause(self) -> None:
        player = self.player
        if player is None:
            return
        # The state asked for: a start on its way is paused or resumed too.
        if player.target is State.PLAYING:
            player.pause()
        elif player.target is State.PAUSED:
            player.resume()
        else:
            self.play()

    def stop(self) -> None:
        """Stop; the current entry stays, Play plays it from its start."""
        self._requests += 1
        if self.player is not None:
            self.player.stop()
        self._source = None
        self.playlist.stop_after = False
        self._changed()

    def next(self) -> None:
        current = self.current
        entry = self._following(current, manual=True)
        if entry is None or entry is current:
            return
        self._go(entry)

    def previous(self) -> None:
        """The entry before, or the start of the track after a few
        seconds of it (or at the start of the playlist)."""
        current = self.current
        if current is None:
            return
        player = self.player
        active = self._source is not None and player is not None
        if active and player is not None and (player.position() or 0.0) > RESTART_AFTER:
            player.seek(0.0)
            return
        entry = self.playlist.preceding(current)
        if entry is None:
            if active and player is not None:
                player.seek(0.0)
            return
        self._go(entry)

    def _go(self, entry: Entry) -> None:
        """Make `entry` current: playing it if something plays."""
        player = self.player
        if self._source is None or player is None or player.target is State.STOPPED:
            self.playlist.current = entry
            self._changed()
            return
        self.play(entry, paused=player.target is State.PAUSED)

    # The engine's callbacks

    def _started(self, source: Source, gain: AppliedGain) -> None:
        if not isinstance(source, QueuedSource) or source.entry is None:
            return
        entry = source.entry
        if entry in self.playlist:
            self.playlist.current = entry
        elif entry is not self.current:
            # Removed from the playlist while it was on its way; plays on.
            log.info("an entry that was removed started playing")
        self._source = source
        self._update()
        if self.on_started is not None:
            self.on_started(source, gain)
        self._notify()

    def _finished(self) -> None:
        """The engine stopped at the end of what it had."""
        current = self.current
        self._source = None
        if self.playlist.stop_after:
            self.playlist.stop_after = False
            following = self._following(current) if current is not None else None
            self.playlist.current = following if following in self.playlist else None
        else:
            self.playlist.current = None
        self._changed()

    # The plan

    def _changed(self) -> None:
        if self._source is None and self.playlist.detached:
            # Nothing plays the entry that was removed: what followed it
            # is current.
            current = self.current
            assert current is not None
            self.playlist.current = self.playlist.following(current, manual=True)
        self._update()
        self._notify()

    def _notify(self) -> None:
        if self.on_changed is not None:
            self.on_changed()

    def _following(self, entry: Entry | None, *, manual: bool = False) -> Entry | None:
        """What plays after `entry`, past tracks not in the library."""
        candidate = self.playlist.following(entry, manual=manual)
        for _ in range(len(self.playlist)):
            if candidate is None:
                return None
            if self.playlist.version(candidate).id not in self._missing:
                return candidate
            candidate = self.playlist.following(candidate, manual=manual)
        return None

    def _mode(self, entry: Entry) -> Mode:
        if self.replaygain != "auto":
            return Mode(self.replaygain)
        if entry not in self.playlist:
            return Mode.TRACK
        return Mode.ALBUM if self.playlist.album_gain(entry) else Mode.TRACK

    def _source_for(
        self, entry: Entry, previous: Source | None, used: Collection[Source]
    ) -> QueuedSource | None:
        """The entry's source after `previous`: the one published there if
        it's still right, else one that isn't `previous` or `used`; None
        while its file isn't known."""
        playable = self._playables.get(self.playlist.version(entry).id)
        if playable is None:
            return None
        mode = self._mode(entry)

        def fits(source: Source | None) -> bool:
            return (
                isinstance(source, QueuedSource)
                and source.entry is entry
                and source.playable is playable
                and source.mode is mode
            )

        published = self._plan.get(previous) if previous is not None else None
        if (
            isinstance(published, QueuedSource)
            and fits(published)
            and published not in used
        ):
            return published
        variants = [v for v in self._variants.get(entry, ()) if fits(v)]
        self._variants[entry] = variants
        for variant in variants:
            if variant is not previous and variant not in used:
                return variant
        for variant in variants:
            if variant is not previous:
                return variant
        replaygain = playable.replaygain if self.gain_source == "beets" else None
        source = QueuedSource(
            playable.path, replaygain, mode, playable.length, entry, playable
        )
        variants.append(source)
        return source

    def _update(self) -> None:
        """Publish what follows the playing source, and tell the engine."""
        plan: dict[Source, Source | None] = {}
        wanted: list[int] = []
        start = self._source
        kept: set[Entry] = set()
        if start is not None and start.entry is not None:
            kept.add(start.entry)
            entries: list[Entry] = []
            entry: Entry | None = start.entry
            if not self.playlist.stop_after:
                for _ in range(LOOKAHEAD):
                    entry = self._following(entry)
                    if entry is None:
                        break
                    entries.append(entry)
            previous: Source = start
            used: set[Source] = {start}
            complete = True
            for entry in entries:
                kept.add(entry)
                item = self.playlist.version(entry).id
                if item not in self._playables:
                    wanted.append(item)
                    complete = False
                if not complete:
                    continue
                source = self._source_for(entry, previous, used)
                assert source is not None
                plan[previous] = source
                used.add(source)
                previous = source
        # Only what the plan needs stays.
        self._variants = {e: v for e, v in self._variants.items() if e in kept}
        needed = {self.playlist.version(e).id for e in kept}
        self._playables = {i: p for i, p in self._playables.items() if i in needed}
        changed = plan != self._plan
        self._plan = plan
        if wanted:
            self._spawn_fetch(wanted)
        if changed and self.player is not None:
            self.player.next_changed()

    # Looking up files

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _spawn_fetch(self, ids: list[int]) -> None:
        if self._loading is not None:
            # Once it's done, the plan is made again and asks again.
            return

        async def fetch() -> None:
            try:
                found = await self._fetch(ids)
            finally:
                self._loading = None
            # After a failure, the next change asks again.
            if found:
                self._update()

        self._loading = asyncio.get_running_loop().create_task(fetch())

    async def _fetch(self, ids: Sequence[int]) -> bool:
        """Look up the files of `ids`; False if the library failed."""
        try:
            found = await self.library.playables(ids)
        except Exception as error:
            log.warning("looking up the playlist's files failed: %s", error)
            return False
        for playable in found:
            self._playables[playable.id] = playable
            self._missing.discard(playable.id)
        self._missing.update(set(ids) - {p.id for p in found})
        return True
