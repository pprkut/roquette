# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The live playlist's M3U8 file."""

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

import pytest

from roquette.library.index import PlaylistItem
from roquette.queue import m3u
from roquette.queue.m3u import PlaylistFile, Saved
from roquette.queue.playlist import Entry, Playlist
from roquette.threads import Inbox


def item(id: int, path: bytes | None = None) -> PlaylistItem:
    return PlaylistItem(id, 0, 1, id, path or f"/m/{id}.flac".encode())


def test_round_trip(tmp_path: Path) -> None:
    rows = [
        Saved(b"/m/a.flac", 1),
        Saved(b"/m/b \xff.flac", 2, pinned=True, current=True),
        Saved(b"/m/c.flac"),
    ]
    path = tmp_path / "state" / "playlist.m3u8"
    m3u.write(path, rows)
    assert m3u.read(path) == rows
    text = path.read_bytes()
    assert text.startswith(b"#EXTM3U\n#ROQUETTE:item=1\n/m/a.flac\n")
    assert b"#ROQUETTE:item=2 pinned current\n/m/b \xff.flac\n" in text
    assert text.endswith(b"/m/b \xff.flac\n/m/c.flac\n")
    # Nothing left behind.
    assert [p.name for p in path.parent.iterdir()] == ["playlist.m3u8"]


def test_other_players_files(tmp_path: Path) -> None:
    text = (
        "#EXTM3U\r\n#EXTINF:123,Artist - Title\r\n/m/a.flac\r\n\r\n"
        "relative/b.flac\n#ROQUETTE:item=x unknown\n/m/c.flac\n"
    )
    assert m3u.parse(text, tmp_path) == [
        Saved(b"/m/a.flac"),
        Saved(bytes(tmp_path / "relative/b.flac")),
        Saved(b"/m/c.flac"),
    ]


def test_no_file(tmp_path: Path) -> None:
    assert m3u.read(tmp_path / "none.m3u8") == []


def test_a_path_that_isnt_a_line() -> None:
    assert m3u.dumps([Saved(b"/m/a\nb.flac"), Saved(b"/m/c.flac")]) == (
        "#EXTM3U\n/m/c.flac\n"
    )


def test_rows() -> None:
    a, b, c = Entry(item(1)), Entry(item(2), pinned=True), Entry(item(3))
    playlist = Playlist()
    playlist.append([a, b, c])
    playlist.current = b
    assert m3u.rows(playlist) == [
        Saved(b"/m/1.flac", 1),
        Saved(b"/m/2.flac", 2, pinned=True, current=True),
        Saved(b"/m/3.flac", 3),
    ]
    # Removed while it plays: what follows it is current.
    playlist.remove([1])
    assert [r.current for r in m3u.rows(playlist)] == [False, True]


class Library:
    def __init__(self, items: Sequence[PlaylistItem]) -> None:
        self.items = {i.id: i for i in items}

    async def playlist_items_by_path(
        self, paths: Sequence[bytes]
    ) -> dict[bytes, PlaylistItem]:
        by_path = {i.path: i for i in self.items.values()}
        return {p: by_path[p] for p in paths if p in by_path}

    async def playlist_items(self, ids: Sequence[int]) -> list[PlaylistItem]:
        return [self.items[i] for i in ids if i in self.items]


def test_entries() -> None:
    moved = item(2, b"/m/moved.flac")
    # Two items of one file, and an id that's another file's now.
    twin = item(4, b"/m/1.flac")
    other = item(5, b"/m/other.flac")
    library = Library([item(1), moved, item(3), twin, other])
    saved = [
        Saved(b"/m/1.flac", 1, pinned=True),
        # Moved since, found by its id.
        Saved(b"/m/2.flac", 2),
        Saved(b"/m/gone.flac", 9, current=True),
        Saved(b"/m/3.flac", None, current=True),
        Saved(b"/m/1.flac", 4),
        Saved(b"/m/3.flac", 5),
    ]
    entries, current = asyncio.run(m3u.entries(library, saved))
    assert [e.item for e in entries] == [item(1), moved, item(3), twin, item(3)]
    assert [e.pinned for e in entries] == [True, False, False, False, False]
    assert current == 2


def run_file(
    tmp_path: Path,
    scenario: Callable[[PlaylistFile], Coroutine[Any, Any, None]],
    delay: float = 0.01,
) -> Path:
    path = tmp_path / "playlist.m3u8"

    async def main() -> None:
        inbox = Inbox(asyncio.get_running_loop())
        file = PlaylistFile(path, inbox, delay)
        try:
            await asyncio.wait_for(scenario(file), 10)
        finally:
            file.close()
            inbox.close()

    asyncio.run(main())
    return path


def test_saved_after_a_change(tmp_path: Path) -> None:
    playlist = Playlist()
    playlist.append([Entry(item(1))])

    async def scenario(file: PlaylistFile) -> None:
        file.changed(playlist)
        file.changed(playlist)
        assert not file.path.exists()
        while not file.path.exists() or file._writing:
            await asyncio.sleep(0.005)
        assert m3u.read(file.path) == [Saved(b"/m/1.flac", 1)]
        # Changed and not saved yet: saved when it closes.
        playlist.append([Entry(item(2))])
        file.delay = 10
        file.changed(playlist)

    path = run_file(tmp_path, scenario)
    assert [r.item for r in m3u.read(path)] == [1, 2]


def test_load(tmp_path: Path) -> None:
    m3u.write(tmp_path / "playlist.m3u8", [Saved(b"/m/1.flac", 1, current=True)])

    async def scenario(file: PlaylistFile) -> None:
        entries, current = await file.load(Library([item(1)]))
        assert [e.item.id for e in entries] == [1] and current == 0

    run_file(tmp_path, scenario)


def test_a_write_that_fails(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "playlist.m3u8").mkdir()
    playlist = Playlist()

    async def scenario(file: PlaylistFile) -> None:
        file.changed(playlist)
        while file._timer is not None or file._writing:
            await asyncio.sleep(0.005)

    run_file(tmp_path, scenario)
    assert "couldn't save the live playlist" in caplog.text


def test_closed_while_a_write_is_on_its_way(tmp_path: Path) -> None:
    """The write that was handed off finishes, and a change after it is
    written too; nothing older is written after that."""
    playlist = Playlist()
    playlist.append([Entry(item(1))])

    async def scenario(file: PlaylistFile) -> None:
        file.delay = 0.0
        file.changed(playlist)
        async with asyncio.timeout(10):
            while not file._writing:
                await asyncio.sleep(0)
        playlist.append([Entry(item(2))])
        file.changed(playlist)
        file.close()
        assert [r.item for r in m3u.read(file.path)] == [1, 2]
        # Changes after it closed aren't written.
        playlist.append([Entry(item(3))])
        file.changed(playlist)
        await asyncio.sleep(0.05)

    path = run_file(tmp_path, scenario)
    assert [r.item for r in m3u.read(path)] == [1, 2]
