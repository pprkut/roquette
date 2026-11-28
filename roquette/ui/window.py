# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The main window: the Slint component wired to the view models."""

import asyncio
import sqlite3
import sys
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple

import slint

from roquette.library import LibraryError
from roquette.library.index import TrackText
from roquette.library.query import QueryError
from roquette.library.service import Collection, LibraryService
from roquette.measure import Stopwatch
from roquette.modules.covers import CoverGrid
from roquette.modules.tracks import TrackList, format_length, sort_query
from roquette.playback.gain import AppliedGain
from roquette.playback.source import PlaybackError, State
from roquette.queue.live import LivePlaylist, QueuedSource
from roquette.queue.m3u import PlaylistFile
from roquette.queue.pane import PlaylistPane, groups_for
from roquette.queue.playlist import Repeat, Shuffle
from roquette.threads import Inbox
from roquette.ui.models import CoverModel, PlaylistModel, TrackModel

if TYPE_CHECKING:
    # Imports GStreamer; the engine is made on a worker thread.
    from roquette.playback.engine import Engine

UI_DIR = Path(__file__).resolve().parent

MODULES = ("tracks", "covers")


def load_ui(name: str = "main_window") -> SimpleNamespace:
    return slint.load_file(UI_DIR / f"{name}.slint")


@dataclass(frozen=True, slots=True)
class WindowOptions:
    module: str = "tracks"
    # Repeats the library this many times in the views.
    scale: int = 1
    color_scheme: str | None = None
    # The shared thumbnail cache, or None to decode every cover itself.
    thumbnails: Path | None = None
    cover_cache_bytes: int = 32 << 20
    # ReplayGain: "auto", "track", "album" or "off"; from "beets" or "tags".
    replaygain: str = "auto"
    replaygain_source: str = "beets"
    # Repeat and shuffle, as `Repeat` and `Shuffle` have them.
    repeat: str = "off"
    shuffle: str = "off"
    # Where the live playlist is kept, or None not to keep it.
    playlist_file: Path | None = None


# The position display ticks once a second, this long after each second
# of the track starts, so the text changes on every tick and only then.
TICK_DELAY = 0.02


class DraggedTracks(NamedTuple):
    """What a drag from the track list or the cover grid carries."""

    ids: tuple[int, ...]
    # A whole release, pinned to its versions.
    pinned: bool = False


def volume_factor(percent: float) -> float:
    """The volume slider's 0–100 to a linear factor, on a cubic curve as
    desktop volume controls use."""
    return float((min(max(percent, 0.0), 100.0) / 100) ** 3)


class MainWindow:
    def __init__(
        self,
        ui: SimpleNamespace,
        options: WindowOptions,
        stopwatch: Stopwatch,
        inbox: Inbox,
    ) -> None:
        self.options = options
        self.inbox = inbox
        self.stopwatch = stopwatch
        self.window: Any = ui.MainWindow()
        w = self.window
        if options.color_scheme:
            w.color_scheme_override = options.color_scheme

        self.collection: Collection | None = None
        self.track_list: TrackList | None = None
        self.track_model = TrackModel()
        w.track_rows = self.track_model
        self.cover_grid: CoverGrid | None = None
        self.cover_model: CoverModel | None = None
        self._cover_columns = 1
        self._cover_cells_shown = 0
        # Counts the sorts of the track list: only the latest one's ids count.
        self._sorts = 0
        # The column and direction of the rows the track list shows.
        self._track_order = (0, True)
        self._tasks: set[asyncio.Task[Any]] = set()

        self.pane = PlaylistPane([])
        self.playlist_model = PlaylistModel(self.pane, ui.PlaylistRowKind)
        w.playlist_rows = self.playlist_model

        # Set once loaded: the engine (on its own a little after the
        # library), the library and the live playlist on it.
        self.engine: Engine | None = None
        self.library: LibraryService | None = None
        self.live: LivePlaylist | None = None
        self.playlist_file = (
            PlaylistFile(options.playlist_file, inbox)
            if options.playlist_file is not None
            else None
        )
        # The live playlist is saved once the saved one is restored.
        self._restored = False
        # The text of the live playlist's tracks, for the pane.
        self._texts: dict[int, TrackText] = {}
        self._pane_task: asyncio.Task[None] | None = None
        self._pane_again = False
        # Counts the calls of `play`: only the latest one's look-up plays.
        self._play_requests = 0
        self._sink_noted = False
        self._ticker: asyncio.Task[None] | None = None
        self._clear_now_playing()

        self.on_autoscroll_finished: Callable[[], None] | None = None

        w.module = MODULES.index(options.module)
        w.status = "Loading the library…"
        w.module_selected = self._module_selected
        w.track_select = self._track_select
        w.track_activate = self._track_activate
        w.track_sort = self._track_sort
        w.track_navigate = self._track_navigate
        w.track_menu = self._track_menu
        w.track_drag_data = self._track_drag_data
        w.cover_columns_changed = self._cover_columns_changed
        w.cover_cells_shown_changed = self._cover_cells_shown_changed
        w.cover_select = self._cover_select
        w.cover_activate = self._cover_activate
        w.cover_navigate = self._cover_navigate
        w.cover_drag_data = self._cover_drag_data
        w.playlist_select = self._playlist_select
        w.playlist_toggle = self._playlist_toggle
        w.playlist_choose = self._playlist_choose
        w.playlist_navigate = self._playlist_navigate
        w.playlist_dropped = self._playlist_dropped
        w.play_pause = self._play_pause
        w.previous = self._previous
        w.next = self._next
        w.seek = self._seek
        w.volume_changed = self._volume_changed
        w.autoscroll_finished = self._autoscroll_finished

    def show(self) -> None:
        self.window.show()

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.track_list is not None:
            self.track_list.close()
        if self.cover_grid is not None:
            self.cover_grid.close()
        if self.cover_model is not None:
            self.cover_model.close()
        if self._ticker is not None:
            self._ticker.cancel()
        if self.live is not None:
            self.live.close()
        if self.playlist_file is not None:
            self.playlist_file.close()
        if self.engine is not None:
            self.engine.close()

    def set_engine(self, engine: "Engine") -> None:
        """The engine, attached to the running event loop."""
        self.engine = engine
        engine.attach(asyncio.get_running_loop())
        if self.live is not None:
            self.live.attach(engine)
        engine.on_state_changed = self._state_changed
        engine.on_error = self._error
        engine.volume = volume_factor(self.window.volume)

    def set_library(self, library: LibraryService, collection: Collection) -> None:
        """The library, opened, and its index."""
        self.library = library
        options = self.options
        live = LivePlaylist(
            library,
            replaygain=options.replaygain,
            gain_source=options.replaygain_source,
        )
        live.playlist.repeat = Repeat(options.repeat)
        live.playlist.shuffle = Shuffle(options.shuffle)
        live.on_started = self._started
        live.on_changed = self._playlist_changed
        if self.engine is not None:
            live.attach(self.engine)
        self.live = live
        scale = self.options.scale
        self.track_list = TrackList(
            collection.index.tracks.ids,
            library.track_text,
            self.track_model.rows_loaded,
            scale,
        )
        self.cover_grid = CoverGrid(
            collection.index.releases.ids,
            library.release_text,
            self._cover_cells_loaded,
            scale,
        )
        self.set_collection(collection)

    def set_collection(self, collection: Collection) -> None:
        """The library's index, again after the library changed (then the
        live playlist reads its tracks again too).

        The views show their text from before until it's read again, so
        that they don't flash empty; the track list keeps its rows until
        they're sorted again, and the cover grid its covers if the
        releases are the same.
        """
        assert self.track_list is not None and self.cover_grid is not None
        first = self.collection is None
        self.collection = collection
        index = collection.index
        tracks = self.track_list
        tracks.text.refresh()
        if tracks.sort_column and not first:
            self._sort_tracks(sort_query(tracks.sort_column, tracks.ascending))
        else:
            tracks.set_ids(index.tracks.ids)
            self._track_order = (tracks.sort_column, tracks.ascending)
            self.window.track_current = -1
            self.track_model.set_tracks(tracks)
        grid = self.cover_grid
        grid.text.refresh()
        if grid.ids != index.releases.ids:
            grid.set_ids(index.releases.ids)
            self.window.cover_current = -1
            if self.cover_model is not None:
                # Covers are cached by cell.
                self.cover_model.close()
                self.cover_model = None
        elif self.cover_model is not None:
            self.cover_model.reset(self.cover_model.row_count())
        scale = self.options.scale
        tracks_count = len(index.tracks) * scale
        releases_count = len(index.releases) * scale
        self.window.status = f"{tracks_count:,} tracks\n{releases_count:,} releases"
        self._module_selected(self.window.module)
        if not first and self.live is not None:
            self._texts.clear()
            self._spawn(self.live.reload())

    def _spawn(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # The autoscroll measurement

    def start_autoscroll(self, module: str, speed: float) -> None:
        index = MODULES.index(module)
        if self.window.module != index:
            self.window.module = index
            self._module_selected(index)
        self.window.autoscroll_speed = speed
        self.window.autoscrolling = True
        self.stopwatch.note(f"autoscroll started ({module}, {speed:.0f} px/s)")

    def _autoscroll_finished(self) -> None:
        reads = self.track_model.reads
        covers = ""
        if self.cover_model is not None:
            count, size = self.cover_model.cached
            covers = (
                f", cover cells read: {self.cover_model.reads},"
                f" covers loaded: {self.cover_model.loaded}, cached: {count}"
                f" ({size / 2**20:.1f} MiB)"
            )
        texts = self.track_list.text.reads if self.track_list is not None else 0
        self.stopwatch.note(
            f"autoscroll over; track rows read: {reads}, text reads: {texts}{covers}"
        )
        if self.on_autoscroll_finished is not None:
            self.on_autoscroll_finished()

    # Navigation

    def _module_selected(self, index: int) -> None:
        if MODULES[index] == "covers":
            if self.cover_model is None and self.cover_grid is not None:
                size = round(self.window.Theme.cover_size * self.window.scale_factor)
                self.cover_model = CoverModel(
                    self.cover_grid,
                    size,
                    self.options.thumbnails,
                    self.options.cover_cache_bytes,
                    self.inbox,
                )
                # Given to Slint empty, then filled (see LazyModel).
                self.window.cover_rows = self.cover_model
                self.cover_model.set_cells_shown(self._cover_cells_shown)
                self.cover_model.set_columns(self._cover_columns)
        elif self.cover_model is not None:
            # Covers that were waiting to load aren't shown any more.
            self.cover_model.cancel_loading()

    # Tracks

    def _track_select(self, row: int) -> None:
        if self.track_list is not None:
            self.track_list.current = row
            self.window.track_current = row

    def _track_activate(self, row: int) -> None:
        """Add the track's release to the playlist and play it from the
        track on (until the tree module decides, M6)."""
        if self.track_list is None or row < 0:
            return
        item_id = self.track_list.id(row)
        ids = self._release_of_track(item_id, self.track_list.position(row))
        self._add(ids, pinned=len(ids) > 1, play=ids.index(item_id))

    def _track_sort(self, column: int) -> None:
        if self.track_list is None:
            return
        self._sort_tracks(self.track_list.sort(column))
        self._show_track_sort()

    def _show_track_sort(self) -> None:
        assert self.track_list is not None
        self.window.track_sort_column = self.track_list.sort_column
        self.window.track_sort_ascending = self.track_list.ascending

    def _sort_tracks(self, query: str) -> None:
        """Show the tracks in the order of a beets query's sort."""
        self._sorts += 1
        sort = self._sorts

        async def sort_tracks() -> None:
            assert self.library is not None and self.collection is not None
            try:
                ids = (
                    await self.library.query(query)
                    if query
                    else self.collection.index.tracks.ids
                )
            except (QueryError, sqlite3.Error) as error:
                # The rows stay as they were, and so does the header.
                print(f"roquette: sorting the tracks: {error}", file=sys.stderr)
                if sort == self._sorts and self.track_list is not None:
                    tracks = self.track_list
                    tracks.sort_column, tracks.ascending = self._track_order
                    self._show_track_sort()
                return
            if sort != self._sorts or self.track_list is None:
                return
            self.track_list.set_ids(ids)
            self._track_order = (self.track_list.sort_column, self.track_list.ascending)
            self.window.track_current = -1
            self.track_model.reset(len(self.track_list))

        if self.library is not None:
            self._spawn(sort_tracks())

    def _track_navigate(self, key: str, page: int) -> None:
        if self.track_list is not None and len(self.track_list):
            self.window.track_current = self.track_list.navigate(key, page)

    def _track_menu(self, row: int, action: str) -> None:
        if self.track_list is not None and row >= 0:
            how = "next" if action == "play-next" else "append"
            self._add([self.track_list.id(row)], how=how)

    def _track_drag_data(self, row: int) -> slint.DataTransfer:
        data = slint.DataTransfer()
        if self.track_list is not None:
            data.user_data = DraggedTracks((self.track_list.id(row),))
            track = self.track_list.track(row)
            if track is not None:
                data.plain_text = f"{track.artist} – {track.title}"
        return data

    # Covers

    def _cover_cells_loaded(self, cells: list[int]) -> None:
        if self.cover_model is not None:
            self.cover_model.cells_loaded(cells)

    def _cover_columns_changed(self, columns: int) -> None:
        self._cover_columns = columns
        if self.cover_model is not None and columns != self.cover_model.columns:
            self.cover_model.set_columns(columns)

    def _cover_cells_shown_changed(self, cells: int) -> None:
        self._cover_cells_shown = cells
        if self.cover_model is not None:
            self.cover_model.set_cells_shown(cells)

    def _cover_select(self, index: int) -> None:
        if self.cover_grid is not None:
            self.cover_grid.current = index
            self.window.cover_current = index

    def _cover_activate(self, index: int) -> None:
        """Add the release to the playlist and play it."""
        if self.cover_grid is not None and index >= 0:
            self._add(self._release_tracks(index), pinned=True, play=0)

    def _cover_navigate(self, key: str, columns: int) -> None:
        if self.cover_grid is not None and len(self.cover_grid):
            self.window.cover_current = self.cover_grid.navigate(key, columns)

    def _cover_drag_data(self, index: int) -> slint.DataTransfer:
        data = slint.DataTransfer()
        if self.cover_grid is not None:
            data.user_data = DraggedTracks(tuple(self._release_tracks(index)), True)
            release = self.cover_grid.release(index)
            if release is not None:
                data.plain_text = f"{release.albumartist} – {release.title}"
        return data

    def _release_tracks(self, index: int) -> list[int]:
        assert self.cover_grid is not None
        return self._album_tracks(self.cover_grid.id(index))

    def _album_tracks(self, album_id: int) -> list[int]:
        """The ids of a release's tracks, in its order."""
        if self.collection is None:
            return []
        group = self.collection.groups.of_album(album_id)
        return [] if group is None else self._group_tracks(group)

    def _group_tracks(self, group: int) -> list[int]:
        assert self.collection is not None
        span = self.collection.groups.tracks(group)
        return self.collection.index.tracks.ids[span.start : span.stop].tolist()

    def _release_of_track(self, item_id: int, position: int | None) -> list[int]:
        """The ids of the tracks of a track's release; the track alone if
        it's a singleton. `position` is the track's in the index, if
        known; otherwise it's looked up."""
        assert self.collection is not None
        tracks, groups = self.collection.index.tracks, self.collection.groups
        if position is None:
            try:
                position = tracks.ids.index(item_id)
            except ValueError:
                return [item_id]
        group = groups.of_track(position)
        return self._group_tracks(group) if groups.album_ids[group] else [item_id]

    # Playlist

    async def load_playlist(self) -> None:
        """Restore the live playlist saved last time."""
        live, file, library = self.live, self.playlist_file, self.library
        if live is None or library is None:
            return
        if file is None:
            self._restored = True
            return
        try:
            entries, current = await file.load(library)
        except (OSError, LibraryError, sqlite3.Error) as error:
            # Not saved over either, until the next start.
            print(
                f"roquette: reading the live playlist {file.path}: {error}",
                file=sys.stderr,
            )
            return
        if not len(live.playlist):
            live.restore(entries, current)
        self._restored = True

    def _add(
        self,
        ids: Sequence[int],
        *,
        pinned: bool = False,
        how: str = "append",
        play: int | None = None,
    ) -> None:
        """Add tracks to the live playlist ("append", "next" or "replace"
        it), once they're looked up, and play the one at `play`."""
        if not ids or self.library is None:
            return
        library, wanted = self.library, list(ids)
        self._play_requests += how == "replace"
        request = self._play_requests

        async def add() -> None:
            items = await library.playlist_items(wanted)
            live = self.live
            # A later "replace" wins, wherever this one was held up.
            if live is None or not items or request != self._play_requests:
                return
            if how == "replace":
                live.replace(items, pinned=pinned)
                start = 0
            elif how == "next":
                start = live.play_next(items, pinned=pinned)
            else:
                start = live.append(items, pinned=pinned)
            if play is not None:
                first = wanted[play]
                offset = next((n for n, i in enumerate(items) if i.id == first), 0)
                live.play(live.playlist[start + offset])

        self._spawn(add())

    def _playlist_changed(self) -> None:
        if self.live is not None and self.playlist_file is not None and self._restored:
            self.playlist_file.changed(self.live.playlist)
        self._refresh_pane()

    def _refresh_pane(self) -> None:
        """Show the live playlist in the pane, once its text is read."""
        if self._pane_task is not None:
            self._pane_again = True
            return

        async def refresh() -> None:
            try:
                while True:
                    self._pane_again = False
                    await self._show_playlist()
                    if not self._pane_again:
                        return
            finally:
                self._pane_task = None

        self._pane_task = asyncio.get_running_loop().create_task(refresh())
        self._tasks.add(self._pane_task)
        self._pane_task.add_done_callback(self._tasks.discard)

    async def _show_playlist(self) -> None:
        live, library = self.live, self.library
        if live is None or library is None:
            return
        ids = {e.item.id for e in live.playlist}
        missing = ids - self._texts.keys()
        if missing:
            try:
                self._texts.update(await library.track_text(missing))
            except sqlite3.Error as error:
                print(f"roquette: the playlist's tracks: {error}", file=sys.stderr)
        self._texts = {i: t for i, t in self._texts.items() if i in ids}
        groups, playing = groups_for(list(live.playlist), self._texts, live.current)
        self.pane.set_groups(groups, playing)
        self.playlist_model.reset(len(self.pane))
        self.window.playlist_current = self.pane.current

    def _playlist_select(self, row: int) -> None:
        self.pane.current = row
        self.window.playlist_current = row

    def _playlist_toggle(self, row: int) -> None:
        start, count = self.pane.toggle(row)
        self.playlist_model.notify_row_changed(row)
        self.playlist_model.rows_changed(start, count)
        self.window.playlist_current = self.pane.current

    def _playlist_choose(self, row: int) -> None:
        for changed in self.pane.choose(row):
            self.playlist_model.notify_row_changed(changed)

    def _playlist_navigate(self, key: str) -> None:
        current, toggled = self.pane.navigate(key)
        if toggled is not None:
            self.playlist_model.notify_row_changed(current)
            self.playlist_model.rows_changed(*toggled)
        self.window.playlist_current = current

    def _playlist_dropped(self, data: slint.DataTransfer) -> None:
        dragged = data.user_data
        if isinstance(dragged, DraggedTracks):
            self._add(dragged.ids, pinned=dragged.pinned)

    # Transport

    def play(self, ids: Sequence[int], start: int = 0) -> None:
        """Replace the live playlist with the tracks `ids` and play them
        from `start` on, once they're looked up."""
        self._add(ids, how="replace", play=start)

    def _started(self, source: QueuedSource, gain: AppliedGain) -> None:
        playable = source.playable
        if playable is None:
            return
        track = playable.text
        w = self.window
        w.now_title = track.title
        w.now_artist = (
            f"{track.artist} · {track.album}" if track.album else track.artist
        )
        w.now_rating = ""
        w.duration = source.length or track.length
        w.duration_text = format_length(w.duration)
        self._show_position(0.0)
        if self.engine is not None and not self._sink_noted:
            self._sink_noted = True
            self.stopwatch.note(f"audio sink: {self.engine.sink_name()}")
        if self.live is not None and source.entry in self.live.playlist:
            playlist = self.live.playlist
            assert source.entry is not None
            number = playlist.index(source.entry) + 1
            self.stopwatch.mark(f"playing track {number} of {len(playlist)}")

    def _state_changed(self, state: State) -> None:
        playing = state is State.PLAYING
        self.window.playing = playing
        if playing and self._ticker is None:
            self._ticker = asyncio.get_running_loop().create_task(self._tick())
        elif not playing and self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        if state is State.STOPPED:
            self._clear_now_playing()
        elif self.engine is not None:
            self._show_position(self.engine.position() or 0.0)

    def _error(self, error: PlaybackError) -> None:
        name = error.source.path if error.source is not None else "?"
        then = "skipped" if error.skipped_to is not None else "stopped"
        print(
            f"roquette: can't play {name} ({error.problem.value}: {error.message});"
            f" {then}",
            file=sys.stderr,
        )

    def _clear_now_playing(self) -> None:
        w = self.window
        w.now_title = ""
        w.now_artist = ""
        w.now_rating = ""
        w.duration = 0.0
        w.duration_text = format_length(0)
        self._show_position(0.0)

    def _show_position(self, position: float) -> None:
        self.window.position = position
        self.window.position_text = format_length(position)

    async def _tick(self) -> None:
        # Only while playing; paused or stopped, nothing wakes up. Every
        # change of the display costs a few frames, so it changes once a
        # second, when the text does.
        while True:
            position = self.engine.position() if self.engine is not None else None
            if position is None:
                await asyncio.sleep(1.0)
                continue
            self._show_position(position)
            await asyncio.sleep(1.0 - position % 1.0 + TICK_DELAY)

    def _play_pause(self) -> None:
        if self.live is not None:
            self.live.play_pause()

    def _previous(self) -> None:
        if self.live is not None:
            self.live.previous()

    def _next(self) -> None:
        if self.live is not None:
            self.live.next()

    def _seek(self, position: float) -> None:
        if self.engine is not None and self.engine.current is not None:
            self.engine.seek(position)
            self._show_position(position)

    def _volume_changed(self, percent: float) -> None:
        if self.engine is not None:
            self.engine.volume = volume_factor(percent)
