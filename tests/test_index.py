# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

import os
import threading
from pathlib import Path

import beets.library

from roquette.library.index import LibraryIndex, load_index


def make_library(tmp_path: Path) -> beets.library.Library:
    music = tmp_path / "music"
    library = beets.library.Library(str(tmp_path / "library.db"), str(music))
    for album, albumartist, sort, tracks in [
        ("Zebra", "The Band", "Band, The", 2),
        ("Apple", "Zed", "", 1),
    ]:
        items = [
            beets.library.Item(
                title=f"{album} {n}",
                artist=albumartist,
                albumartist=albumartist,
                albumartist_sort=sort,
                album=album,
                track=n,
                disc=1,
                length=60.0 * n,
                path=os.fsencode(music / album / f"{n}.flac"),
            )
            for n in range(tracks, 0, -1)
        ]
        stored = library.add_album(items)
        stored.artpath = os.fsencode(music / album / "cover.jpg")
        stored.store()
    return library


def test_index(tmp_path: Path) -> None:
    index = load_index(make_library(tmp_path))
    # Sorted by album artist's sort name, then disc and track.
    assert [t.title for t in index.tracks] == ["Zebra 1", "Zebra 2", "Apple 1"]
    assert [r.title for r in index.releases] == ["Zebra", "Apple"]
    # Stored relative to the music directory, read back absolute.
    assert index.releases[0].cover == tmp_path / "music" / "Zebra" / "cover.jpg"


def test_index_on_another_thread(tmp_path: Path) -> None:
    """The music directory is a context variable threads don't inherit."""
    library = make_library(tmp_path)
    result: list[LibraryIndex] = []
    thread = threading.Thread(target=lambda: result.append(load_index(library)))
    thread.start()
    thread.join()
    assert result[0].releases[1].cover == tmp_path / "music" / "Apple" / "cover.jpg"
