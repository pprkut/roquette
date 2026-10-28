# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Handing results from worker threads to the event loop.

asyncio's `call_soon_threadsafe` (and with it `run_in_executor`) calls
into Slint's native module on the worker thread. That's where pyo3
releases the Python objects Slint dropped while it didn't hold the GIL,
and Slint's models may only be released on the thread that made them
(slint 1.18.1b1: "PyModelBase is unsendable, but is being dropped on
another thread"). Workers therefore never call Slint: they queue the
callback and wake the event loop through a pipe it watches.
"""

import asyncio
import contextlib
import os
import queue
from collections.abc import Callable
from concurrent.futures import Executor
from typing import Any


class Inbox:
    """Callbacks posted from any thread, run on the event loop's."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: queue.SimpleQueue[tuple[Callable[..., Any], tuple[Any, ...]]] = (
            queue.SimpleQueue()
        )
        self._read, self._write = os.pipe()
        os.set_blocking(self._read, False)
        os.set_blocking(self._write, False)
        loop.add_reader(self._read, self._drain)

    def post(self, callback: Callable[..., Any], *args: Any) -> None:
        self._queue.put((callback, args))
        # A full pipe already wakes the loop.
        with contextlib.suppress(BlockingIOError):
            os.write(self._write, b"\0")

    async def run[T](self, executor: Executor, function: Callable[[], T]) -> T:
        """`function` on the executor, like `loop.run_in_executor`."""
        future: asyncio.Future[T] = self._loop.create_future()

        def job() -> None:
            try:
                result = function()
            except BaseException as error:
                self.post(_set_exception, future, error)
            else:
                self.post(_set_result, future, result)

        executor.submit(job)
        return await future

    def close(self) -> None:
        self._loop.remove_reader(self._read)
        os.close(self._read)
        os.close(self._write)

    def _drain(self) -> None:
        with contextlib.suppress(BlockingIOError):
            while os.read(self._read, 4096):
                pass
        while True:
            try:
                callback, args = self._queue.get_nowait()
            except queue.Empty:
                return
            # One failing callback mustn't hold up the ones queued after it.
            try:
                callback(*args)
            except Exception as error:
                self._loop.call_exception_handler(
                    {"message": f"Exception in {callback!r}", "exception": error}
                )


def _set_result[T](future: asyncio.Future[T], result: T) -> None:
    if not future.done():
        future.set_result(result)


def _set_exception(future: asyncio.Future[Any], error: BaseException) -> None:
    if not future.done():
        future.set_exception(error)
