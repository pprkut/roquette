# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Incremental search: a beets query run as the user types.

One query runs at a time (on the reader worker, docs/decisions/
0003-library.md). Text typed meanwhile waits, and only the latest of it
runs: typing "pink floyd" while the reader is busy runs "pink floyd",
not every prefix of it. A query that runs in Python (in chunks of
Items) is stopped as soon as newer text arrives; queries in SQL take
under 20 ms and finish. A query that doesn't parse, as one being typed
often doesn't, reports its error and keeps the last results.
"""

import asyncio
from collections.abc import Awaitable, Callable

from roquette.library.query import QueryError, Stopped

# Runs a query: the text, and a function that says whether to stop.
type Run[T] = Callable[[str, Callable[[], bool]], Awaitable[T]]


class Search[T]:
    def __init__(
        self,
        run: Run[T],
        found: Callable[[str, T], None],
        failed: Callable[[str, str], None] = lambda text, message: None,
    ) -> None:
        self._run = run
        self._found = found
        self._failed = failed
        # The text wanted, and the text of the results (or error) shown.
        self.text: str | None = None
        self._shown: str | None = None
        self._task: asyncio.Task[None] | None = None
        # How many queries ran, for the tests.
        self.runs = 0

    def set_text(self, text: str) -> None:
        self.text = " ".join(text.split())
        if self._task is None and self.text != self._shown:
            self._task = asyncio.get_running_loop().create_task(self._search())

    async def wait(self) -> None:
        """Until the latest text's query is done."""
        while self._task is not None:
            await asyncio.shield(self._task)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._shown = None

    def _superseded(self, text: str) -> Callable[[], bool]:
        return lambda: self.text != text

    async def _search(self) -> None:
        try:
            while self.text is not None and self.text != self._shown:
                text = self.text
                self.runs += 1
                try:
                    result = await self._run(text, self._superseded(text))
                except Stopped:
                    continue
                except QueryError as error:
                    self._shown = text
                    if self.text == text:
                        self._failed(text, str(error))
                    continue
                self._shown = text
                if self.text == text:
                    self._found(text, result)
        finally:
            self._task = None
