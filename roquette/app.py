# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Wiring: S1's window on the beets library of `$BEETSDIR`."""

import argparse
import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from roquette.measure import Stopwatch
from roquette.threads import Inbox


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


def run(args: argparse.Namespace) -> int:
    stopwatch = Stopwatch(args.measure)
    import slint

    from roquette.library import index
    from roquette.playback import load_gstreamer
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
        player = load_gstreamer()
        stopwatch.mark("library and gstreamer loaded")
        empty = ui.load_ui("empty_window").EmptyWindow()
        empty.show()
        stopwatch.mark("window created")

        async def floor() -> None:
            stopwatch.mark("event loop running")

        slint.run_event_loop(floor())
        del library, player
        return 0

    compiled = ui.load_ui()
    stopwatch.mark("ui compiled")
    options = ui.WindowOptions(
        module=args.module,
        scale=args.scale,
        color_scheme=args.color_scheme,
        thumbnails=None if args.no_thumbnails else thumbnail_dir(),
        cover_cache_bytes=args.cover_cache << 20,
    )
    window: ui.MainWindow | None = None

    async def main() -> None:
        nonlocal window
        stopwatch.mark("event loop running")
        loop = asyncio.get_running_loop()
        inbox = Inbox(loop)
        window = ui.MainWindow(compiled, options, stopwatch, inbox)
        window.show()
        stopwatch.mark("window created")
        # beets connections are per thread: the library is opened and
        # read on the same worker.
        with ThreadPoolExecutor(2, "roquette worker") as pool:
            gstreamer = asyncio.ensure_future(inbox.run(pool, load_gstreamer))
            library = await inbox.run(
                pool, lambda: index.load_index(index.open_library())
            )
            window.set_index(library)
            stopwatch.mark(
                f"library loaded ({len(library.tracks)} tracks, "
                f"{len(library.releases)} releases)"
            )
            window.player = await gstreamer
            stopwatch.mark("gstreamer loaded")
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
    return 0
