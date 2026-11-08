#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Write to a beets library while `beet` writes to it (spike S3).

roquette writes play counts and ratings into the beets database while
`beet modify`, `beet mbsync` or `beet update` may be running on it. This
copies the library of `$BEETSDIR` (default dev/beets, the copy of
`just dev-library`) to build/locking/ and runs, for each background job,

- the job: `beet modify` of every item (one transaction, as `beet`
  does it), something like `mbsync` (a short transaction per release
  after a pause for the network), or something like `update` (one
  transaction over the library, writing every twentieth item);
- roquette's side, in this process: a writer thread adding 1 to a
  random item's `play_count` every 100 ms, and a reader thread asking
  for one item every 50 ms, as the UI would;

once with beets' defaults (one `Library` for both threads, beets'
timeout of 5 s) and once with roquette's candidate strategy (writes
through a `Library` of their own, a short timeout and retries with the
lock released in between). Then it checks that no write of either side
was lost. `--wal` puts the copy into SQLite's WAL mode first.

`beet` runs on the copy with no plugins and never writes, moves or
copies files; the music files are only read (and not even that: the
jobs only touch the database). Prints a Markdown summary.
"""

import argparse
import os
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
WORK = REPO / "build" / "locking"


@dataclass
class Side:
    """What roquette's threads saw."""

    write_ms: list[float] = field(default_factory=list)
    read_ms: list[float] = field(default_factory=list)
    failed_writes: int = 0
    failed_reads: int = 0
    retries: int = 0
    expected: Counter[int] = field(default_factory=Counter)
    hung: list[bool] = field(default_factory=list)


def copy_library(source_dir: Path, wal: bool) -> tuple[Path, Path]:
    """The copy of the library and a beets config for it."""
    os.environ["BEETSDIR"] = str(source_dir)
    from beets import config

    from roquette.app import check_library_copy

    check_library_copy()
    source = Path(config["library"].as_filename())
    directory = config["directory"].as_filename()
    shutil.rmtree(WORK, ignore_errors=True)
    beets_dir = WORK / "beets"
    beets_dir.mkdir(parents=True)
    copy = WORK / "library.db"
    live = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    target = sqlite3.connect(copy)
    with target:
        live.backup(target)
    if wal:
        target.execute("PRAGMA journal_mode=WAL")
    target.close()
    live.close()
    (beets_dir / "config.yaml").write_text(
        f"library: {copy}\n"
        f"directory: {directory}\n"
        "plugins: []\n"
        "write: no\n"
        "import: {write: no, copy: no, move: no}\n"
        "roquette: {library: {writable: yes}}\n",
        encoding="utf-8",
    )
    return copy, beets_dir


def job_command(job: str, beets_dir: Path) -> list[str]:
    if job == "modify":
        return [
            sys.executable,
            "-m",
            "beets",
            "modify",
            "--yes",
            "--nowrite",
            "--nomove",
            "comments=s3 modify",
        ]
    return [sys.executable, __file__, "--job", job]


def run_job(job: str) -> None:
    """The `mbsync`- and `update`-like jobs, in a process of their own."""
    import beets.library
    from beets import config

    library = beets.library.Library(
        config["library"].as_filename(), config["directory"].as_filename()
    )
    if job == "mbsync":
        # mbsync: a lookup per release (here 0.2 s instead of
        # MusicBrainz' 1 s), then the release's items stored in one
        # transaction.
        for album in list(library.albums())[:100]:
            time.sleep(0.2)
            with library.transaction():
                for item in album.items():
                    item.comments = "s3 mbsync"
                    item.store()
                album.store()
    elif job == "update":
        # update: one transaction over the library; a stat per file
        # (here 0.5 ms) and every twentieth item changed.
        with library.transaction():
            for n, item in enumerate(library.items()):
                time.sleep(0.0005)
                if n % 20 == 0:
                    item.comments = "s3 update"
                    item.store()


def is_locked(error: Exception) -> bool:
    return isinstance(error, sqlite3.OperationalError) and "locked" in str(error)


def roquette_side(
    read_library: Any,
    write_library: Any,
    ids: list[int],
    retry: bool,
    running: Callable[[], bool],
) -> Side:
    side = Side()
    rng = random.Random(1)

    from roquette.library import writes

    def add_play(item: Any) -> dict[str, int]:
        return {"play_count": int(item.get("play_count", 0) or 0) + 1}

    def increment(item_id: int) -> None:
        if retry:
            writes.store_fields(write_library, item_id, add_play)
            return
        with write_library.transaction():
            item = write_library.get_item(item_id)
            item.update(add_play(item))
            item.store(fields=["play_count"])

    def writer() -> None:
        while running():
            item_id = rng.choice(ids)
            start = time.perf_counter()
            try:
                if retry:
                    side.retries += writes.write_retrying(
                        lambda i=item_id: increment(i), give_up_after=60
                    )
                else:
                    increment(item_id)
            except Exception as error:
                if not is_locked(error):
                    raise
                side.failed_writes += 1
            else:
                side.expected[item_id] += 1
            side.write_ms.append((time.perf_counter() - start) * 1000)
            time.sleep(0.1)

    def reader() -> None:
        while running():
            start = time.perf_counter()
            try:
                read_library.get_item(rng.choice(ids))
            except Exception as error:
                if not is_locked(error):
                    raise
                side.failed_reads += 1
            side.read_ms.append((time.perf_counter() - start) * 1000)
            time.sleep(0.05)

    threads = [
        threading.Thread(target=writer, daemon=True),
        threading.Thread(target=reader, daemon=True),
    ]
    for thread in threads:
        thread.start()
    while running():
        time.sleep(0.1)
    for thread in threads:
        thread.join(timeout=70)
    # A thread waiting for beets' lock forever (see roquette.library.writes).
    side.hung = [t.is_alive() for t in threads]
    return side


def experiment(job: str, strategy: str, source_dir: Path, wal: bool) -> dict[str, Any]:
    copy, beets_dir = copy_library(source_dir, wal)
    os.environ["BEETSDIR"] = str(beets_dir)
    import beets.library
    from beets import config

    config.clear()
    config.read()
    library = beets.library.Library(str(copy), config["directory"].as_filename())
    with library.transaction() as tx:
        ids = [row[0] for row in tx.query("SELECT id FROM items")]
        before = {
            row[0]: int(row[1])
            for row in tx.query(
                "SELECT entity_id, value FROM item_attributes WHERE key='play_count'"
            )
        }
    if strategy == "roquette":
        from roquette.library.writes import WriteLibrary

        write_library = WriteLibrary(str(copy), config["directory"].as_filename())
    else:
        write_library = library

    start = time.perf_counter()
    process = subprocess.Popen(
        job_command(job, beets_dir),
        env={**os.environ, "BEETSDIR": str(beets_dir)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    finished: list[float] = []

    def running() -> bool:
        if process.poll() is None:
            return True
        if not finished:
            finished.append(time.perf_counter() - start)
        # A second after the job, to see the end of the waiting.
        return time.perf_counter() - start < finished[0] + 1

    side = roquette_side(library, write_library, ids, strategy == "roquette", running)
    _, errors = process.communicate()

    # What survived: roquette's play counts and the job's comments, read
    # with a connection of its own, as the library may hang.
    check = sqlite3.connect(copy)
    after = {
        row[0]: int(row[1])
        for row in check.execute(
            "SELECT entity_id, value FROM item_attributes WHERE key='play_count'"
        )
    }
    comments = check.execute(
        "SELECT count(*) FROM items WHERE comments LIKE 's3 %'"
    ).fetchone()[0]
    check.close()
    lost = sum(
        1
        for item_id, count in side.expected.items()
        if after.get(item_id, 0) - before.get(item_id, 0) != count
    )
    expected_comments = {
        "modify": len(ids),
        "update": -(-len(ids) // 20),
        "mbsync": None,
    }[job]

    def quantile(values: list[float], q: float) -> float:
        if len(values) < 2:
            return values[0] if values else 0.0
        return statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1]

    return {
        "job": job,
        "strategy": strategy,
        "journal": "WAL" if wal else "rollback",
        "job s": round(finished[0], 1) if finished else 0.0,
        "job exit": process.returncode,
        "job failed": "locked" if "locked" in errors else (errors.strip()[-60:] or ""),
        "writes": len(side.write_ms),
        "failed writes": side.failed_writes,
        "retries": side.retries,
        "write ms p50/max": f"{quantile(side.write_ms, 50):.0f} / "
        f"{max(side.write_ms, default=0):.0f}",
        "read ms p50/p99/max": f"{quantile(side.read_ms, 50):.1f} / "
        f"{quantile(side.read_ms, 99):.0f} / {max(side.read_ms, default=0):.0f}",
        "failed reads": side.failed_reads,
        "lost play counts": lost,
        "hung": ", ".join(
            name
            for name, hung in zip(("writer", "reader"), side.hung, strict=True)
            if hung
        )
        or "no",
        "job's changes": comments
        if expected_comments is None
        else f"{comments} of {expected_comments}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job", help=argparse.SUPPRESS)
    parser.add_argument("--child", nargs=3, help=argparse.SUPPRESS)
    parser.add_argument("--wal", action="store_true", help="use WAL mode on the copy")
    parser.add_argument(
        "jobs", nargs="*", default=["modify", "mbsync", "update"], metavar="JOB"
    )
    args = parser.parse_args()
    sys.path.insert(0, str(REPO))
    if args.job:
        run_job(args.job)
        return 0
    if args.child:
        job, strategy, source = args.child
        import json

        print(json.dumps(experiment(job, strategy, Path(source), args.wal)), flush=True)
        # Hung threads mustn't keep the process.
        os._exit(0)

    source_dir = Path(os.environ.get("BEETSDIR", REPO / "dev" / "beets")).resolve()
    if not (source_dir / "config.yaml").exists():
        print(f"no beets data in {source_dir}", file=sys.stderr)
        return 1
    import json

    rows = []
    for job in args.jobs:
        for strategy in ("beets", "roquette"):
            print(f"{job}, {strategy}…", file=sys.stderr)
            command = [sys.executable, __file__, "--child", job, strategy]
            command.append(str(source_dir))
            if args.wal:
                command.append("--wal")
            output = subprocess.run(command, capture_output=True, text=True)
            if output.returncode:
                print(output.stderr, file=sys.stderr)
                return 1
            rows.append(json.loads(output.stdout.splitlines()[-1]))
    keys = list(rows[0])
    print(f"BEETSDIR={source_dir}\n")
    print("| " + " | ".join(keys) + " |")
    print("|" + "---|" * len(keys))
    for row in rows:
        print("| " + " | ".join(str(row[k]) for k in keys) + " |")
    shutil.rmtree(WORK, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
