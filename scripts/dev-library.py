#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Make the development copy of the beets config and library.

The live beets data is read-only for development (docs/PLAN.md,
"Development data"). This copies the beets config and database into
dev/beets/ and changes the copy so that

- `library:` points at the copied database,
- `pluginpath:` finds the roquette plugin in this checkout, which is
  enabled, with `roquette.library.writable` set,
- beets doesn't write, move or copy music files (`write`, `move`,
  `copy` off), as the music files are still the live ones.

The live database is opened read-only and copied with SQLite's online
backup, so the copy is consistent even while `beet` is running.

The copy is migrated to the installed beets' schema right away.

Use the copy with `BEETSDIR=dev/beets`, for roquette and for `beet`.
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

import beets.library
import yaml

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("~/.config/beets").expanduser(),
        help="the live beets config directory (default: %(default)s)",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=REPO / "dev" / "beets",
        help="where to put the copy (default: %(default)s)",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing copy")
    args = parser.parse_args()

    source: Path = args.source.resolve()
    target: Path = args.target.resolve()
    config_path = source / "config.yaml"

    with config_path.open(encoding="utf-8") as config_file:
        config: dict[str, Any] = yaml.safe_load(config_file) or {}

    if "include" in config:
        print("`include:` in the beets config isn't supported", file=sys.stderr)
        return 1

    library = Path(config.get("library", source / "library.db")).expanduser()
    if not library.is_absolute():
        library = source / library
    library = library.resolve()

    # Before anything is removed: the copy and the live data must not
    # overlap in either direction.
    for live_path in (source, library):
        if live_path.is_relative_to(target) or target.is_relative_to(live_path):
            print(f"the copy must be apart from {live_path}", file=sys.stderr)
            return 1

    if target.exists():
        if not args.force:
            print(f"{target} exists; --force replaces it", file=sys.stderr)
            return 1
        shutil.rmtree(target)

    target.mkdir(parents=True)
    copy = target / "library.db"
    live = sqlite3.connect(f"{library.as_uri()}?mode=ro", uri=True)
    dev = sqlite3.connect(copy)
    with dev:
        live.backup(dev)
    dev.close()
    live.close()

    # Bring the copy to the installed beets' schema now, instead of at the
    # first `beet` command, and drop the backups beets makes before each
    # migration: the live database is the backup.
    beets.library.Library(str(copy))
    for backup in target.glob(f"{copy.name}-before-*.bak"):
        backup.unlink()

    config["library"] = str(copy)
    config["write"] = False
    importer = config.setdefault("import", {})
    importer.update({"write": False, "move": False, "copy": False})

    pluginpath = config.get("pluginpath", [])
    if isinstance(pluginpath, str):
        pluginpath = [pluginpath]
    config["pluginpath"] = [str(REPO / "beetsplug"), *pluginpath]
    plugins = config.get("plugins", [])
    if isinstance(plugins, str):
        plugins = plugins.split()
    if "roquette" not in plugins:
        plugins.append("roquette")
    config["plugins"] = plugins
    config.setdefault("roquette", {}).setdefault("library", {})["writable"] = True

    playlist_dir = config.get("playlist", {}).get("playlist_dir")
    if playlist_dir:
        playlists = target / "playlists"
        shutil.copytree(Path(playlist_dir).expanduser(), playlists)
        config["playlist"]["playlist_dir"] = str(playlists)

    with (target / "config.yaml").open("w", encoding="utf-8") as config_file:
        config_file.write(
            "# The development copy, made by `just dev-library` from\n"
            f"# {config_path}; edit or remake it freely.\n"
        )
        yaml.safe_dump(config, config_file, sort_keys=False, allow_unicode=True)

    print(f"Copied {library} to {copy}.")
    print(f"Use it with BEETSDIR={target.relative_to(Path.cwd(), walk_up=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
