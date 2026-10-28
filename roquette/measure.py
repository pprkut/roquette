# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Timings for `--measure` (scripts/measure.sh)."""

import os
import sys
import time


class Stopwatch:
    """Prints "roquette: <event> after <n> ms" to stderr.

    The time counts from `$ROQUETTE_T0` (seconds since the epoch, set by
    scripts/measure.sh right before starting roquette, so the
    interpreter's startup is included), or else from the stopwatch's
    creation.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._start_ns = time.time_ns()
        t0 = os.environ.get("ROQUETTE_T0")
        if t0:
            self._start_ns = int(float(t0) * 1e9)

    def mark(self, event: str) -> None:
        if self.enabled:
            ms = (time.time_ns() - self._start_ns) / 1e6
            print(f"roquette: {event} after {ms:.1f} ms", file=sys.stderr, flush=True)

    def note(self, message: str) -> None:
        if self.enabled:
            print(f"roquette: {message}", file=sys.stderr, flush=True)
