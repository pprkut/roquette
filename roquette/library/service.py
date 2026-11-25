# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The library as the rest of roquette uses it, from the event loop.

Everything blocking runs on the reader or the writer worker
(`roquette.library.workers`) and is awaited: the config and the plugins
are read on the reader before its library opens, the index is loaded
there with the grouping and the recording index, queries and the text
of the rows on screen are read there. Writes need
`roquette.library.writable`.

Outside changes to the database arrive through the watch
(`roquette.library.watch`); SQLite's `data_version` tells whether
another program committed, rather than roquette's own writes (or a WAL
checkpoint) changing the file: it changes for a connection when any
other connection commits. With a writer, its connection asks, as the
reader's would see the writer's commits too. When another file
replaced the database, the reader and the writer connect again.
"""

import asyncio
import sqlite3
from array import array
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import beets.library

from roquette.library import LibraryError, config, database, grouping, recordings
from roquette.library.index import (
    LibraryIndex,
    Playable,
    ReleaseText,
    TrackText,
    load_index,
    load_playables,
    release_text,
    track_text,
)
from roquette.library.query import Query, Stopped, parse, query_ids
from roquette.library.watch import DatabaseWatch
from roquette.library.workers import Worker
from roquette.library.writes import WriteLibrary, store_fields
from roquette.threads import Inbox


class ReadOnlyError(LibraryError):
    pass


@dataclass(frozen=True, slots=True)
class Collection:
    """The index and what's derived from it, loaded together."""

    index: LibraryIndex
    groups: grouping.Groups
    # Each track of a recording with versions to its versions, best first.
    versions: dict[int, tuple[int, ...]]


def load_collection(library: beets.library.Library) -> Collection:
    index = load_index(library)
    versions = recordings.index_versions(recordings.load_versions(library))
    return Collection(index, grouping.group(index.tracks), versions)


def data_version(library: beets.library.Library) -> int:
    row = library._connection().execute("PRAGMA data_version").fetchone()
    return int(row[0])


def close_connections(library: beets.library.Library) -> None:
    library._close()


class LibraryService:
    def __init__(self, inbox: Inbox, config_file: Path | None = None) -> None:
        self._inbox = inbox
        self._config_file = config_file
        self._reader: Worker[database.ReadLibrary] = Worker("reader", inbox)
        self._writer: Worker[WriteLibrary] | None = None
        self.settings: config.LibraryConfig | None = None
        self.plugins: list[str] = []
        self._watch: DatabaseWatch | None = None
        self._version = 0
        self._checks: set[asyncio.Task[None]] = set()
        self._preloaded: asyncio.Future[Collection] | None = None

    @property
    def writable(self) -> bool:
        return self._writer is not None

    async def open(self) -> config.LibraryConfig:
        """Read the config, load the plugins and open the libraries;
        raises `LibraryError` with a message for the user.

        The index is loaded on the reader as soon as it's open, without
        a round trip through the event loop, which takes a while as the
        window draws its first frames; `collection()` returns it.
        """
        write_library: WriteLibrary | None = None

        def open_libraries() -> database.ReadLibrary:
            nonlocal write_library
            self.settings = config.read_config(self._config_file)
            self.plugins = config.load_plugins()
            # The write library first: opening it updates the database to
            # the installed beets' schema, as any `beet` command would,
            # and the read-only library refuses a database it would
            # update. It makes its connections on the writer's thread.
            write_library = database.open_writer(self.settings)
            library = database.open_reader(self.settings)
            if write_library is None:
                self._version = data_version(library)
            return library

        opening = asyncio.ensure_future(self._reader.start(open_libraries))
        # On the reader's thread right after the opening.
        preloaded = asyncio.ensure_future(self._reader.run(load_collection))
        try:
            await opening
        except BaseException:
            preloaded.cancel()
            raise
        self._preloaded = preloaded
        assert self.settings is not None
        if write_library is not None:
            library = write_library
            writer: Worker[WriteLibrary] = Worker("writer", self._inbox)
            self._writer = writer

            def adopt() -> WriteLibrary:
                self._version = data_version(library)
                return library

            await writer.start(adopt)
        return self.settings

    def close(self) -> None:
        if self._watch is not None:
            self._watch.close()
            self._watch = None
        for task in self._checks:
            task.cancel()
        if self._preloaded is not None:
            self._preloaded.cancel()
            self._preloaded = None
        self._reader.close()
        if self._writer is not None:
            self._writer.close()

    # Reading

    async def collection(self) -> Collection:
        """The index; the first time, the one loaded while opening."""
        preloaded, self._preloaded = self._preloaded, None
        if preloaded is not None:
            return await preloaded
        return await self._reader.run(load_collection)

    async def parse(self, text: str) -> Query:
        return await self._reader.run(lambda library: parse(library, text))

    async def query(
        self, query: Query | str, stop: Callable[[], bool] = lambda: False
    ) -> array[int]:
        """The ids of the tracks the query finds, in its order; raises
        `QueryError` for one beets can't parse, `Stopped` if `stop` said so."""
        try:
            return await self._reader.run(
                lambda library: query_ids(library, query, stop), stop
            )
        except sqlite3.OperationalError:
            # Stopped while the database was locked.
            if stop():
                raise Stopped from None
            raise

    async def track_text(self, ids: Iterable[int]) -> dict[int, TrackText]:
        wanted = list(ids)
        return await self._reader.run(lambda library: track_text(library, wanted))

    async def release_text(self, ids: Iterable[int]) -> dict[int, ReleaseText]:
        wanted = list(ids)
        return await self._reader.run(lambda library: release_text(library, wanted))

    async def playables(self, ids: Sequence[int]) -> list[Playable]:
        wanted = list(ids)
        return await self._reader.run(lambda library: load_playables(library, wanted))

    # Writing

    async def store(
        self, item_id: int, update: Callable[[Any], Mapping[str, Any]]
    ) -> None:
        """Set fields of an item in one transaction: `update` gets the
        item as it is in the database (on the writer's thread) and
        returns the new values. Waits while `beet` holds the lock."""
        if self._writer is None:
            raise ReadOnlyError(
                "roquette doesn't write to this beets library;"
                " set roquette.library.writable in the beets config to allow it"
            )
        await self._writer.run(lambda library: store_fields(library, item_id, update))

    # Outside changes

    def watch(self, changed: Callable[[], None], delay: float = 1.0) -> None:
        """Call `changed` when another program changed the database."""
        assert self.settings is not None

        def check(replaced: bool) -> None:
            task = asyncio.get_running_loop().create_task(
                self._check(changed, replaced)
            )
            self._checks.add(task)
            task.add_done_callback(self._checks.discard)

        self._watch = DatabaseWatch(self.settings.database, check, delay)

    async def _check(self, changed: Callable[[], None], replaced: bool) -> None:
        if replaced:
            # Connections opened before still read the old file: beets
            # opens new ones as they're needed.
            await self._reader.run(close_connections)
            if self._writer is not None:
                await self._writer.run(close_connections)
        version = await self._changes(data_version)
        if replaced or version != self._version:
            self._version = version
            changed()

    async def _changes[T](self, job: Callable[[beets.library.Library], T]) -> T:
        # The writer's connection sees others' commits but not its own.
        if self._writer is not None:
            return await self._writer.run(job)
        return await self._reader.run(job)
