# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The main window: the Slint component wired to the view models."""

import asyncio
import sys
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import beets.library
import slint

from roquette.library.index import LibraryIndex, Track, load_playables
from roquette.measure import Stopwatch
from roquette.modules.covers import CoverGrid
from roquette.modules.tracks import TrackList, format_length
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

        self.index: LibraryIndex | None = None
        self.track_list: TrackList | None = None
        self.track_model = TrackModel()
        w.track_rows = self.track_model
        self.cover_grid: CoverGrid | None = None
        self.cover_model: CoverModel | None = None
        self._cover_columns = 1
        self._cover_cells_shown = 0
        self._album_tracks: dict[int, list[Track]] | None = None

        self.pane = PlaylistPane(placeholder_groups())
        self.playlist_model = PlaylistModel(self.pane, ui.PlaylistRowKind)
        w.playlist_rows = self.playlist_model

        # Set once loaded: the engine (on its own a little after the
        # library), the library and the worker to query it on.
        self.engine: Engine | None = None
        self.library: beets.library.Library | None = None
        self.worker: Executor | None = None
        # The tracks of what the engine plays, by the engine's index.
        self._playing: list[Track] = []
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

    def set_index(self, index: LibraryIndex) -> None:
        self.index = index
        scale = self.options.scale
        self.track_list = TrackList(index.tracks, scale)
        self.track_model.set_tracks(self.track_list)
        self.cover_grid = CoverGrid(index.releases, scale)
        tracks, releases = len(index.tracks) * scale, len(index.releases) * scale
        self.window.status = f"{tracks:,} tracks\n{releases:,} releases"
        self._module_selected(self.window.module)

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
        self.stopwatch.note(f"autoscroll over; track rows read: {reads}{covers}")
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
        track = self.track_list.track(row)
        tracks = self._album_tracks_of(track.album_id) if track.album_id else [track]
        self.play(tracks, tracks.index(track) if track in tracks else 0)

    def _track_sort(self, column: int) -> None:
        if self.track_list is None:
            return
        self.track_list.sort(column)
        self.window.track_sort_column = self.track_list.sort_column
        self.window.track_sort_ascending = self.track_list.ascending
        self.window.track_current = -1
        self.track_model.reset(len(self.track_list))

    def _track_navigate(self, key: str, page: int) -> None:
        if self.track_list is not None and len(self.track_list):
            self.window.track_current = self.track_list.navigate(key, page)

    def _track_menu(self, row: int, action: str) -> None:
        # The spike has no "next" in the playlist; both append.
        if self.track_list is not None and row >= 0:
            self._append([self.track_list.track(row)])

    def _track_drag_data(self, row: int) -> slint.DataTransfer:
        data = slint.DataTransfer()
        if self.track_list is not None:
            track = self.track_list.track(row)
            data.user_data = [track]
            data.plain_text = f"{track.artist} – {track.title}"
        return data

    # Covers

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
            release = self.cover_grid.release(index)
            data.user_data = self._release_tracks(index)
            data.plain_text = f"{release.albumartist} – {release.title}"
        return data

    def _release_tracks(self, index: int) -> list[Track]:
        assert self.cover_grid is not None
        return self._album_tracks_of(self.cover_grid.release(index).id)

    def _album_tracks_of(self, album_id: int) -> list[Track]:
        assert self.index is not None
        if self._album_tracks is None:
            albums: defaultdict[int, list[Track]] = defaultdict(list)
            for track in self.index.tracks:
                albums[track.album_id].append(track)
            self._album_tracks = dict(albums)
        return self._album_tracks.get(album_id, [])

    # Playlist

    def _append(self, tracks: Sequence[Track]) -> None:
        if tracks:
            start, count = self.pane.append(tracks)
            self.playlist_model.rows_changed(start, count)

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
        tracks = data.user_data
        if isinstance(tracks, list) and all(isinstance(t, Track) for t in tracks):
            self._append(tracks)

    # Transport

    def play(self, tracks: Sequence[Track], start: int = 0) -> None:
        """Play `tracks` from `start` on, once their files are looked up."""
        if self.engine is None or self.library is None or self.worker is None:
            return
        library, ids = self.library, [t.id for t in tracks]

        async def look_up() -> None:
            assert self.worker is not None and self.engine is not None
            playables = await self.inbox.run(
                self.worker, lambda: load_playables(library, ids)
            )
            by_id = {t.id: t for t in tracks}
            playing = [by_id[p.id] for p in playables]
            if not playing:
                return
            first = tracks[start].id
            index = next((n for n, t in enumerate(playing) if t.id == first), 0)
            sources = make_sources(
                playables, self.options.replaygain, self.options.replaygain_source
            )
            self._playing = playing
            self.engine.play(sources, index)

        asyncio.get_running_loop().create_task(look_up())

    def _started(self, index: int, source: Source) -> None:
        track = self._playing[index]
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
