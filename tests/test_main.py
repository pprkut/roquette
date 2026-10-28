# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

import pytest

from roquette import __version__
from roquette.__main__ import main, parse_args


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"roquette {__version__}\n"


def test_defaults() -> None:
    args = parse_args([])
    assert args.module == "tracks"
    assert args.scale == 1
    assert args.autoscroll is None
    assert not args.measure


def test_measuring() -> None:
    args = parse_args(["--measure", "--autoscroll", "covers", "--scale", "10"])
    assert args.measure
    assert args.autoscroll == "covers"
    assert args.scale == 10
