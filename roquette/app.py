# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Wiring: S1's window and S2's engine on M1's library layer."""

import argparse
import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from roquette.library import LibraryError
from roquette.measure import Stopwatch
from roquette.threads import Inbox

if TYPE_CHECKING:
    from roquette.playback.engine import Engine


def thumbnail_dir() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(cache) / "thumbnails"


def make_engine(args: argparse.Namespace) -> "Engine":
    """Load GStreamer and create the engine; blocking, for a worker thread."""
    from roquette.playback.engine import Engine

    return Engine(args.audio_sink, preamp=args.preamp)


def run(args: argparse.Namespace) -> int:
    stopwatch = Stopwatch(args.measure)
    # The renderer of docs/decisions/0001-ui-toolkit.md; SLINT_BACKEND
    # picks another until M4 makes it a setting.
    os.environ.setdefault("SLINT_BACKEND", "winit-femtovg")
    import slint

    from roquette.library.query import QueryError
    from roquette.library.service import LibraryService
    from roquette.ui import window as ui

    stopwatch.mark("imports done")

    if args.floor:
        # Python, beets, Slint and GStreamer loaded, an empty window.
        from roquette.library import config, database

        try:
            reader = database.open_reader(config.read_config(args.beets_config))
        except LibraryError as error:
            print(f"roquette: {error}", file=sys.stderr)
            return 1
        engine = make_engine(args)
        stopwatch.mark("library and gstreamer loaded")
        empty = ui.load_ui("empty_window").EmptyWindow()
        empty.show()
        stopwatch.mark("window created")

        async def floor() -> None:
            stopwatch.mark("event loop running")

        slint.run_event_loop(floor())
        engine.close()
        del reader
        return 0

    compiled = ui.load_ui()
    stopwatch.mark("ui compiled")
    options = ui.WindowOptions(
        module=args.module,
        scale=args.scale,
        color_scheme=args.color_scheme,
        thumbnails=None if args.no_thumbnails else thumbnail_dir(),
        cover_cache_bytes=args.cover_cache << 20,
        replaygain=args.replaygain,
        replaygain_source=args.replaygain_source,
    )
    window: ui.MainWindow | None = None
    library: LibraryService | None = None
    # GStreamer is loaded here, while the library opens on its reader.
    pool = ThreadPoolExecutor(1, "roquette gstreamer")
    status = 0

    async def main() -> None:
        nonlocal window, library, status
        stopwatch.mark("event loop running")
        loop = asyncio.get_running_loop()
        inbox = Inbox(loop)
        window = ui.MainWindow(compiled, options, stopwatch, inbox)
        window.show()
        stopwatch.mark("window created")
        gstreamer = asyncio.ensure_future(inbox.run(pool, lambda: make_engine(args)))

        library = LibraryService(inbox, args.beets_config)
        try:
            settings = await library.open()
        except LibraryError as error:
            print(f"roquette: {error}", file=sys.stderr)
            status = 1
            slint.quit_event_loop()
            return
        mode = "writable" if settings.writable else "read-only"
        stopwatch.note(
            f"beets library {settings.database} ({mode}),"
            f" plugins: {' '.join(library.plugins) or 'none'}"
        )
        collection = await library.collection()
        window.set_library(library, collection)
        index = collection.index
        stopwatch.mark(
            f"library loaded ({len(index.tracks)} tracks, "
            f"{len(index.releases)} releases)"
        )
        try:
            library.watch(lambda: reload.start())
        except OSError as error:
            # Without inotify (used up, or a filesystem without it), the
            # rest works; changes by other programs show after a restart.
            print(
                f"roquette: not watching the beets library for changes: {error}",
                file=sys.stderr,
            )
        window.set_engine(await gstreamer)
        stopwatch.mark("gstreamer loaded")
        if args.play:
            try:
                ids = await library.query(args.play)
            except QueryError as error:
                print(f"roquette: --play: {error}", file=sys.stderr)
                status = 1
                slint.quit_event_loop()
                return
            window.play(ids)
        if args.autoscroll:

            def finished() -> None:
                loop.call_later(args.autoscroll_settle, slint.quit_event_loop)

            window.on_autoscroll_finished = finished
            window.start_autoscroll(args.autoscroll, args.autoscroll_speed)

    class Reload:
        """Reloads the index after outside changes, one load at a time."""

        def __init__(self) -> None:
            self.task: asyncio.Task[None] | None = None
            self.again = False

        def start(self) -> None:
            if self.task is not None:
                self.again = True
                return
            self.task = asyncio.get_running_loop().create_task(self.run())

        async def run(self) -> None:
            try:
                while True:
                    self.again = False
                    assert library is not None and window is not None
                    collection = await library.collection()
                    window.set_collection(collection)
                    stopwatch.note(
                        f"library changed ({len(collection.index.tracks)} tracks)"
                    )
                    if not self.again:
                        return
            finally:
                self.task = None

    reload = Reload()

    try:
        slint.run_event_loop(main())
    finally:
        if window is not None:
            window.close()
        if library is not None:
            library.close()
        pool.shutdown(wait=False, cancel_futures=True)
    return status
