# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The reader and the writer: a thread each, with a library each.

beets makes a connection per thread and keeps it until the library is
closed, and lets one transaction run at a time per library; reads in
parallel are much slower than one after the other (S3). So all reads
run on one worker thread with the read-only library, all writes on
another with the write library, each keeping its connection for the
life of the process. Jobs run one after the other, and are tried
again while the database is locked (`writes.retrying`). The event loop
awaits them without blocking: results come back through the inbox.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import beets.library

from roquette.library.writes import retrying
from roquette.threads import Inbox


class Worker[L: beets.library.Library]:
    def __init__(self, name: str, inbox: Inbox) -> None:
        self._inbox = inbox
        self._executor = ThreadPoolExecutor(1, thread_name_prefix=f"roquette {name}")
        self._library: L | None = None

    @property
    def library(self) -> L | None:
        return self._library

    async def start(self, open_library: Callable[[], L]) -> L:
        """Open the worker's library on its thread, where the connection
        made while opening it stays."""

        def opened() -> L:
            self._library = open_library()
            return self._library

        return await self._inbox.run(self._executor, opened)

    async def run[T](self, job: Callable[[L], T]) -> T:
        """`job` with the worker's library, on its thread."""

        def call() -> T:
            library = self._library
            if library is None:
                raise RuntimeError("the worker's library isn't open")
            return retrying(lambda: job(library))

        return await self._inbox.run(self._executor, call)

    async def call[T](self, job: Callable[[], T]) -> T:
        """`job` on the worker's thread, without the library."""
        return await self._inbox.run(self._executor, job)

    def close(self) -> None:
        """Stop the thread after its current job; queued ones are dropped."""
        self._executor.shutdown(wait=False, cancel_futures=True)
