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
    assert args.play is None
    assert args.audio_sink is None
    assert args.replaygain == "auto"
    assert args.replaygain_source == "beets"
    assert args.preamp == 0.0


def test_playback() -> None:
    args = parse_args(
        [
            "--play",
            "album:Foo",
            "--audio-sink",
            "pipewiresink",
            "--replaygain",
            "track",
            "--replaygain-source",
            "tags",
            "--preamp",
            "-3",
        ]
    )
    assert args.play == "album:Foo"
    assert args.audio_sink == "pipewiresink"
    assert args.replaygain == "track"
    assert args.replaygain_source == "tags"
    assert args.preamp == -3.0


def test_measuring() -> None:
    args = parse_args(["--measure", "--autoscroll", "covers", "--scale", "10"])
    assert args.measure
    assert args.autoscroll == "covers"
    assert args.scale == 10
