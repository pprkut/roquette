# SPDX-FileCopyrightText: Copyright 2017-2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Command line entry point: `python3 -m roquette`."""

import argparse
from collections.abc import Sequence

from roquette import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="roquette",
        description="Music player for obsessive-compulsive music geeks.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.parse_args(argv)
    print("roquette can't play anything yet; see docs/PLAN.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
