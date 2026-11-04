#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""What loading the beets plugins costs roquette, and what they do (S3).

roquette loads the plugins of the beets config, so that their field
types, queries and template fields are known. Loading a plugin should
only register it: this checks, for each plugin of the config of
`$BEETSDIR` (default dev/beets), or with `--all` for every plugin beets
ships, in a process of its own,

- the time and memory (RSS) its import and creation take;
- what it does meanwhile: files opened for writing, network
  connections and name lookups, subprocesses, threads (through an audit
  hook, `sys.addaudithook`);
- which events it listens to, and what it does on the events roquette
  sends itself: `pluginload` (sent by `load_plugins()`) and
  `database_change` (sent by every `store()`, which roquette's writes
  use). `library_opened`, `cli_exit` and the import events are sent by
  `beet`, not by roquette.

The database_change check stores an item of an in-memory library, so
the beets data isn't touched. Prints a Markdown table.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent

# What roquette's process sends.
ROQUETTE_EVENTS = {"pluginload", "database_change"}

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


class Recorder:
    """Side effects seen by an audit hook while `recording` is set."""

    def __init__(self) -> None:
        self.recording = False
        self.seen: list[str] = []
        sys.addaudithook(self.hook)

    def hook(self, event: str, args: tuple[Any, ...]) -> None:
        if not self.recording:
            return
        note = None
        if event == "open":
            path, mode, flags = args
            writing = (
                any(c in mode for c in "wax+")
                if isinstance(mode, str)
                else bool(flags & _WRITE_FLAGS)
            )
            if writing and not str(path).startswith(("/dev/", "/proc/")):
                note = f"writes {path}"
        elif event == "socket.connect":
            note = f"connects to {args[1]}"
        elif event == "socket.getaddrinfo":
            note = f"looks up {args[0]}"
        elif event in ("subprocess.Popen", "os.system"):
            note = f"runs {args[0]}"
        elif event in ("os.remove", "os.rename", "os.mkdir", "shutil.rmtree"):
            note = f"{event} {args[0]}"
        if note and note not in self.seen:
            self.seen.append(note)


def rss() -> float:
    with Path("/proc/self/status").open() as status:
        for line in status:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    return 0.0


def check(name: str, all_mode: bool) -> dict[str, Any]:
    """Load one plugin (`*`: all of the config's), as `load_plugins()`
    does, and send it the events roquette sends."""
    sys.dont_write_bytecode = True
    recorder = Recorder()
    import beets.library
    from beets import config, plugins

    config.read()
    if not config["plugins"].as_str_seq() or name != "*":
        config["plugins"] = [name]
    if all_mode:
        # A backend that loads with what roquette has anyway.
        config["replaygain"]["backend"] = "gstreamer"
    config["pluginpath"] = [str(REPO / "beetsplug"), *config["pluginpath"].as_str_seq()]
    threads_before = set(threading.enumerate())
    before = rss()
    start = time.perf_counter()
    recorder.recording = True
    plugins.load_plugins()
    recorder.recording = False
    loaded = time.perf_counter()
    after = rss()
    result: dict[str, Any] = {"plugin": name}
    instances = list(plugins.find_plugins())
    if not instances:
        result["loads"] = "no"
        result["at load"] = "; ".join(recorder.seen)
        return result
    # Every plugin listens to pluginload through BeetsPlugin itself, to
    # check its config; that's not counted.
    listens = sorted(
        {
            event
            for event, funcs in plugins.BeetsPlugin._raw_listeners.items()
            for func in funcs
            if not func.__qualname__.startswith("BeetsPlugin.")
        }
    )
    result.update(
        {
            "loads": "yes",
            "ms": (loaded - start) * 1000,
            "MiB": after - before,
            "threads": len(set(threading.enumerate()) - threads_before),
            "listens to": ", ".join(
                f"**{e}**" if e in ROQUETTE_EVENTS else e for e in listens
            ),
            "types": ", ".join(
                sorted(t for p in instances for t in getattr(p, "item_types", {}))
            ),
            "at load": "; ".join(recorder.seen),
        }
    )

    # A store() of an item, as roquette's writes do.
    recorder.seen.clear()
    _ = beets.library.Item._types
    # A database of its own: beets backs up even an in-memory one before
    # its migrations, into the current directory.
    scratch = tempfile.mkdtemp()
    library = beets.library.Library(f"{scratch}/library.db", "/nonexistent/music")
    item = beets.library.Item(title="t", path=b"/nonexistent/music/t.flac")
    library.add(item)
    item["play_count"] = 1
    recorder.recording = True
    start = time.perf_counter()
    item.store()
    result["store ms"] = (time.perf_counter() - start) * 1000
    recorder.recording = False
    result["on store"] = "; ".join(recorder.seen)
    shutil.rmtree(scratch)
    return result


def configured_plugins() -> list[str]:
    from beets import config

    config.read()
    return config["plugins"].as_str_seq()


def all_plugins() -> list[str]:
    import beetsplug

    names = set()
    for directory in beetsplug.__path__:
        for path in Path(directory).iterdir():
            if path.name.startswith("_") or path.name == "roquette.py":
                continue
            if path.suffix == ".py" or (path / "__init__.py").exists():
                names.add(path.stem)
    return sorted(names)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument(
        "--all", action="store_true", help="every plugin beets ships, not the config's"
    )
    args = parser.parse_args()
    if args.child:
        print(json.dumps(check(args.child, args.all)))
        return 0

    os.environ.setdefault("BEETSDIR", str(REPO / "dev" / "beets"))
    names = all_plugins() if args.all else [*configured_plugins(), "*"]
    rows = []
    for name in names:
        print(f"{name}…", file=sys.stderr)
        output = subprocess.run(
            [
                sys.executable,
                __file__,
                "--child",
                name,
                *(["--all"] if args.all else []),
            ],
            capture_output=True,
            text=True,
            # A plugin that does what it shouldn't mustn't hang the check.
            timeout=60,
        )
        try:
            row = json.loads(output.stdout.splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            row = {"plugin": name, "loads": "crashed"}
        if name == "*":
            row["plugin"] = "all of them"
        rows.append(row)
    keys: list[str] = []
    for row in rows:
        keys.extend(k for k in row if k not in keys)
    print(f"BEETSDIR={os.environ['BEETSDIR']}\n")
    print("| " + " | ".join(keys) + " |")
    print("|" + "---|" * len(keys))
    for row in rows:
        cells = []
        for key in keys:
            value = row.get(key, "")
            cells.append(f"{value:.1f}" if isinstance(value, float) else str(value))
        print("| " + " | ".join(cells) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
