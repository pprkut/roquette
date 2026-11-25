#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Measure the playback engine without the window.

Plays generated FLAC files (CD quality, gapless, a transition every few
seconds) in real time through an audio sink and samples, after it has
settled: the CPU time of the process, the wake-ups (voluntary and
involuntary context switches) of every thread and of the main thread,
RSS and PSS. The wake-ups are also listed per thread, by the names
GStreamer gives its threads. Prints a Markdown summary.

    scripts/measure-playback.py [--sink SINK] [--buffer MS] [--latency MS]
        [--secs N] [--settle N] [--runs N]

The default sink is autoaudiosink's choice; where there's no sound card,
"fakesink sync=true". The files are made once, in build/measure-playback.
"""

import argparse
import asyncio
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from roquette.playback.engine import Engine  # noqa: E402
from roquette.playback.gain import Mode, ReplayGain  # noqa: E402
from roquette.playback.source import Source, SourceList  # noqa: E402

FILES = REPO / "build" / "measure-playback"
TRACK_SECS = 7


def make_tracks(count: int) -> list[Path]:
    """`count` tracks of pink noise, TRACK_SECS long each."""
    FILES.mkdir(parents=True, exist_ok=True)
    paths = []
    for n in range(count):
        path = FILES / f"{n:02}.flac"
        if not path.exists():
            buffers = TRACK_SECS * 44100 // 1024
            pipeline: Any = Gst.parse_launch(
                f"audiotestsrc wave=pink-noise num-buffers={buffers}"
                " samplesperbuffer=1024 volume=0.3"
                " ! audio/x-raw,format=S16LE,rate=44100,channels=2"
                f" ! flacenc ! filesink location={path}"
            )
            pipeline.set_state(Gst.State.PLAYING)
            pipeline.get_bus().timed_pop_filtered(
                Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR
            )
            pipeline.set_state(Gst.State.NULL)
        paths.append(path)
    return paths


def threads() -> dict[str, tuple[str, int]]:
    """Each thread's name and context switches, by its id."""
    found = {}
    for task in Path("/proc/self/task").iterdir():
        try:
            name = (task / "comm").read_text().strip()
            status = (task / "status").read_text()
        except OSError:
            continue
        switches = sum(
            int(n) for n in re.findall(r"^(?:non)?voluntary_ctxt_switches:\s+(\d+)",
                                       status, re.M)
        )  # fmt: skip
        found[task.name] = (name, switches)
    return found


def memory() -> tuple[int, int]:
    """RSS and PSS in KiB."""
    fields = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        name, _, value = line.partition(":")
        fields[name] = value.split()[0] if value.split() else ""
    return int(fields["Rss"]), int(fields["Pss"])


def cpu_seconds() -> float:
    times = os.times()
    return times.user + times.system


async def measure(args: argparse.Namespace, paths: list[Path]) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    engine = Engine(
        args.sink,
        buffer_time=args.buffer / 1000,
        latency_time=args.latency / 1000,
    )
    engine.attach(loop)
    sources = tuple(
        Source(path, ReplayGain(-6.0, 0.5, -6.0, 0.5), Mode.ALBUM) for path in paths
    )
    engine.upcoming = SourceList(sources)
    errors: list[str] = []
    engine.on_error = lambda error: errors.append(error.message)
    engine.play(sources[0])
    await asyncio.sleep(args.settle)
    main = str(os.getpid())
    before, cpu_before, start = threads(), cpu_seconds(), time.monotonic()
    await asyncio.sleep(args.secs)
    after, cpu_after, end = threads(), cpu_seconds(), time.monotonic()
    rss, pss = memory()
    sink = engine.sink_name()
    engine.close()
    if errors:
        raise SystemExit(f"playback failed: {errors}")
    elapsed = end - start
    per_thread: Counter[str] = Counter()
    for tid, (name, switches) in after.items():
        per_thread[name] += switches - before.get(tid, (name, 0))[1]
    return {
        "cpu": 100 * (cpu_after - cpu_before) / elapsed,
        "all": sum(per_thread.values()) / elapsed,
        "main": (after[main][1] - before[main][1]) / elapsed,
        "threads": len(after),
        "rss": rss / 1024,
        "pss": pss / 1024,
        "sink": sink,
        "per_thread": {name: n / elapsed for name, n in per_thread.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sink", help="the audio sink (default: autoaudiosink's)")
    parser.add_argument("--buffer", type=float, default=400, metavar="MS")
    parser.add_argument("--latency", type=float, default=100, metavar="MS")
    parser.add_argument("--secs", type=float, default=30)
    parser.add_argument("--settle", type=float, default=5)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    Gst.init([])
    count = int((args.settle + args.secs) // TRACK_SECS) + 2
    paths = make_tracks(count)
    results = [asyncio.run(measure(args, paths)) for _ in range(args.runs)]
    print(
        f"Sink {results[0]['sink']} ({args.sink or 'autoaudiosink'}), buffer"
        f" {args.buffer:g} ms, segments {args.latency:g} ms; {args.secs:g} s"
        f" after {args.settle:g} s, FLAC 44.1 kHz with a transition every"
        f" {TRACK_SECS} s."
    )
    print()
    print("| run | CPU % | wake-ups/s (all / main) | RSS MiB | PSS MiB | threads |")
    print("|---|---|---|---|---|---|")
    for n, r in enumerate(results, 1):
        print(
            f"| {n} | {r['cpu']:.1f} | {r['all']:.1f} / {r['main']:.1f}"
            f" | {r['rss']:.1f} | {r['pss']:.1f} | {r['threads']} |"
        )
    print()
    print("Wake-ups per second by thread (run 1):")
    print()
    for name, rate in sorted(results[0]["per_thread"].items(), key=lambda x: -x[1]):
        if rate >= 0.5:
            print(f"- {name}: {rate:.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
