# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The reader and writer workers, the watch on the database."""

import asyncio
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import beets.library

from roquette.library.watch import DatabaseWatch
from roquette.library.workers import Worker
from roquette.threads import Inbox


def run[T](main: Callable[[Inbox], Awaitable[T]]) -> T:
    async def with_inbox() -> T:
        inbox = Inbox(asyncio.get_running_loop())
        try:
            return await main(inbox)
        finally:
            inbox.close()

    return asyncio.run(with_inbox())


# The workers


def test_worker_runs_jobs_on_its_thread(tmp_path: Path) -> None:
    path = tmp_path / "library.db"

    async def main(inbox: Inbox) -> tuple[set[str], int]:
        worker: Worker[beets.library.Library] = Worker("reader", inbox)
        try:
            await worker.start(lambda: beets.library.Library(str(path), str(tmp_path)))
            names = set()
            for _ in range(3):
                names.add(await worker.run(lambda _: threading.current_thread().name))
            assert worker.library is not None
            return names, len(worker.library._connections)
        finally:
            worker.close()

    names, connections = run(main)
    assert len(names) == 1 and next(iter(names)).startswith("roquette reader")
    # One connection, kept: the one made while opening the library.
    assert connections == 1


def test_worker_retries_while_locked(tmp_path: Path) -> None:
    path = tmp_path / "library.db"

    def count(library: beets.library.Library) -> int:
        with library.transaction() as tx:
            return int(tx.query("SELECT count(*) FROM items")[0][0])

    async def main(inbox: Inbox) -> float:
        worker: Worker[beets.library.Library] = Worker("reader", inbox)
        try:
            library = await worker.start(
                lambda: beets.library.Library(str(path), str(tmp_path))
            )
            # The next connection gets a timeout shorter than the lock.
            library.timeout = 0.05
            library._close()
            other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            other.execute("BEGIN EXCLUSIVE")
            threading.Timer(0.3, other.close).start()
            start = time.monotonic()
            assert await worker.run(count) == 0
            return time.monotonic() - start
        finally:
            worker.close()

    assert run(main) >= 0.25


# The watch


def test_watch_reports_writes_once(tmp_path: Path) -> None:
    database = tmp_path / "library.db"
    database.write_bytes(b"")
    reports: list[bool] = []

    async def main(inbox: Inbox) -> None:
        watch = DatabaseWatch(database, reports.append, delay=0.1)
        try:
            (tmp_path / "other.txt").write_text("ignored", encoding="utf-8")
            await asyncio.sleep(0.2)
            assert reports == []
            for _ in range(5):
                with database.open("ab") as file:
                    file.write(b"x")
                (tmp_path / "library.db-journal").write_bytes(b"x")
            await asyncio.sleep(0.3)
            assert reports == [False]
            replacement = tmp_path / "new.db"
            replacement.write_bytes(b"")
            replacement.rename(database)
            await asyncio.sleep(0.3)
            assert reports == [False, True]
        finally:
            watch.close()
            watch.close()

    run(main)
