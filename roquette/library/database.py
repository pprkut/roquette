# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The libraries roquette reads and writes through.

Reads go through a `ReadLibrary`, whose connections SQLite opens
read-only (`mode=ro`): nothing can write through it, whatever the
settings. Writes go through a `WriteLibrary` of their own
(`roquette.library.writes`), which roquette only makes when the config
sets `roquette.library.writable`; that's the read-only guard
(docs/PLAN.md, "Development data: read-only rule").

beets updates a database to its schema when it opens one, which
writes. A read-only library refuses a database beets would update,
asking for a `beet` command to do it, before writing anything (beets'
backups before its migrations included).
"""

import os
import sqlite3
import sys
from pathlib import Path

import beets
import beets.library
from beets.dbcore.db import DBAccessError, Transaction

from roquette.library import LibraryError
from roquette.library.config import LibraryConfig
from roquette.library.writes import SafeTransaction, WriteLibrary


class _Outdated(Exception):
    pass


class ReadTransaction(SafeTransaction):
    # A transaction's reads see one state of the database (Python's
    # sqlite3 doesn't begin one for reads).
    begin = "BEGIN"


class ReadLibrary(beets.library.Library):
    """The beets library for roquette's reads, opened read-only.

    It doesn't set the process' music directory: paths are read within
    `music_dir_context()`.
    """

    def __init__(self, path: Path, directory: Path) -> None:
        if not path.is_file():
            raise LibraryError(f"there's no beets library at {path}")
        try:
            super().__init__(str(path), str(directory), set_music_dir=False)
        except _Outdated:
            raise LibraryError(_outdated(path)) from None
        except (sqlite3.OperationalError, DBAccessError) as error:
            if "readonly" in str(error):
                raise LibraryError(_outdated(path)) from None
            raise LibraryError(f"the beets library {path}: {error}") from error
        except sqlite3.DatabaseError as error:
            raise LibraryError(f"the beets library {path}: {error}") from error

    def transaction(self) -> Transaction:
        return ReadTransaction(self)

    def _create_connection(self) -> sqlite3.Connection:
        # As beets makes them (beets.dbcore.db.Database), read-only.
        conn = sqlite3.connect(
            f"{self.path.absolute().as_uri()}?mode=ro",
            uri=True,
            timeout=self.timeout,
            check_same_thread=False,
        )
        if sys.version_info >= (3, 12) and sqlite3.sqlite_version_info >= (3, 29, 0):
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DDL, False)
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
        self.add_functions(conn)
        if self.supports_extensions:
            conn.enable_load_extension(True)
            for extension in self._extensions:
                conn.load_extension(extension)
        conn.row_factory = sqlite3.Row
        return conn

    def _migrate(self) -> None:
        # beets backs the database up before each migration, next to it.
        for migration, models in self._migrations:
            for model in models:
                if not self.migration_exists(migration.name, model._table):
                    raise _Outdated


def _outdated(path: Path) -> str:
    return (
        f"the beets library {path} isn't up to date with beets {beets.__version__};"
        " run any `beet` command (e.g. `beet stats`) to update it"
    )


def open_reader(settings: LibraryConfig) -> ReadLibrary:
    """The library for reads; its connection stays with the calling thread."""
    return ReadLibrary(settings.database, settings.directory)


def open_writer(settings: LibraryConfig) -> WriteLibrary | None:
    """The library for writes, or None unless the settings allow them.

    Opening it updates the database to the installed beets' schema, as
    any `beet` command would.
    """
    if not settings.writable:
        return None
    if not settings.database.is_file():
        raise LibraryError(f"there's no beets library at {settings.database}")
    try:
        return WriteLibrary(os.fspath(settings.database), str(settings.directory))
    except sqlite3.DatabaseError as error:
        raise LibraryError(f"the beets library {settings.database}: {error}") from error
