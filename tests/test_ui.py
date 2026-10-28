# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The .slint files compile; showing them needs a display (from M5 on)."""

import pytest

from roquette.ui.window import load_ui


@pytest.mark.parametrize("name", ["main_window", "empty_window"])
def test_compiles(name: str) -> None:
    load_ui(name)
