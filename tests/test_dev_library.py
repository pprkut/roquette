# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The development copy of the beets data (`just dev-library`).

Skipped where there is none: in CI and in cloud sessions.
"""

from collections.abc import Iterator
from pathlib import Path

import beets.library
import pytest
from beets import config

DEV = Path(__file__).resolve().parent.parent / "dev" / "beets"

pytestmark = pytest.mark.skipif(
    not (DEV / "config.yaml").exists(), reason="no dev library (just dev-library)"
)


@pytest.fixture
def dev_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("BEETSDIR", str(DEV))
    config.clear()
    config.read()
    yield
    config.clear()
    config.read(user=False)


@pytest.mark.usefixtures("dev_config")
def test_dev_library_is_the_copy() -> None:
    library_path = Path(config["library"].as_filename())
    assert library_path.is_relative_to(DEV)
    assert config["roquette"]["library"]["writable"].get(bool) is True
    assert config["import"]["write"].get(bool) is False
    assert config["import"]["move"].get(bool) is False

    library = beets.library.Library(str(library_path))
    assert len(library.items()) > 0
    assert len(library.albums()) > 0
