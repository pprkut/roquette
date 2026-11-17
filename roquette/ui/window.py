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

from roquette.library.index import Playable
from roquette.library.query import QueryError
from roquette.library.service import Collection, LibraryService
from roquette.measure import Stopwatch
from roquette.modules.covers import CoverGrid
from roquette.modules.tracks import TrackList, format_length, sort_query
from roquette.playback.source import Source, State
from roquette.queue.pane import PlaylistPane, placeholder_groups
from roquette.queue.replaygain import make_sources
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


# The position display ticks once a second, this long after each second
# of the track starts, so the text changes on every tick and only then.
TICK_DELAY = 0.02
# "Previous" goes back to the start of the track after this many seconds.
RESTART_AFTER = 3.0


class DraggedTracks(NamedTuple):
    """What a drag from the track list or the cover grid carries."""

    ids: tuple[int, ...]


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

        self.pane = PlaylistPane(placeholder_groups())
        self.playlist_model = PlaylistModel(self.pane, ui.PlaylistRowKind)
        w.playlist_rows = self.playlist_model

        # Set once loaded: the engine (on its own a little after the
        # library) and the library.
        self.engine: Engine | None = None
        self.library: LibraryService | None = None
        # The tracks of what the engine plays, by the engine's index.
        self._playing: list[Playable] = []
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
        if self.engine is not None:
            self.engine.close()

    def set_engine(self, engine: "Engine") -> None:
        """The engine, attached to the running event loop."""
        self.engine = engine
        engine.attach(asyncio.get_running_loop())
        engine.on_started = self._started
        engine.on_state_changed = self._state_changed
        engine.on_error = self._error
        engine.volume = volume_factor(self.window.volume)

    def set_library(self, library: LibraryService, collection: Collection) -> None:
        """The library, opened, and its index."""
        self.library = library
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
        """The library's index, again after the library changed."""
        assert self.track_list is not None and self.cover_grid is not None
        self.collection = collection
        index = collection.index
        self.track_list.text.clear()
        self.track_list.set_ids(index.tracks.ids)
        self._track_order = (0, True)
        self.window.track_current = -1
        self.track_model.set_tracks(self.track_list)
        if self.track_list.sort_column:
            self._sort_tracks(
                sort_query(self.track_list.sort_column, self.track_list.ascending)
            )
        self.cover_grid.text.clear()
        self.cover_grid.set_ids(index.releases.ids)
        self.window.cover_current = -1
        if self.cover_model is not None:
            # Covers are cached by cell.
            self.cover_model.close()
            self.cover_model = None
        scale = self.options.scale
        tracks, releases = len(index.tracks) * scale, len(index.releases) * scale
        self.window.status = f"{tracks:,} tracks\n{releases:,} releases"
        self._module_selected(self.window.module)

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
        """Play the track's release from the track on (S2's stand-in until
        the live playlist, M3)."""
        if self.track_list is None or row < 0:
            return
        item_id = self.track_list.id(row)
        ids = self._release_of_track(item_id)
        self.play(ids, ids.index(item_id))

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
        # The spike has no "next" in the playlist; both append.
        if self.track_list is not None and row >= 0:
            self._append([self.track_list.id(row)])

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
        """Play the release (S2's stand-in until the live playlist, M3)."""
        if self.cover_grid is not None and index >= 0:
            self.play(self._release_tracks(index))

    def _cover_navigate(self, key: str, columns: int) -> None:
        if self.cover_grid is not None and len(self.cover_grid):
            self.window.cover_current = self.cover_grid.navigate(key, columns)

    def _cover_drag_data(self, index: int) -> slint.DataTransfer:
        data = slint.DataTransfer()
        if self.cover_grid is not None:
            data.user_data = DraggedTracks(tuple(self._release_tracks(index)))
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
        if group is None:
            return []
        span = self.collection.groups.tracks(group)
        return self.collection.index.tracks.ids[span.start : span.stop].tolist()

    def _release_of_track(self, item_id: int) -> list[int]:
        """The ids of the tracks of a track's release; the track alone if
        it's a singleton."""
        assert self.collection is not None
        tracks = self.collection.index.tracks
        try:
            position = tracks.ids.index(item_id)
        except ValueError:
            return [item_id]
        album_id = tracks.album_ids[position]
        return self._album_tracks(album_id) if album_id else [item_id]

    # Playlist

    def _append(self, ids: Sequence[int]) -> None:
        """Add tracks to the pane, once their text is read."""
        if not ids or self.library is None:
            return
        library, wanted = self.library, list(ids)

        async def append() -> None:
            text = await library.track_text(wanted)
            tracks = [text[i] for i in wanted if i in text]
            if tracks:
                start, count = self.pane.append(tracks)
                self.playlist_model.rows_changed(start, count)

        self._spawn(append())

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
            self._append(dragged.ids)

    # Transport

    def play(self, ids: Sequence[int], start: int = 0) -> None:
        """Play the tracks `ids` from `start` on, once their files are
        looked up."""
        if self.engine is None or self.library is None or not ids:
            return
        library, wanted, first = self.library, list(ids), ids[start]
        self._play_requests += 1
        request = self._play_requests

        async def look_up() -> None:
            playables = await library.playables(wanted)
            # A later look-up wins, wherever this one was held up.
            if request != self._play_requests or self.engine is None:
                return
            if not playables:
                return
            index = next((n for n, p in enumerate(playables) if p.id == first), 0)
            sources = make_sources(
                playables, self.options.replaygain, self.options.replaygain_source
            )
            self._playing = playables
            self.engine.play(sources, index)

        self._spawn(look_up())

    def _started(self, index: int, source: Source) -> None:
        track = self._playing[index].text
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
        self.stopwatch.mark(f"playing track {index + 1} of {len(self._playing)}")

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

    def _error(self, source: Source | None, message: str) -> None:
        name = source.path.name if source is not None else "?"
        print(f"roquette: can't play {name}: {message}", file=sys.stderr)

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
        engine = self.engine
        if engine is None:
            return
        if engine.state is State.PLAYING:
            engine.pause()
        elif engine.state is State.PAUSED:
            engine.resume()
        elif engine.sources:
            engine.play(engine.sources)

    def _previous(self) -> None:
        engine = self.engine
        if engine is None or engine.current is None:
            return
        if (engine.position() or 0.0) > RESTART_AFTER or not engine.skip(-1):
            self._seek(0.0)

    def _next(self) -> None:
        if self.engine is not None:
            self.engine.skip(1)

    def _seek(self, position: float) -> None:
        if self.engine is not None and self.engine.current is not None:
            self.engine.seek(position)
            self._show_position(position)

    def _volume_changed(self, percent: float) -> None:
        if self.engine is not None:
            self.engine.volume = volume_factor(percent)
