# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The search and the library service."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from roquette.library import LibraryError
from roquette.library.query import QueryError, Stopped
from roquette.library.search import Search
from roquette.library.service import LibraryService, ReadOnlyError
from roquette.threads import Inbox
from tests.conftest import Library


def run[T](main: Callable[[Inbox], Awaitable[T]]) -> T:
    async def with_inbox() -> T:
        inbox = Inbox(asyncio.get_running_loop())
        try:
            return await main(inbox)
        finally:
            inbox.close()

    return asyncio.run(with_inbox())


def writable(beets_dir: Path) -> None:
    with (beets_dir / "config.yaml").open("a", encoding="utf-8") as file:
        file.write("roquette:\n  library:\n    writable: yes\n")


# The search


def test_search_runs_the_latest_text() -> None:
    ran: list[str] = []
    found: list[tuple[str, int]] = []

    async def main(inbox: Inbox) -> None:
        release = asyncio.Event()

        async def query(text: str, stop: Callable[[], bool]) -> int:
            ran.append(text)
            if text == "p":
                await release.wait()
            return len(text)

        search = Search(query, lambda text, result: found.append((text, result)))
        search.set_text("p")
        await asyncio.sleep(0)
        for text in ["pi", "pin", " pink  floyd "]:
            search.set_text(text)
        release.set()
        await search.wait()
        # The same text again (whitespace aside) doesn't run again.
        search.set_text("pink floyd")
        await search.wait()

    run(main)
    assert ran == ["p", "pink floyd"]
    assert found == [("pink floyd", 10)]


def test_search_stops_slow_queries() -> None:
    found: list[str] = []

    async def main(inbox: Inbox) -> None:
        started = asyncio.Event()

        async def query(text: str, stop: Callable[[], bool]) -> str:
            if text == "slow":
                started.set()
                while not stop():
                    await asyncio.sleep(0.001)
                raise Stopped
            return text

        search = Search(query, lambda text, result: found.append(result))
        search.set_text("slow")
        await started.wait()
        search.set_text("fast")
        await search.wait()

    run(main)
    assert found == ["fast"]


def test_search_reports_invalid_queries() -> None:
    found: list[str] = []
    failed: list[tuple[str, str]] = []

    async def main(inbox: Inbox) -> None:
        async def query(text: str, stop: Callable[[], bool]) -> str:
            if text.endswith(":"):
                raise QueryError("incomplete")
            return text

        search = Search(
            query,
            lambda text, result: found.append(result),
            lambda text, message: failed.append((text, message)),
        )
        search.set_text("year:")
        await search.wait()
        search.set_text("year:1975")
        await search.wait()

    run(main)
    assert failed == [("year:", "incomplete")]
    assert found == ["year:1975"]


# The service


def test_service_reads(library: Library) -> None:
    async def main(inbox: Inbox) -> None:
        service = LibraryService(inbox)
        try:
            settings = await service.open()
            assert settings.writable is False
            assert not service.writable
            assert service.plugins == ["roquette"]
            collection = await service.collection()
            assert len(collection.index.tracks) == len(library.items)
            assert len(collection.groups) == 7
            assert len(collection.versions) == 3
            found = await service.query("pink year:1979")
            assert len(found) == 4
            text = await service.track_text(found[:1])
            assert text[found[0]].album == "The Wall"
            wall = library.albums[("The Wall", "album")]
            releases = await service.release_text([wall])
            assert releases[wall].title == "The Wall"
            playables = await service.playables(list(found))
            assert [p.id for p in playables] == list(found)
            with pytest.raises(QueryError):
                await service.query('"pink')
            with pytest.raises(ReadOnlyError):
                await service.store(found[0], lambda item: {"play_count": 1})
        finally:
            service.close()

    run(main)


@pytest.mark.usefixtures("beets_dir")
def test_service_without_a_library() -> None:
    async def main(inbox: Inbox) -> None:
        service = LibraryService(inbox)
        try:
            with pytest.raises(LibraryError, match="no beets library"):
                await service.open()
        finally:
            service.close()

    run(main)


def test_service_with_a_config_file(library: Library, tmp_path: Path) -> None:
    other = tmp_path / "other.yaml"
    other.write_text("library: /nowhere/library.db\n", encoding="utf-8")

    async def main(inbox: Inbox) -> None:
        service = LibraryService(inbox, other)
        try:
            with pytest.raises(LibraryError, match="/nowhere/library"):
                await service.open()
        finally:
            service.close()

    run(main)


def test_service_writes(library: Library, beets_dir: Path) -> None:
    writable(beets_dir)
    item_id = library.items["Hey You"]

    def add_play(item: Any) -> dict[str, int]:
        return {"play_count": int(item.get("play_count", 0)) + 1}

    async def main(inbox: Inbox) -> int:
        service = LibraryService(inbox)
        try:
            await service.open()
            assert service.writable
            await service.store(item_id, add_play)
            await service.store(item_id, add_play)
            return len(await service.query("play_count:2"))
        finally:
            service.close()

    assert run(main) == 1


def test_service_reports_outside_changes(library: Library, beets_dir: Path) -> None:
    """Changes by other programs, not roquette's own writes."""
    writable(beets_dir)
    item_id = library.items["Hey You"]
    changes: list[int] = []

    async def main(inbox: Inbox) -> None:
        service = LibraryService(inbox)
        try:
            await service.open()
            service.watch(lambda: changes.append(1), delay=0.1)
            await service.store(item_id, lambda item: {"play_count": 1})
            await asyncio.sleep(0.4)
            assert changes == []
            other = sqlite3.connect(library.path)
            with other:
                other.execute("UPDATE items SET title = 'Hey' WHERE id = ?", (item_id,))
            other.close()
            await asyncio.sleep(0.4)
            assert changes == [1]
            text = await service.track_text([item_id])
            assert text[item_id].title == "Hey"
        finally:
            service.close()

    run(main)


def test_service_read_only_reports_outside_changes(library: Library) -> None:
    changes: list[int] = []

    async def main(inbox: Inbox) -> None:
        service = LibraryService(inbox)
        try:
            await service.open()
            service.watch(lambda: changes.append(1), delay=0.1)
            # Reading doesn't count.
            await service.collection()
            await asyncio.sleep(0.3)
            assert changes == []
            other = sqlite3.connect(library.path)
            with other:
                other.execute("UPDATE items SET title = 'Hey'")
            other.close()
            await asyncio.sleep(0.4)
            assert changes == [1]
        finally:
            service.close()

    run(main)
