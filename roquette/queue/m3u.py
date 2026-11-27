# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The live playlist saved as an M3U8 file, in `$XDG_STATE_HOME/roquette/`.

The paths are the files' absolute paths, so other players read it.
What only roquette needs goes in a comment line before the path, which
other players ignore:

    #EXTM3U
    #ROQUETTE:item=1234 pinned current
    /mnt/media/Music/Pink Floyd/Wish You Were Here/04 Wish You Were Here.flac

`item` is the beets item's id: a file that has moved since (`beet mv`
while roquette didn't run) is found by it. `pinned` pins the entry to
that version, `current` is the current entry. Paths are bytes in beets;
ones that aren't UTF-8 are kept as they are (surrogate escapes).

The file is written a little after each change, on a thread of its own,
and when roquette closes; atomically, so a crash leaves the previous
one.
"""

import asyncio
import contextlib
import logging
import os
import tempfile
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple, Protocol

from roquette.library.index import PlaylistItem
from roquette.queue.playlist import Entry, Playlist
from roquette.threads import Inbox

log = logging.getLogger(__name__)

HEADER = "#EXTM3U"
TAG = "#ROQUETTE:"

# Seconds after a change until the file is written: changes in a row are
# written once.
SAVE_DELAY = 2.0


class Saved(NamedTuple):
    """An entry as it's saved."""

    path: bytes
    item: int | None = None
    pinned: bool = False
    current: bool = False


def dumps(rows: Iterable[Saved]) -> str:
    lines = [HEADER]
    for row in rows:
        path = os.fsdecode(row.path)
        if "\n" in path or "\r" in path:
            # Can't be written as a line.
            log.warning("not saving %r in the live playlist", path)
            continue
        words = [f"item={row.item}"] if row.item is not None else []
        words += ["pinned"] * row.pinned + ["current"] * row.current
        if words:
            lines.append(TAG + " ".join(words))
        lines.append(path)
    return "\n".join(lines) + "\n"


def parse(text: str, directory: Path) -> list[Saved]:
    """The entries of an M3U file; relative paths are relative to
    `directory`, as M3U has them."""
    rows = []
    words: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(TAG):
            words = line[len(TAG) :].split()
            continue
        if line.startswith("#"):
            continue
        path = Path(line)
        if not path.is_absolute():
            path = directory / path
        item = None
        for word in words:
            if word.startswith("item="):
                with contextlib.suppress(ValueError):
                    item = int(word[5:])
        rows.append(
            Saved(os.fsencode(path), item, "pinned" in words, "current" in words)
        )
        words = []
    return rows


def write(path: Path, rows: Iterable[Saved]) -> None:
    """Write the file atomically; blocking."""
    text = dumps(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    written = Path(temporary)
    try:
        with os.fdopen(
            descriptor, "w", encoding="utf-8", errors="surrogateescape"
        ) as file:
            file.write(text)
        written.replace(path)
    except BaseException:
        written.unlink()
        raise


def read(path: Path) -> list[Saved]:
    """The entries saved; none if there's no file. Blocking."""
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        return []
    return parse(text, path.parent)


def rows(playlist: Playlist) -> list[Saved]:
    """The playlist's entries to save. A current entry that was removed
    isn't in it: what follows it is current then."""
    current = playlist.current
    if current is not None and playlist.detached:
        current = playlist.following(current, manual=True)
    return [Saved(e.item.path, e.item.id, e.pinned, e is current) for e in playlist]


class Library(Protocol):
    async def playlist_items_by_path(
        self, paths: Sequence[bytes]
    ) -> dict[bytes, PlaylistItem]: ...

    async def playlist_items(self, ids: Sequence[int]) -> list[PlaylistItem]: ...


async def entries(
    library: Library, saved: Sequence[Saved]
) -> tuple[list[Entry], int | None]:
    """The entries of a saved playlist in the library, and the position
    of the current one. An entry is its item if that's still the file,
    else the file's item; else its item, the file moved; files beets
    doesn't know are left out."""
    by_path = await library.playlist_items_by_path([s.path for s in saved])
    ids = [s.item for s in saved if s.item is not None]
    by_id = {i.id: i for i in await library.playlist_items(ids)} if ids else {}
    found: list[Entry] = []
    current = None
    for row in saved:
        item = by_id.get(row.item) if row.item is not None else None
        if item is None or item.path != row.path:
            item = by_path.get(row.path, item)
        if item is None:
            continue
        if row.current:
            current = len(found)
        found.append(Entry(item, row.pinned))
    if len(found) < len(saved):
        log.warning(
            "%d tracks of the live playlist aren't in the library",
            len(saved) - len(found),
        )
    return found, current


class PlaylistFile:
    """The live playlist's file: written a little after it changed."""

    def __init__(self, path: Path, inbox: Inbox, delay: float = SAVE_DELAY) -> None:
        self.path = path
        self.delay = delay
        self._inbox = inbox
        self._executor: ThreadPoolExecutor | None = None
        self._playlist: Playlist | None = None
        self._timer: asyncio.TimerHandle | None = None
        # A write was handed to the thread and hasn't finished.
        self._writing = False
        self._closed = False
        # Changed since it was last written (or since the write on its way
        # took its rows).
        self._dirty = False

    async def load(self, library: Library) -> tuple[list[Entry], int | None]:
        saved = await self._inbox.run(self._pool(), lambda: read(self.path))
        return await entries(library, saved)

    def changed(self, playlist: Playlist) -> None:
        """Write `playlist` in a little while."""
        if self._closed:
            return
        self._playlist = playlist
        self._dirty = True
        if self._timer is None:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(self.delay, self._save)

    def _save(self) -> None:
        self._timer = None
        if self._writing:
            # Once it's written, this change is.
            self._timer = asyncio.get_running_loop().call_later(self.delay, self._save)
            return
        playlist = self._playlist
        if playlist is None or not self._dirty:
            return
        saved = rows(playlist)
        self._dirty = False

        def save() -> None:
            try:
                write(self.path, saved)
            except OSError as error:
                log.warning("couldn't save the live playlist: %s", error)
            finally:
                self._inbox.post(self._written)

        # Handed to the thread right away, so that `close` waits for it.
        self._writing = True
        self._pool().submit(save)

    def _written(self) -> None:
        self._writing = False

    def close(self) -> None:
        """Write what changed now; blocking."""
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._executor is not None:
            # A write on its way finishes first.
            self._executor.shutdown(wait=True)
            self._executor = None
        if self._dirty and self._playlist is not None:
            self._dirty = False
            try:
                write(self.path, rows(self._playlist))
            except OSError as error:
                log.warning("couldn't save the live playlist: %s", error)

    def _pool(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(1, thread_name_prefix="roquette state")
        return self._executor
