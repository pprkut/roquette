# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Slint models over the view models; rows are built when a view asks."""

import asyncio
from enum import Enum
from pathlib import Path
from typing import Any

import slint

from roquette.covers import CoverLoader, ImageCache, Pixels
from roquette.modules.covers import CoverGrid
from roquette.modules.tracks import TrackList
from roquette.queue.pane import PlaylistPane
from roquette.threads import Inbox

type RowData = dict[str, Any]


class LazyModel(slint.Model[RowData]):
    """A model that is given to Slint empty and filled afterwards.

    Assigning a model to a property makes Slint read every row of it once
    (slint 1.18.1b1); rows added to a model Slint already has are only
    read when a view shows them.
    """

    def __init__(self) -> None:
        super().__init__()
        self._count = 0

    def row_count(self) -> int:
        return self._count

    def reset(self, count: int) -> None:
        if self._count:
            removed, self._count = self._count, 0
            self.notify_row_removed(0, removed)
        self._count = count
        if count:
            self.notify_row_added(0, count)

    def row_data(self, row: int) -> RowData | None:
        raise NotImplementedError


class TrackModel(LazyModel):
    def __init__(self) -> None:
        super().__init__()
        self.tracks: TrackList | None = None
        # How many rows the view asked for, for the measurements.
        self.reads = 0

    def set_tracks(self, tracks: TrackList) -> None:
        self.tracks = tracks
        self.reset(len(tracks))

    def row_data(self, row: int) -> RowData | None:
        if self.tracks is None or not 0 <= row < len(self.tracks):
            return None
        self.reads += 1
        return self.tracks.row(row)._asdict()


class CoverModel(LazyModel):
    """The cover grid's rows of `columns` cells; covers are loaded when a
    row is shown.

    The decoded covers are kept in a cache bounded by its size in bytes;
    a cell that scrolls back into view after its cover was evicted loads
    it again.
    """

    def __init__(
        self,
        grid: CoverGrid,
        size: int,
        thumbnails: Path | None,
        cache_bytes: int,
        inbox: Inbox,
    ) -> None:
        super().__init__()
        self.grid = grid
        self.columns = 1
        self._cache: ImageCache[int, slint.Image] = ImageCache(cache_bytes)
        self._missing: set[int] = set()
        self._empty = slint.Image()

        def deliver(index: int, pixels: Pixels | None) -> None:
            inbox.post(self._loaded, index, pixels)

        self._loader: CoverLoader[int] = CoverLoader(size, thumbnails, deliver)
        self.loaded = 0
        self.reads = 0
        # Rows with newly loaded covers, updated together.
        self._changed_rows: set[int] = set()

    @property
    def cached(self) -> tuple[int, int]:
        """How many covers are cached, and their size in bytes."""
        return len(self._cache), self._cache.bytes

    def set_cells_shown(self, cells: int) -> None:
        """At most `cells` cells are on screen at once."""
        self._loader.set_max_pending(max(64, cells))

    def set_columns(self, columns: int) -> None:
        self.columns = max(1, columns)
        self.reset(-(-len(self.grid) // self.columns))

    def cancel_loading(self) -> None:
        self._loader.cancel_all()

    def close(self) -> None:
        self._loader.close()
        self._cache.clear()

    def row_data(self, row: int) -> RowData | None:
        first = row * self.columns
        if not 0 <= first < len(self.grid):
            return None
        last = min(first + self.columns, len(self.grid))
        # Arrays in structs must be models: a list fails to convert (and
        # Slint only prints the error).
        cells = slint.ListModel([self._cell(i) for i in range(first, last)])
        return {"first": first, "cells": cells}

    def _cell(self, index: int) -> RowData:
        self.reads += 1
        title, artist, year = self.grid.text(index)
        cover = self._cache.get(index)
        has_cover = index not in self._missing
        if cover is None:
            path = self.grid.release(index).cover
            if path is None:
                has_cover = False
                self._missing.add(index)
            elif has_cover:
                self._loader.request(index, path)
        return {
            "title": title,
            "artist": artist,
            "year": year,
            "cover": cover or self._empty,
            "has-cover": has_cover,
        }

    def _loaded(self, index: int, pixels: Pixels | None) -> None:
        if pixels is None:
            self._missing.add(index)
        else:
            shape = (pixels.height, pixels.width, 3)
            image = slint.Image.load_from_array(
                memoryview(pixels.data).cast("B", shape)
            )
            self._cache.put(index, image, len(pixels.data))
            self.loaded += 1
        if not self._changed_rows:
            asyncio.get_running_loop().call_soon(self._update_rows)
        self._changed_rows.add(index // self.columns)

    def _update_rows(self) -> None:
        for row in sorted(self._changed_rows):
            if row < self._count:
                self.notify_row_changed(row)
        self._changed_rows.clear()


class PlaylistModel(LazyModel):
    def __init__(self, pane: PlaylistPane, kinds: type[Enum]) -> None:
        super().__init__()
        self.pane = pane
        # The Slint enum PlaylistRowKind, by RowKind's values.
        self._kinds = list(kinds)
        self.reset(len(pane))

    def row_data(self, row: int) -> RowData | None:
        if not 0 <= row < len(self.pane):
            return None
        data = self.pane.row(row)._asdict()
        data["kind"] = self._kinds[data["kind"]]
        return data

    def rows_changed(self, start: int, count: int) -> None:
        """`count` rows were inserted at `start`, or removed if negative."""
        if count > 0:
            self._count += count
            self.notify_row_added(start, count)
        elif count < 0:
            self._count += count
            self.notify_row_removed(start, -count)
