# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The beets config, roquette's settings in it, and the plugins.

The config is found as `beet` finds it: the directory in `$BEETSDIR`,
or beets' default (`~/.config/beets`), and a file given on the command
line (`--beets-config`, like `beet -c`) over it.

roquette loads only the plugins of the config that add something it
uses (`roquette.beets_plugins`, by default the roquette plugin, `types`,
`inline`, `playlist` and `smartplaylist`); the others are `beet`'s
business and together cost about 50 MiB (docs/decisions/0003-library.md).
Plugins must be loaded before anything parses a query or reads a typed
field, as beets computes the field types once.
"""

from dataclasses import dataclass
from pathlib import Path

import confuse
from beets import config, plugins
from beets.util import cached_classproperty

from beetsplug.roquette import DEFAULTS
from roquette.library import LibraryError


@dataclass(frozen=True, slots=True)
class LibraryConfig:
    """Where the library is, and whether roquette may write to it."""

    config_dir: Path
    database: Path
    directory: Path
    writable: bool


def read_config(config_file: Path | None = None) -> LibraryConfig:
    """Read the beets config, `config_file` over the user's."""
    try:
        config.clear()
        config.read()
        if config_file is not None:
            if not config_file.is_file():
                raise LibraryError(f"no beets config at {config_file}")
            config.set_file(str(config_file))
        # The plugin adds the same when it's loaded; without it, the
        # settings still have their defaults.
        config["roquette"].add(DEFAULTS)
        # As beets adds it when it loads plugins.
        config.add({"disabled_plugins": []})
        return LibraryConfig(
            Path(config.config_dir()),
            Path(config["library"].as_filename()),
            Path(config["directory"].as_filename()),
            config["roquette"]["library"]["writable"].get(bool),
        )
    except confuse.ConfigError as error:
        raise LibraryError(f"the beets config can't be read: {error}") from error


def plugin_names() -> list[str]:
    """The plugins of the config that roquette loads, in the config's order."""
    try:
        wanted = set(config["roquette"]["beets_plugins"].as_str_seq())
        enabled = config["plugins"].as_str_seq()
        disabled = set(config["disabled_plugins"].as_str_seq())
    except confuse.ConfigError as error:
        raise LibraryError(f"the beets config can't be read: {error}") from error
    names: list[str] = []
    for name in enabled:
        if name in wanted and name not in disabled and name not in names:
            names.append(name)
    return names


def load_plugins() -> list[str]:
    """Load the plugins roquette uses (once per process); their names."""
    names = plugin_names()
    # beets loads the plugins `plugins` names (and adds `musicbrainz` for
    # its deprecated `musicbrainz.enabled`). The setting is only changed
    # in memory, and then says which plugins are loaded.
    config["plugins"].set(names)
    if "musicbrainz" not in names:
        disabled = config["disabled_plugins"].as_str_seq()
        config["disabled_plugins"].set([*disabled, "musicbrainz"])
    plugins.load_plugins()
    # Field types and queries computed before (in tests) are stale.
    cached_classproperty.cache.clear()
    return [plugin.name for plugin in plugins.find_plugins()]


def check_development_copy() -> None:
    """Refuse a config that isn't a development copy (`just dev-library`,
    `just fake-library`): for the scripts that open the library with
    beets' own `Library`, which updates it to the installed beets, or
    write to it."""
    writable = config["roquette"]["library"]["writable"]
    if not (writable.exists() and writable.get(bool)):
        raise LibraryError(
            f"{config.config_dir()} isn't a development copy of the beets data; "
            "run with BEETSDIR=dev/beets (`just dev-library`)"
        )
