# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""A watch on the beets database for changes made outside roquette.

`beet` (an import, `mbsync`, `modify`) changes the database while
roquette runs. Without polling (docs/PLAN.md, "Design rules"), the
kernel tells: inotify on the database's directory, for the database
file and the journals SQLite writes beside it (`-journal`, `-wal`).
Writes come in bursts (an import commits many times), so a change is
reported once `delay` seconds after the first write, and further
writes in that time are part of the same change. Whether it was a
change at all, rather than roquette's own write or a WAL checkpoint, is
for the caller to find out (`PRAGMA data_version`, see
`roquette.library.service`).

Linux only, as roquette is; through libc, without a dependency.
"""

import asyncio
import ctypes
import ctypes.util
import os
import struct
from collections.abc import Callable
from pathlib import Path

IN_MODIFY = 0x00000002
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_NONBLOCK = os.O_NONBLOCK
IN_CLOEXEC = os.O_CLOEXEC

_EVENT = struct.Struct("iIII")
_MASK = IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


class DatabaseWatch:
    """Calls `changed(replaced)` on the event loop after the database
    changed on disk; `replaced` if the file was replaced rather than
    written (moved or created over), which connections opened before
    don't see."""

    def __init__(
        self,
        database: Path,
        changed: Callable[[bool], None],
        delay: float = 1.0,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._names = {
            os.fsencode(database.name + suffix) for suffix in ("", "-journal", "-wal")
        }
        self._database = os.fsencode(database.name)
        self._changed = changed
        self._delay = delay
        self._pending: asyncio.TimerHandle | None = None
        self._replaced = False
        libc = _libc()
        self._fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self._fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        directory = os.fsencode(database.parent)
        if libc.inotify_add_watch(self._fd, directory, _MASK) < 0:
            error = ctypes.get_errno()
            os.close(self._fd)
            raise OSError(error, os.strerror(error), os.fsdecode(directory))
        self._loop.add_reader(self._fd, self._read)

    def close(self) -> None:
        if self._fd < 0:
            return
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        self._loop.remove_reader(self._fd)
        os.close(self._fd)
        self._fd = -1

    def _read(self) -> None:
        try:
            data = os.read(self._fd, 64 * 1024)
        except BlockingIOError:
            return
        offset = 0
        relevant = False
        while offset + _EVENT.size <= len(data):
            _, mask, _, length = _EVENT.unpack_from(data, offset)
            offset += _EVENT.size
            name = data[offset : offset + length].rstrip(b"\0")
            offset += length
            if name not in self._names:
                continue
            relevant = True
            if name == self._database and mask & (IN_MOVED_TO | IN_CREATE):
                self._replaced = True
        if relevant and self._pending is None:
            self._pending = self._loop.call_later(self._delay, self._report)

    def _report(self) -> None:
        self._pending = None
        replaced, self._replaced = self._replaced, False
        self._changed(replaced)
