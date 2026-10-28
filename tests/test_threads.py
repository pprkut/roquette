# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from roquette.threads import Inbox


def test_post_from_a_thread() -> None:
    async def main() -> list[str]:
        loop = asyncio.get_running_loop()
        inbox = Inbox(loop)
        done = loop.create_future()
        seen: list[str] = []

        def on_loop(name: str) -> None:
            assert threading.current_thread() is threading.main_thread()
            seen.append(name)
            if len(seen) == 2:
                done.set_result(None)

        def worker() -> None:
            inbox.post(on_loop, "first")
            inbox.post(on_loop, "second")

        threading.Thread(target=worker).start()
        await asyncio.wait_for(done, 5)
        inbox.close()
        return seen

    assert asyncio.run(main()) == ["first", "second"]


def test_failing_callback() -> None:
    async def main() -> tuple[list[BaseException], bool]:
        loop = asyncio.get_running_loop()
        errors: list[BaseException] = []
        loop.set_exception_handler(
            lambda _, context: errors.append(context["exception"])
        )
        inbox = Inbox(loop)
        done = loop.create_future()

        def fail() -> None:
            raise ValueError

        # Both are queued before the loop drains the inbox.
        inbox.post(fail)
        inbox.post(done.set_result, True)
        result = await asyncio.wait_for(done, 5)
        inbox.close()
        return errors, result

    errors, result = asyncio.run(main())
    assert result
    assert [type(error) for error in errors] == [ValueError]


def test_run() -> None:
    async def main() -> None:
        inbox = Inbox(asyncio.get_running_loop())
        with ThreadPoolExecutor(1) as pool:
            assert await inbox.run(pool, lambda: threading.current_thread().name) != (
                threading.main_thread().name
            )
            with pytest.raises(ZeroDivisionError):
                await inbox.run(pool, lambda: 1 / 0)
        inbox.close()

    asyncio.run(main())
