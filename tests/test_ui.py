# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The .slint files compile; showing them needs a display (from M5 on)."""

import pytest

from roquette.ui.window import load_ui, volume_factor


@pytest.mark.parametrize("name", ["main_window", "empty_window"])
def test_compiles(name: str) -> None:
    load_ui(name)


def test_volume_factor() -> None:
    assert volume_factor(0) == 0.0
    assert volume_factor(100) == 1.0
    assert volume_factor(50) == 0.125
    assert volume_factor(150) == 1.0
