#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Measure the library layer's work on a beets library (spike S3).

For the beets data of `$BEETSDIR` (default dev/beets, the copy of
`just dev-library`): the time and memory of opening the library with
its plugins, of the whole library as beets `Item`s and `Album`s against
the compact index, of the recording index and the grouping, of typical
queries, and of the per-thread connections.

Every scenario runs in a process of its own, `--runs` times; the
figures are medians. Memory is the process' RSS (anonymous memory
mostly, as the database file is read through SQLite's page cache):
"peak" is the high-water mark during the work, "kept" what stays after
a garbage collection and `malloc_trim()` while the result is still
referenced, both above what the process had before the work.

Prints a Markdown summary.
"""

import argparse
import ctypes
import gc
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

SCENARIOS = [
    "open",
    "items",
    "albums",
    "items-iter",
    "index",
    "index-full",
    "columns",
    "queries",
    "connections",
]


# Measuring inside a scenario's process.


def memory() -> dict[str, float]:
    """RSS, its anonymous part and the high-water mark, in MiB."""
    values = {}
    with Path("/proc/self/status").open() as status:
        for line in status:
            key, _, value = line.partition(":")
            if key in ("VmRSS", "RssAnon", "VmHWM"):
                values[key] = int(value.split()[0]) / 1024
    return values


def trim() -> None:
    gc.collect()
    ctypes.CDLL("libc.so.6").malloc_trim(0)


def reset_peak() -> None:
    """Reset VmHWM to the current RSS."""
    Path("/proc/self/clear_refs").write_text("5")


class Work:
    """Time and memory of one piece of work."""

    def __init__(self) -> None:
        self.seconds = 0.0
        self.peak = 0.0
        self.kept = 0.0

    def as_dict(self) -> dict[str, float]:
        return {"ms": self.seconds * 1000, "peak": self.peak, "kept": self.kept}


@contextmanager
def measured() -> Iterator[Work]:
    work = Work()
    trim()
    before = memory()["VmRSS"]
    reset_peak()
    start = time.perf_counter()
    yield work
    work.seconds = time.perf_counter() - start
    work.peak = memory()["VmHWM"] - before
    trim()
    work.kept = memory()["VmRSS"] - before


def open_library() -> Any:
    """beets' config and plugins as `beet` loads them, and the library."""
    sys.path.insert(0, str(REPO))
    import beets.library
    from beets import config, plugins

    from roquette.app import check_library_copy

    check_library_copy()
    plugins.load_plugins()
    # The plugins' field types, as `beet` registers them.
    _ = beets.library.Item._types, beets.library.Album._types
    return beets.library.Library(
        config["library"].as_filename(), config["directory"].as_filename()
    )


def scenario_open() -> dict[str, Any]:
    start = time.perf_counter()
    sys.path.insert(0, str(REPO))
    import beets.library
    from beets import config, plugins

    from roquette.app import check_library_copy

    check_library_copy()
    imported = time.perf_counter()
    after_import = memory()["VmRSS"]
    plugins.load_plugins()
    _ = beets.library.Item._types, beets.library.Album._types
    loaded = time.perf_counter()
    after_plugins = memory()["VmRSS"]
    library = beets.library.Library(
        config["library"].as_filename(), config["directory"].as_filename()
    )
    opened = time.perf_counter()
    with library.transaction() as tx:
        items = tx.query("SELECT count(*) FROM items")[0][0]
        albums = tx.query("SELECT count(*) FROM albums")[0][0]
        flex = tx.query("SELECT count(*) FROM item_attributes")[0][0]
        singletons = tx.query("SELECT count(*) FROM items WHERE album_id IS NULL")
    return {
        "import ms": (imported - start) * 1000,
        "plugins ms": (loaded - imported) * 1000,
        "open ms": (opened - loaded) * 1000,
        "import MiB": after_import,
        "plugins MiB": after_plugins - after_import,
        "open MiB": memory()["VmRSS"] - after_plugins,
        "plugins": " ".join(sorted(p.name for p in plugins.find_plugins())),
        "items": items,
        "albums": albums,
        "singletons": singletons[0][0],
        "flexible attributes": flex,
        "database MiB": Path(library.path).stat().st_size / 2**20,
    }


def scenario_items() -> dict[str, Any]:
    library = open_library()
    with measured() as work:
        items = list(library.items())
    return {"count": len(items), **work.as_dict()}


def scenario_albums() -> dict[str, Any]:
    library = open_library()
    with measured() as work:
        albums = list(library.albums())
    return {"count": len(albums), **work.as_dict()}


def scenario_items_iter() -> dict[str, Any]:
    """The compact index's tracks through beets' API: every Item is made
    and dropped again."""
    library = open_library()
    from roquette.library.index import Track

    intern = sys.intern
    with measured() as work:
        tracks = [
            Track(
                item.id,
                item.album_id or 0,
                item.disc,
                item.track,
                item.title,
                intern(item.artist),
                intern(item.album),
                intern(item.albumartist),
                item.length,
            )
            for item in library.items()
        ]
    return {"count": len(tracks), **work.as_dict()}


def scenario_index() -> dict[str, Any]:
    """S1's compact index: one narrow query per table."""
    library = open_library()
    from roquette.library.index import load_index

    with measured() as work:
        index = load_index(library)
    return {"count": len(index.tracks), **work.as_dict()}


def scenario_index_full() -> dict[str, Any]:
    """The index with the recording index and the grouping."""
    library = open_library()
    from roquette.library import grouping, recordings
    from roquette.library.index import load_index

    result: dict[str, Any] = {}
    with measured() as total:
        with measured() as work:
            index = load_index(library)
        result["index ms"] = work.seconds * 1000
        with measured() as work:
            versions = recordings.index_versions(recordings.load_versions(library))
        result["versions ms"] = work.seconds * 1000
        result["versions MiB"] = work.kept
        with measured() as work:
            groups = grouping.group(index.tracks)
        result["grouping ms"] = work.seconds * 1000
        result["grouping MiB"] = work.kept
    result["tracks with versions"] = len(versions)
    result["recordings with versions"] = len({id(v) for v in versions.values()})
    result["groups"] = len(groups)
    result["singleton groups"] = sum(1 for g in groups if not g.album_id)
    return {**result, **total.as_dict()}


_COLUMNS = """
    SELECT id, album_id, disc, track, length FROM items
    ORDER BY COALESCE(NULLIF(albumartist_sort, ''), albumartist) COLLATE NOCASE,
             album COLLATE NOCASE, album_id, disc, track
"""


def scenario_columns() -> dict[str, Any]:
    """The index as columns of numbers in the collection's order, without
    text: what's shown is read by id for the rows on screen."""
    library = open_library()
    from array import array

    with measured() as work:
        ids, albums, lengths = array("q"), array("q"), array("f")
        discs, tracks = array("h"), array("h")
        with library.transaction():
            cursor = library._connection().execute(_COLUMNS)
            cursor.row_factory = None
            for row in cursor:
                ids.append(row[0])
                albums.append(row[1] or 0)
                discs.append(row[2] or 0)
                tracks.append(row[3] or 0)
                lengths.append(row[4] or 0.0)
    middle = len(ids) // 2
    screen = ids[middle : middle + 60].tolist()
    sql = (
        "SELECT id, title, artist, album, albumartist FROM items "
        f"WHERE id IN ({', '.join('?' * len(screen))})"
    )

    def read_screen() -> None:
        with library.transaction() as tx:
            tx.query(sql, screen)

    return {
        "count": len(ids),
        **work.as_dict(),
        "60 rows' text ms": median_ms(read_screen, runs=5),
    }


def typical_queries(library: Any) -> list[str]:
    """Queries of the kinds roquette makes, with names from the library."""
    with library.transaction() as tx:
        artist = tx.query(
            "SELECT albumartist FROM items WHERE albumartist != '' "
            "GROUP BY albumartist ORDER BY count(*) DESC LIMIT 1"
        )
        word = tx.query("SELECT title FROM items ORDER BY id LIMIT 1")
    artist_word = (artist[0][0] if artist else "a").split()[0]
    title_word = (word[0][0] if word else "a").split()[0]
    return [
        f"albumartist:{artist_word}",
        title_word,
        f"{title_word} year:1990..1999",
        "year:1990..1999",
        "format:MP3",
        "albumtype:single",
        "singleton:true",
        "added:-1y..",
        "title::^The",
        "fmps_rating:0.8..",
        "play_count:1..",
        "last_played:-1y..",
    ]


def median_ms(function: Callable[[], object], runs: int = 3) -> float:
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        function()
        times.append(time.perf_counter() - start)
    return statistics.median(times) * 1000


def matching_ids(library: Any, query_string: str) -> list[int]:
    """The ids of the items a query matches, without making Items where
    beets can run the query in SQL."""
    import beets.library

    model = beets.library.Item
    with library.music_dir_context():
        query, _ = beets.library.parse_query_string(query_string, model)
    where, subvals = query.clause()
    if where is None:
        # A slow query: beets matches every Item in Python.
        return [item.id for item in library.items(query)]
    source = model._table
    if query.field_names & model.other_db_fields:
        source += f" {model.relation_join}"
    sql = f"SELECT items.id FROM {source} WHERE {where} GROUP BY items.id"
    with library.transaction() as tx:
        return [row[0] for row in tx.query(sql, subvals)]


# Flexible attributes as SQL instead of Python: what a query class from
# the roquette plugin could do (`item_queries`).
_FLEX_SQL = {
    "fmps_rating:0.8..": (
        "SELECT entity_id FROM item_attributes "
        "WHERE key = 'fmps_rating' AND CAST(value AS REAL) >= 0.8"
    ),
    "play_count:1..": (
        "SELECT entity_id FROM item_attributes "
        "WHERE key = 'play_count' AND CAST(value AS INTEGER) >= 1"
    ),
    "last_played:-1y..": (
        "SELECT entity_id FROM item_attributes "
        "WHERE key = 'last_played' AND CAST(value AS REAL) >= "
        f"{time.time() - 365 * 86400}"
    ),
}


def scenario_queries() -> dict[str, Any]:
    library = open_library()
    import beets.library

    rows = []
    for query_string in typical_queries(library):
        with library.music_dir_context():
            query, _ = beets.library.parse_query_string(
                query_string, beets.library.Item
            )
        fast = query.clause()[0] is not None
        count = len(matching_ids(library, query_string))
        row: dict[str, Any] = {
            "query": query_string,
            "in SQL": "yes" if fast else "no",
            "matches": count,
            "Items ms": median_ms(lambda q=query_string: list(library.items(q))),
            "ids ms": median_ms(lambda q=query_string: matching_ids(library, q)),
        }
        if query_string in _FLEX_SQL:
            sql = _FLEX_SQL[query_string]

            def flex(sql: str = sql) -> list[int]:
                with library.transaction() as tx:
                    return [r[0] for r in tx.query(sql)]

            row["ids in SQL ms"] = median_ms(flex)
            row["SQL matches"] = len(flex())
        rows.append(row)
    return {"rows": rows}


def scenario_connections() -> dict[str, Any]:
    """beets makes a connection per thread, and lets one transaction run
    at a time per `Library`."""
    library = open_library()
    import beets.library
    from beets import config

    from roquette.library.index import load_index

    load_index(library)
    trim()
    before = memory()["VmRSS"]
    results: dict[str, Any] = {}
    threads = 8
    barrier = threading.Barrier(threads)

    def read() -> None:
        load_index(library)
        # Keep the thread alive until every thread has read.
        barrier.wait()

    workers = [threading.Thread(target=read) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    trim()
    results["threads"] = threads
    results["connections"] = len(library._connections)
    results["MiB per connection"] = (memory()["VmRSS"] - before) / threads
    library._close()
    trim()
    results["MiB after closing"] = memory()["VmRSS"] - before

    def timed(libraries: list[Any]) -> float:
        def work(lib: Any) -> None:
            load_index(lib)

        workers = [threading.Thread(target=work, args=(lib,)) for lib in libraries]
        start = time.perf_counter()
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        return (time.perf_counter() - start) * 1000

    results["one index read ms"] = timed([library])
    results["4 at once, one Library ms"] = timed([library] * 4)
    others = [
        beets.library.Library(
            config["library"].as_filename(), config["directory"].as_filename()
        )
        for _ in range(4)
    ]
    results["4 at once, a Library each ms"] = timed(others)
    return results


# Running the scenarios and reporting.


def run_child(scenario: str) -> dict[str, Any]:
    output = subprocess.run(
        [sys.executable, __file__, "--child", scenario],
        check=True,
        capture_output=True,
        text=True,
    )
    result: dict[str, Any] = json.loads(output.stdout.splitlines()[-1])
    return result


def median_of(results: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in results[0].items():
        if key == "rows":
            merged[key] = [
                median_of([r["rows"][i] for r in results]) for i in range(len(value))
            ]
        elif isinstance(value, int | float) and not isinstance(value, bool):
            merged[key] = statistics.median(r[key] for r in results)
        else:
            merged[key] = value
    return merged


def fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def table(rows: list[dict[str, Any]]) -> str:
    keys: list[str] = []
    for row in rows:
        keys.extend(k for k in row if k not in keys)
    lines = [
        "| " + " | ".join(keys) + " |",
        "|" + "---|" * len(keys),
    ]
    lines.extend(
        "| " + " | ".join(fmt(row.get(k, "")) for k in keys) + " |" for row in rows
    )
    return "\n".join(lines)


def summary(results: dict[str, dict[str, Any]]) -> str:
    out = []
    if "open" in results:
        opened = dict(results["open"])
        out.append("Library and opening it (MiB of RSS, ms from `python3`):\n")
        out.append(table([opened]))
    whole = [
        {"what": name, **results[key]}
        for key, name in [
            ("items", "all Items (`list(lib.items())`)"),
            ("albums", "all Albums (`list(lib.albums())`)"),
            ("items-iter", "index through Items (made and dropped)"),
            ("index", "index, narrow SQL (S1)"),
            ("index-full", "index + versions + grouping"),
            ("columns", "index as columns of numbers"),
        ]
        if key in results
    ]
    if whole:
        out.append("\nThe whole library (memory in MiB above the opened library):\n")
        out.append(
            table(
                [
                    {
                        k: v
                        for k, v in row.items()
                        if k in ("what", "count", "ms", "peak", "kept")
                    }
                    for row in whole
                ]
            )
        )
    if "columns" in results:
        out.append("\nIndex as columns, and a screen of text read by id:\n")
        out.append(table([results["columns"]]))
    if "index-full" in results:
        out.append("\nIndex, versions and grouping:\n")
        out.append(table([results["index-full"]]))
    if "queries" in results:
        out.append("\nQueries (ms; Items: `list(lib.items(q))`, ids: matching ids):\n")
        out.append(table(results["queries"]["rows"]))
    if "connections" in results:
        out.append("\nConnections:\n")
        out.append(table([results["connections"]]))
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--child", choices=SCENARIOS, help=argparse.SUPPRESS)
    parser.add_argument(
        "--runs", type=int, default=3, help="runs of each scenario (default 3)"
    )
    parser.add_argument(
        "scenarios", nargs="*", choices=[[], *SCENARIOS], metavar="SCENARIO"
    )
    args = parser.parse_args()

    if args.child:
        function = globals()["scenario_" + args.child.replace("-", "_")]
        print(json.dumps(function()))
        return 0

    os.environ.setdefault("BEETSDIR", str(REPO / "dev" / "beets"))
    if not (Path(os.environ["BEETSDIR"]) / "config.yaml").exists():
        print(f"no beets data in {os.environ['BEETSDIR']}", file=sys.stderr)
        return 1
    results = {}
    for scenario in args.scenarios or SCENARIOS:
        print(f"{scenario}…", file=sys.stderr)
        results[scenario] = median_of([run_child(scenario) for _ in range(args.runs)])
    print(f"BEETSDIR={os.environ['BEETSDIR']}, Python {sys.version.split()[0]}\n")
    print(summary(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
