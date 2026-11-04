# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""S3's prototypes: the recording index, the grouping, the writes."""

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

import beets.library
import pytest

from roquette.library import grouping, recordings, writes
from roquette.library.index import Track, load_index


def item(music: Path, name: str, **fields: Any) -> beets.library.Item:
    return beets.library.Item(title=name, path=os.fsencode(music / name), **fields)


def test_versions_best_first(tmp_path: Path) -> None:
    library = beets.library.Library(str(tmp_path / "library.db"), str(tmp_path))
    versions = [
        ("mp3", "MP3", 0, 44100, 320_000, 200.0),
        ("cd", "FLAC", 16, 44100, 900_000, 200.0),
        ("hires", "FLAC", 24, 96000, 2_800_000, 199.0),
        ("cd-longer", "FLAC", 16, 44100, 900_000, 203.0),
    ]
    for name, fmt, bitdepth, samplerate, bitrate, length in versions:
        library.add(
            item(
                tmp_path,
                name,
                mb_trackid="rec",
                format=fmt,
                bitdepth=bitdepth,
                samplerate=samplerate,
                bitrate=bitrate,
                length=length,
            )
        )
    library.add(item(tmp_path, "alone", mb_trackid="other"))
    library.add(item(tmp_path, "no id 1"))
    library.add(item(tmp_path, "no id 2"))

    index = recordings.index_versions(recordings.load_versions(library))
    titles = {i.id: i.title for i in library.items()}
    best_first = [titles[i] for i in index[next(iter(index))]]
    assert best_first == ["hires", "cd-longer", "cd", "mp3"]
    # Only recordings with versions; tracks without an id are on their own.
    assert len(index) == 4
    assert len({id(v) for v in index.values()}) == 1


def track(id: int, album_id: int, disc: int, albumartist: str = "A") -> Track:
    return Track(id, album_id, disc, id, f"t{id}", albumartist, "", albumartist, 1.0)


def test_grouping() -> None:
    tracks = [
        track(1, 0, 0),
        track(2, 0, 0),
        track(3, 7, 1),
        track(4, 7, 1),
        track(5, 7, 2),
        track(6, 8, 1),
        track(7, 0, 0, albumartist="B"),
    ]
    groups = grouping.group(tracks)
    assert [(g.album_id, g.albumartist, g.start, g.stop) for g in groups] == [
        (0, "A", 0, 2),
        (7, "A", 2, 5),
        (8, "A", 5, 6),
        (0, "B", 6, 7),
    ]
    assert groups[1].media == (grouping.Medium(1, 2, 4), grouping.Medium(2, 4, 5))
    assert groups[0].media == ()


def test_grouping_of_the_index(tmp_path: Path) -> None:
    """A same-named album and single stay apart, as beets has them."""
    library = beets.library.Library(str(tmp_path / "library.db"), str(tmp_path))
    for album, count in [("Song", 3), ("Song", 1)]:
        library.add_album(
            [
                item(tmp_path, f"{album}{count}{n}", album=album, albumartist="A")
                for n in range(count)
            ]
        )
    groups = grouping.group(load_index(library).tracks)
    assert sorted(g.stop - g.start for g in groups) == [1, 3]


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
