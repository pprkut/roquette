# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Writing to the beets database while `beet` may be writing too.

Prototype of S3's write strategy (docs/decisions/0003-library.md):

- Writes go through a `Library` of their own (`WriteLibrary`): beets
  lets one transaction run at a time per `Library`, and a write waiting
  for another process' lock would otherwise hold up every read.
- Its transactions always release beets' lock and roll back what failed.
  beets 2.14's `Transaction.__exit__` leaves its lock taken when the
  commit fails ("database is locked"), and the `Library` then hangs at
  its next query.
- They take SQLite's write lock when they begin (`BEGIN IMMEDIATE`).
  Python's `sqlite3` only begins a transaction at the first write, so an
  item read before it could be changed by another process before
  roquette writes it back, and that change would be lost.
- The SQLite timeout is short, and a write that found the database
  locked is tried again later, with the lock released in between, so
  other threads aren't blocked while it waits.
"""

import sqlite3
import time
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any, Self

import beets.library
from beets.dbcore.db import Transaction


class SafeTransaction(Transaction):
    def __enter__(self) -> Self:
        with self.db._tx_stack() as stack:
            root = not stack
        super().__enter__()
        if root:
            try:
                self.db._connection().execute("BEGIN IMMEDIATE")
            except BaseException:
                with self.db._tx_stack() as stack:
                    stack.pop()
                self.db._db_lock.release()
                raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        self.db.revision += self._mutated
        with self.db._tx_stack() as stack:
            assert stack.pop() is self
            root = not stack
        if not root:
            return None
        try:
            if exc_value is None:
                self.db._connection().commit()
            else:
                self.db._connection().rollback()
        except BaseException:
            self.db._connection().rollback()
            raise
        finally:
            self._mutated = False
            self.db._db_lock.release()
        return None


class WriteLibrary(beets.library.Library):
    """The beets library for roquette's writes."""

    def __init__(self, path: str, directory: str, timeout: float = 0.2) -> None:
        super().__init__(path, directory, set_music_dir=False)
        # Connections are made per thread when first used; the writer
        # thread's gets this timeout.
        self.timeout = timeout
        self._close()

    def transaction(self) -> Transaction:
        return SafeTransaction(self)


def is_locked(error: BaseException) -> bool:
    return isinstance(error, sqlite3.OperationalError) and "locked" in str(error)


def write_retrying(
    write: Callable[[], None],
    give_up_after: float = 300,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run `write` (one transaction) until the database isn't locked.

    Returns the number of retries. Raises the last error when the
    database stayed locked for `give_up_after` seconds.
    """
    start = time.monotonic()
    delay = 0.01
    retries = 0
    while True:
        try:
            write()
        except sqlite3.OperationalError as error:
            if not is_locked(error) or time.monotonic() - start > give_up_after:
                raise
            retries += 1
            sleep(delay)
            delay = min(delay * 2, 1.0)
        else:
            return retries


def store_fields(
    library: WriteLibrary, item_id: int, update: Callable[[Any], Mapping[str, Any]]
) -> None:
    """Set fields of one item in one transaction: `update` gets the item
    as it is in the database and returns the new values."""
    with library.transaction():
        item = library.get_item(item_id)
        if item is None:
            return
        values = update(item)
        item.update(values)
        item.store(fields=list(values))
