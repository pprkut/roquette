# SPDX-FileCopyrightText: Copyright 2017-2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Command line entry point: `python3 -m roquette`."""

import argparse
from collections.abc import Sequence

from roquette import __version__


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="roquette",
        description="Music player for obsessive-compulsive music geeks.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--module",
        choices=["tracks", "covers"],
        default="tracks",
        help="the module shown first",
    )
    parser.add_argument(
        "--color-scheme",
        choices=["light", "dark"],
        help="use this colour scheme instead of the desktop's",
    )
    parser.add_argument(
        "--no-thumbnails",
        action="store_true",
        help="decode every cover instead of using the shared thumbnail cache",
    )
    parser.add_argument(
        "--cover-cache",
        type=int,
        default=32,
        metavar="MIB",
        help="memory for decoded covers (default: %(default)s MiB)",
    )
    measuring = parser.add_argument_group("measuring (scripts/measure.sh)")
    measuring.add_argument(
        "--measure", action="store_true", help="print startup timings to stderr"
    )
    measuring.add_argument(
        "--floor",
        action="store_true",
        help="only an empty window, with beets, Slint and GStreamer loaded",
    )
    measuring.add_argument(
        "--scale",
        type=int,
        default=1,
        metavar="N",
        help="show the library N times over, as a larger one",
    )
    measuring.add_argument(
        "--autoscroll",
        choices=["tracks", "covers"],
        help="scroll the module from top to bottom, then quit",
    )
    measuring.add_argument(
        "--autoscroll-speed",
        type=float,
        default=8000,
        metavar="PX",
        help="logical pixels per second (default: %(default)s)",
    )
    measuring.add_argument(
        "--autoscroll-settle",
        type=float,
        default=3,
        metavar="SECONDS",
        help="time to wait after the scroll before quitting (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from roquette import app

    return app.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
