# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Wiring: S1's window and S2's engine on the beets library of `$BEETSDIR`."""

import argparse
import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from roquette.measure import Stopwatch
from roquette.threads import Inbox

if TYPE_CHECKING:
    import beets.library

    from roquette.library.index import LibraryIndex
    from roquette.playback.engine import Engine


class NotADevelopmentCopy(Exception):
    pass


def check_library_copy() -> None:
    """Refuse libraries other than a development copy.

    Opening a beets library migrates its database to the installed beets'
    schema, which writes to it. Until M1's read-only guard, S1 only opens
    libraries whose config sets `roquette.library.writable`: the copies
    of `just dev-library` and `scripts/fake-library.py`.
    """
    from beets import config

    writable = config["roquette"]["library"]["writable"]
    if not (writable.exists() and writable.get(bool)):
        raise NotADevelopmentCopy(
            f"{config.config_dir()} isn't a development copy of the beets data; "
            "run with BEETSDIR=dev/beets (`just dev-library`)"
        )


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

    from roquette.library import index
    from roquette.ui import window as ui

    stopwatch.mark("imports done")
    try:
        check_library_copy()
    except NotADevelopmentCopy as error:
        print(f"roquette: {error}", file=sys.stderr)
        return 1

    if args.floor:
        # Python, beets, Slint and GStreamer loaded, an empty window.
        library = index.open_library()
        engine = make_engine(args)
        stopwatch.mark("library and gstreamer loaded")
        empty = ui.load_ui("empty_window").EmptyWindow()
        empty.show()
        stopwatch.mark("window created")

        async def floor() -> None:
            stopwatch.mark("event loop running")

        slint.run_event_loop(floor())
        engine.close()
        del library
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
    # Lives as long as the window: the library is queried on it.
    pool = ThreadPoolExecutor(2, "roquette worker")

    async def main() -> None:
        nonlocal window
        stopwatch.mark("event loop running")
        loop = asyncio.get_running_loop()
        inbox = Inbox(loop)
        window = ui.MainWindow(compiled, options, stopwatch, inbox)
        window.show()
        stopwatch.mark("window created")
        gstreamer = asyncio.ensure_future(inbox.run(pool, lambda: make_engine(args)))

        # beets connections are per thread (the Library object keeps one
        # for each); the index is read on the worker that opened it.
        def load() -> tuple["beets.library.Library", "LibraryIndex"]:
            library = index.open_library()
            return library, index.load_index(library)

        library, library_index = await inbox.run(pool, load)
        window.library = library
        window.worker = pool
        window.set_index(library_index)
        stopwatch.mark(
            f"library loaded ({len(library_index.tracks)} tracks, "
            f"{len(library_index.releases)} releases)"
        )
        window.set_engine(await gstreamer)
        stopwatch.mark("gstreamer loaded")
        if args.play:
            query = args.play
            ids = await inbox.run(pool, lambda: index.query_ids(library, query))
            by_id = {t.id: t for t in library_index.tracks}
            window.play([by_id[i] for i in ids if i in by_id])
        if args.autoscroll:

            def finished() -> None:
                loop.call_later(args.autoscroll_settle, slint.quit_event_loop)

            window.on_autoscroll_finished = finished
            window.start_autoscroll(args.autoscroll, args.autoscroll_speed)

    try:
        slint.run_event_loop(main())
    finally:
        if window is not None:
            window.close()
        pool.shutdown(wait=False, cancel_futures=True)
    return 0
