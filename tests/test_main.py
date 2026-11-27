# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

from pathlib import Path

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
    assert args.beets_config is None
    assert args.module == "tracks"
    assert args.scale == 1
    assert args.autoscroll is None
    assert not args.measure
    assert args.play is None
    assert args.audio_sink is None
    assert args.replaygain == "auto"
    assert args.replaygain_source == "beets"
    assert args.preamp == 0.0
    assert not args.list_outputs
    assert (args.audio_buffer, args.audio_latency) == (400, 100)


def test_beets_config() -> None:
    assert parse_args(["--beets-config", "x.yaml"]).beets_config == Path("x.yaml")


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
            "--audio-buffer",
            "200",
            "--audio-latency",
            "10",
        ]
    )
    assert args.play == "album:Foo"
    assert args.audio_sink == "pipewiresink"
    assert args.replaygain == "track"
    assert args.replaygain_source == "tags"
    assert args.preamp == -3.0
    assert (args.audio_buffer, args.audio_latency) == (200, 10)


@pytest.mark.parametrize(
    "options",
    [
        ["--audio-buffer", "0"],
        ["--audio-buffer", "-5"],
        ["--audio-latency", "nan"],
        ["--audio-latency", "inf"],
        ["--audio-latency", "ten"],
        ["--audio-buffer", "50", "--audio-latency", "100"],
    ],
)
def test_audio_sizes_checked(
    options: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        parse_args(options)
    assert "--audio-" in capsys.readouterr().err


def test_list_outputs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from roquette.playback import outputs

    found = [
        outputs.Output("Speakers", "pulsesink device=speakers", True),
        outputs.Output("USB DAC", 'pulsesink device="usb dac"'),
    ]
    monkeypatch.setattr(outputs, "list_outputs", lambda: found)
    assert main(["--list-outputs"]) == 0
    assert capsys.readouterr().out == (
        "Speakers (default)\n"
        "    --audio-sink 'pulsesink device=speakers'\n"
        "USB DAC\n"
        "    --audio-sink 'pulsesink device=\"usb dac\"'\n"
    )
    monkeypatch.setattr(outputs, "list_outputs", lambda: [])
    assert main(["--list-outputs"]) == 0
    assert capsys.readouterr().out == "No audio outputs found.\n"


def test_measuring() -> None:
    args = parse_args(["--measure", "--autoscroll", "covers", "--scale", "10"])
    assert args.measure
    assert args.autoscroll == "covers"
    assert args.scale == 10
