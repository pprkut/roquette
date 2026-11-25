# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The text of the rows on screen, read by id when a view asks for it.

The index holds numbers only (docs/decisions/0003-library.md); a view
asks for the text of each row it shows. The reader worker is never
waited for on the event loop: a row without its text yet is shown
empty, the ids asked for in one turn of the event loop are read
together, and the view is told which ids arrived so it updates those
rows. Text is kept for the most recently asked-for ids only, a few
screens' worth. An id the reader doesn't find (a track deleted by
another program, until the library reloads) isn't asked for again
before `clear()`. When the library changed, `refresh()` reads the text
again, and shows what it had until the new text arrives, so that the
views don't flash empty.

Reading only the rows shown took a read per frame while scrolling, a
round trip to the reader whose GIL hand-overs cost the frame rate (44
instead of 60 fps on the desktop) and showed the rows coming into view
empty. `Ahead` reads the text in blocks of rows instead: the block of a
row shown and the blocks on either side, so that the rows have their
text before they come into view.
"""

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable

type Load[T] = Callable[[list[int]], Awaitable[dict[int, T]]]


class RowText[T]:
    def __init__(
        self,
        load: Load[T],
        loaded: Callable[[list[int]], None],
        limit: int = 2048,
    ) -> None:
        self._load = load
        self._loaded = loaded
        self._limit = limit
        self._text: OrderedDict[int, T] = OrderedDict()
        self._wanted: set[int] = set()
        self._loading: set[int] = set()
        self._missing: set[int] = set()
        # Text from before the library changed, shown until it's read again.
        self._stale: set[int] = set()
        self._flush: asyncio.Handle | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        # Text read before a `clear()` is dropped when it arrives.
        self._generation = 0
        # How many reads, for the measurements.
        self.reads = 0

    def __len__(self) -> int:
        return len(self._text)

    def get(self, item_id: int) -> T | None:
        """The text, or None while it's read. Text from before a
        `refresh()` is returned while it's read again (see `is_stale`)."""
        text = self._text.get(item_id)
        if text is not None:
            self._text.move_to_end(item_id)
            if item_id not in self._stale or item_id in self._loading:
                return text
        elif item_id in self._loading or item_id in self._missing:
            return None
        self._wanted.add(item_id)
        if self._flush is None:
            self._flush = asyncio.get_running_loop().call_soon(self._read)
        return text

    def is_stale(self, item_id: int) -> bool:
        """Whether the text is from before the library changed, and so
        will arrive again."""
        return item_id in self._stale

    def want(self, ids: Iterable[int]) -> None:
        """Read the text of `ids` ahead, unless it's there or being read."""
        text = self._text
        wanted = self._wanted
        stale = self._stale
        for item_id in ids:
            if item_id in text:
                # Kept as if shown, so it isn't dropped before it is.
                text.move_to_end(item_id)
                if item_id not in stale:
                    continue
            if item_id not in self._loading and item_id not in self._missing:
                wanted.add(item_id)
        if wanted and self._flush is None:
            self._flush = asyncio.get_running_loop().call_soon(self._read)

    def clear(self) -> None:
        """Forget all text."""
        self._generation += 1
        self._text.clear()
        self._stale.clear()
        self._wanted.clear()
        self._loading.clear()
        self._missing.clear()

    def refresh(self) -> None:
        """The library changed: read the text again when it's asked for,
        and return the text from before until then."""
        self._generation += 1
        self._stale = set(self._text)
        self._wanted.clear()
        self._loading.clear()
        self._missing.clear()

    def close(self) -> None:
        self.clear()
        if self._flush is not None:
            self._flush.cancel()
            self._flush = None
        for task in self._tasks:
            task.cancel()

    def _read(self) -> None:
        self._flush = None
        if not self._wanted:
            return
        ids = sorted(self._wanted)
        self._wanted.clear()
        self._loading.update(ids)
        task = asyncio.get_running_loop().create_task(
            self._read_ids(ids, self._generation)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read_ids(self, ids: list[int], generation: int) -> None:
        self.reads += 1
        try:
            found = await self._load(ids)
        finally:
            if generation == self._generation:
                self._loading.difference_update(ids)
        if generation != self._generation:
            return
        self._store(found.items())
        for item_id in ids:
            if item_id not in found:
                self._missing.add(item_id)
                # Deleted meanwhile: its text from before is no use.
                self._text.pop(item_id, None)
                self._stale.discard(item_id)
        self._loaded([i for i in ids if i in found])

    def _store(self, found: Iterable[tuple[int, T]]) -> None:
        for item_id, text in found:
            self._text[item_id] = text
            self._stale.discard(item_id)
        while len(self._text) > self._limit:
            dropped, _ = self._text.popitem(last=False)
            self._stale.discard(dropped)


# Rows read together: half a screen of the track list. Larger blocks
# mean fewer reads, but each takes the GIL from the main thread for
# longer (Python's sqlite3 hands it over for every row): with 128 rows,
# scrolling ten times the library fell from 55 to 44 fps.
BLOCK = 32


class Ahead[T]:
    """Reads the text of the rows around the ones a view shows, a block
    at a time; the view's rows are given by `id_of`."""

    def __init__(self, text: RowText[T], block: int = BLOCK) -> None:
        self._text = text
        self._block_size = block
        self._block = -1

    def shown(self, row: int, count: int, id_of: Callable[[int], int]) -> None:
        """Row `row` of `count` is shown."""
        block = row // self._block_size
        if block == self._block:
            return
        self._block = block
        start = max(0, (block - 1) * self._block_size)
        stop = min(count, (block + 2) * self._block_size)
        self._text.want(id_of(r) for r in range(start, stop))

    def reset(self) -> None:
        """The rows changed."""
        self._block = -1
