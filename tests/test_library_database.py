# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The beets config, the plugins roquette loads, the read-only guard."""

import os
import sqlite3
from pathlib import Path

import beets.library
import pytest
from beets import config, plugins
from beets.dbcore import types
from beets.dbcore.db import DBAccessError

from roquette.library import LibraryError
from roquette.library.config import load_plugins, plugin_names, read_config
from roquette.library.database import open_reader, open_writer
from tests.conftest import Library


@pytest.mark.usefixtures("beets_dir")
def test_config_from_beetsdir(tmp_path: Path) -> None:
    settings = read_config()
    assert settings.config_dir == tmp_path / "beets"
    assert settings.database == tmp_path / "beets" / "library.db"
    assert settings.directory == tmp_path / "music"
    # The plugin's defaults, though it isn't loaded.
    assert settings.writable is False
    assert config["roquette"]["beets_plugins"].as_str_seq()[0] == "roquette"


def test_config_file_over_the_users(beets_dir: Path, tmp_path: Path) -> None:
    """As `beet -c`: the file's settings over the user's config."""
    other = tmp_path / "other.yaml"
    other.write_text(
        "library: /elsewhere/library.db\nroquette:\n  library:\n    writable: yes\n",
        encoding="utf-8",
    )
    settings = read_config(other)
    assert settings.database == Path("/elsewhere/library.db")
    assert settings.directory == tmp_path / "music"
    assert settings.writable is True


@pytest.mark.usefixtures("beets_dir")
def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(LibraryError, match="no beets config"):
        read_config(tmp_path / "missing.yaml")


def test_broken_config(beets_dir: Path) -> None:
    (beets_dir / "config.yaml").write_text("plugins: [\n", encoding="utf-8")
    with pytest.raises(LibraryError, match="can't be read"):
        read_config()


def test_plugin_names(beets_dir: Path) -> None:
    (beets_dir / "config.yaml").write_text(
        "plugins: [lyrics, inline, roquette, types, playlist, missing]\n"
        "disabled_plugins: [playlist]\n",
        encoding="utf-8",
    )
    read_config()
    # Only those that add something roquette uses, in the config's order.
    assert plugin_names() == ["inline", "roquette", "types"]
    config["roquette"]["beets_plugins"] = ["lyrics", "roquette"]
    assert plugin_names() == ["lyrics", "roquette"]


@pytest.mark.usefixtures("beets_dir")
def test_load_plugins() -> None:
    read_config()
    # `info` is in the config, but not for roquette.
    assert load_plugins() == ["roquette"]
    assert config["plugins"].as_str_seq() == ["roquette"]
    assert beets.library.Item._types["play_count"] is types.INTEGER
    assert "play_count" in beets.library.Item._queries
    assert [p.name for p in plugins.find_plugins()] == ["roquette"]


def test_reader_is_read_only(library: Library) -> None:
    reader = open_reader(read_config())
    item = reader.get_item(library.items["Something"])
    assert item is not None and item.album == "Abbey Road"
    item.title = "Something else"
    with pytest.raises(DBAccessError, match="readonly"):
        item.store()
    # The failed write doesn't hang the library (S3's beets bug).
    assert len(reader.items()) == len(library.items)


@pytest.mark.usefixtures("beets_dir")
def test_no_library(tmp_path: Path) -> None:
    with pytest.raises(LibraryError, match="no beets library"):
        open_reader(read_config())
    assert not (tmp_path / "beets" / "library.db").exists()


def test_outdated_library_is_refused(library: Library) -> None:
    """A database beets would update is refused before anything is
    written, beets' backups included."""
    connection = sqlite3.connect(library.path)
    with connection:
        connection.execute("DELETE FROM migrations WHERE name = 'relative_path'")
    connection.close()
    before = sorted(library.path.parent.iterdir())
    modified = library.path.stat().st_mtime_ns
    with pytest.raises(LibraryError, match="run any `beet` command"):
        open_reader(read_config())
    assert sorted(library.path.parent.iterdir()) == before
    assert library.path.stat().st_mtime_ns == modified


def test_library_without_a_new_column_is_refused(library: Library) -> None:
    connection = sqlite3.connect(library.path)
    with connection:
        connection.execute("ALTER TABLE items DROP COLUMN remixers")
    connection.close()
    with pytest.raises(LibraryError, match="run any `beet` command"):
        open_reader(read_config())


def test_no_writer_unless_writable(library: Library) -> None:
    assert open_writer(read_config()) is None


def test_writer_when_writable(library: Library, beets_dir: Path) -> None:
    with (beets_dir / "config.yaml").open("a", encoding="utf-8") as file:
        file.write("roquette:\n  library:\n    writable: yes\n")
    writer = open_writer(read_config())
    assert writer is not None
    item = writer.get_item(library.items["Something"])
    assert item is not None
    item["play_count"] = 1
    item.store()
    assert os.fsdecode(item.path).endswith("Something.flac")
