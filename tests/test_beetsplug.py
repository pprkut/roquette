# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

from beets import config

from beetsplug.roquette import RoquettePlugin


def test_library_is_read_only_by_default() -> None:
    plugin = RoquettePlugin()
    assert plugin.config["library"]["writable"].get(bool) is False
    assert config["roquette"]["library"]["writable"].get(bool) is False
