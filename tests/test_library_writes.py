# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Writing while `beet` writes (S3), and beets bugs worked around."""

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import beets.library
import pytest

from roquette.library import writes


def item(music: Path, name: str, **fields: Any) -> beets.library.Item:
    return beets.library.Item(title=name, path=os.fsencode(music / name), **fields)


def make_library(tmp_path: Path) -> tuple[Path, int]:
    path = tmp_path / "library.db"
    library = beets.library.Library(str(path), str(tmp_path))
    item_id = library.add(item(tmp_path, "t"))
    assert item_id is not None
    library._close()
    return path, item_id


def hold_shared_lock(path: Path) -> sqlite3.Connection:
    """Another process' read transaction: no commit while it lasts."""
    other = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    other.execute("BEGIN")
    other.execute("SELECT * FROM items").fetchall()
    return other


def test_beets_keeps_its_lock_when_a_commit_fails(tmp_path: Path) -> None:
    """The beets bug the write library works around (beets 2.14)."""
    path, item_id = make_library(tmp_path)
    library = beets.library.Library(str(path), str(tmp_path))
    library.timeout = 0.05
    library._close()
    other = hold_shared_lock(path)
    stored = library.get_item(item_id)
    assert stored is not None
    stored["play_count"] = 1
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        stored.store()
    other.close()
    if not library._db_lock.locked():
        pytest.fail("fixed in beets: SafeTransaction may go")
    library._db_lock.release()


def test_write_library_releases_its_lock(tmp_path: Path) -> None:
    path, item_id = make_library(tmp_path)
    library = writes.WriteLibrary(str(path), str(tmp_path), timeout=0.05)
    other = hold_shared_lock(path)

    def add_play(item: beets.library.Item) -> dict[str, int]:
        return {"play_count": int(item.get("play_count", 0)) + 1}

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        writes.store_fields(library, item_id, add_play)
    assert not library._db_lock.locked()
    other.close()

    writes.store_fields(library, item_id, add_play)
    stored = library.get_item(item_id)
    assert stored is not None
    # The failed write was rolled back, not committed later.
    assert int(stored["play_count"]) == 1


def test_write_library_keeps_outside_changes(tmp_path: Path) -> None:
    """Another process' write between roquette's read of an item and its
    write waits for roquette's transaction instead of being overwritten."""
    path, item_id = make_library(tmp_path)
    library = writes.WriteLibrary(str(path), str(tmp_path))
    writes.store_fields(library, item_id, lambda item: {"play_count": 1})
    outside_done = threading.Event()

    def add_ten() -> None:
        other = sqlite3.connect(path, timeout=5)
        with other:
            other.execute(
                "UPDATE item_attributes SET value = value + 10"
                " WHERE entity_id = ? AND key = 'play_count'",
                (item_id,),
            )
        other.close()
        outside_done.set()

    outside = threading.Thread(target=add_ten)

    def add_play(item: beets.library.Item) -> dict[str, int]:
        outside.start()
        # Without the write lock, the outside write commits meanwhile.
        outside_done.wait(0.3)
        return {"play_count": int(item["play_count"]) + 1}

    writes.store_fields(library, item_id, add_play)
    outside.join()
    stored = library.get_item(item_id)
    assert stored is not None
    assert int(stored["play_count"]) == 12


def test_write_library_releases_its_lock_when_it_cannot_begin(
    tmp_path: Path,
) -> None:
    path, item_id = make_library(tmp_path)
    library = writes.WriteLibrary(str(path), str(tmp_path), timeout=0.05)
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        writes.store_fields(library, item_id, lambda item: {"play_count": 1})
    assert not library._db_lock.locked()
    other.close()
    writes.store_fields(library, item_id, lambda item: {"play_count": 1})


def test_write_retrying(tmp_path: Path) -> None:
    path, item_id = make_library(tmp_path)
    library = writes.WriteLibrary(str(path), str(tmp_path), timeout=0.01)
    other = hold_shared_lock(path)
    released = threading.Timer(0.2, other.close)
    released.start()

    def write() -> None:
        writes.store_fields(library, item_id, lambda item: {"play_count": 5})

    assert writes.write_retrying(write) > 0
    released.join()
    stored = library.get_item(item_id)
    assert stored is not None
    assert int(stored["play_count"]) == 5


def test_write_retrying_gives_up() -> None:
    def locked() -> None:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        writes.write_retrying(locked, give_up_after=0, sleep=lambda _: None)


def test_playlist_query_misses_relative_paths(tmp_path: Path) -> None:
    """The beets `playlist` plugin's query (beets 2.14) compares the M3U's
    paths, made absolute, with the paths beets stores relative to the
    music directory, so `playlist:name` finds nothing (S3, for M13)."""
    from beets import config
    from beetsplug.playlist import PlaylistQuery

    music = tmp_path / "music"
    library = beets.library.Library(str(tmp_path / "library.db"), str(music))
    library.add(item(music, "One.flac"))
    (tmp_path / "list.m3u").write_text("#EXTM3U\nOne.flac\n", encoding="utf-8")
    config["playlist"]["playlist_dir"] = str(tmp_path)
    config["playlist"]["relative_to"] = "library"
    config["directory"] = str(music)
    try:
        with library.music_dir_context():
            query = PlaylistQuery("playlist", "list", True)
            found = list(library.items(query))
        # The comment line isn't skipped either (bytes compared to "#").
        assert len(query.pattern) == 2
    finally:
        config.clear()
        config.read(user=False)
    if found:
        pytest.fail("fixed in beets: `playlist:` queries work again")


def test_write_library_updates_an_outdated_database(tmp_path: Path) -> None:
    """beets backs the database up before updating it, from within a
    transaction; with the write lock taken, the backup never finished."""
    path, item_id = make_library(tmp_path)
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("DELETE FROM migrations WHERE name = 'relative_path'")
    connection.close()
    opened: list[writes.WriteLibrary] = []
    thread = threading.Thread(
        target=lambda: opened.append(writes.WriteLibrary(str(path), str(tmp_path))),
        daemon=True,
    )
    thread.start()
    thread.join(10)
    assert opened, "opening the library hung"
    writes.store_fields(opened[0], item_id, lambda item: {"play_count": 1})
    assert list(tmp_path.glob("library.db-before-*.bak"))
