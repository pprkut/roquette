# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The main window: the Slint component wired to the view models."""

import asyncio
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import slint

from roquette.library.index import LibraryIndex, Track
from roquette.measure import Stopwatch
from roquette.modules.covers import CoverGrid
from roquette.modules.tracks import TrackList, format_length
from roquette.queue.pane import PlaylistPane, placeholder_groups, stars
from roquette.threads import Inbox
from roquette.ui.models import CoverModel, PlaylistModel, TrackModel

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

        self.playing = False
        self._position = 0.0
        self._ticker: asyncio.Task[None] | None = None
        self._show_now_playing()

        self.on_autoscroll_finished: Callable[[], None] | None = None
        # GStreamer's playbin3, loaded but unused until S2.
        self.player: Any = None

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
        w.previous = lambda: None
        w.next = lambda: None
        w.seek = self._seek
        w.autoscroll_finished = self._autoscroll_finished

    def show(self) -> None:
        self.window.show()

    def close(self) -> None:
        if self.cover_model is not None:
            self.cover_model.close()

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
        if self.track_list is not None and row >= 0:
            self._append([self.track_list.track(row)])

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
        self._track_activate(row)

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
        if self.cover_grid is not None and index >= 0:
            self._append(self._release_tracks(index))

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
        assert self.index is not None and self.cover_grid is not None
        if self._album_tracks is None:
            albums: defaultdict[int, list[Track]] = defaultdict(list)
            for track in self.index.tracks:
                albums[track.album_id].append(track)
            self._album_tracks = dict(albums)
        return self._album_tracks.get(self.cover_grid.release(index).id, [])

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

    # Transport: a stand-in that counts the position up while "playing".

    def _show_now_playing(self) -> None:
        w = self.window
        playing = self.pane.playing
        if playing is None:
            return
        group = self.pane.groups[playing[0]]
        entry = group.entries[playing[1]]
        w.now_title = entry.title
        w.now_artist = f"{entry.artist} · {group.title}"
        w.now_rating = stars(entry.rating)
        w.duration = entry.length
        w.duration_text = format_length(entry.length)
        self._show_position()

    def _show_position(self) -> None:
        self.window.position = self._position
        self.window.position_text = format_length(self._position)

    def _play_pause(self) -> None:
        self.playing = not self.playing
        self.window.playing = self.playing
        if self.playing:
            self._ticker = asyncio.get_running_loop().create_task(self._tick())
        elif self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None

    async def _tick(self) -> None:
        # A few updates per second, as the position display will have.
        while True:
            await asyncio.sleep(0.25)
            self._position = min(self.window.duration, self._position + 0.25)
            self._show_position()

    def _seek(self, position: float) -> None:
        self._position = position
        self._show_position()
