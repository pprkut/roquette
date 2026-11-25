# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Gapless playback and ReplayGain with real GStreamer, sample-exactly.

Generated FLAC files are played into an appsink as fast as it takes
them. The output must be every input sample, in order, with no gap and
no overlap at the transitions, each multiplied by its file's gain: a
gain that changes a sample early or late shows as a mismatch at the
boundary.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from roquette.playback.gain import OFF, AppliedGain, Mode, Origin, ReplayGain
from roquette.playback.source import Source
from tests.audio import RATE, Audio, check, db, make_flac, play


@pytest.fixture(scope="module")
def album(tmp_path_factory: pytest.TempPathFactory) -> list[Audio]:
    """Three tracks, with ReplayGain tags that differ from the database's."""
    directory = tmp_path_factory.mktemp("album")
    tags = [
        ReplayGain(-6.0, 0.25, -3.0, 0.5),
        ReplayGain(2.0, 0.25, -3.0, 0.5),
        ReplayGain(-1.0, 0.25, -3.0, 0.5),
    ]
    # Odd lengths, so a boundary is never on a round number of samples.
    lengths = [RATE * 2 + 123, RATE + 77, RATE // 2 + 1]
    return [
        make_flac(directory / f"{n}.flac", frames, n, tags[n])
        for n, frames in enumerate(lengths)
    ]


# What the beets database has for the album above.
DATABASE = [
    ReplayGain(-4.0, 0.25, -2.0, 0.5),
    ReplayGain(1.0, 0.25, -2.0, 0.5),
    ReplayGain(-7.0, 0.25, -2.0, 0.5),
]


def test_gapless_with_database_track_gain(album: list[Audio]) -> None:
    sources = [Source(a.path, DATABASE[n], Mode.TRACK) for n, a in enumerate(album)]
    output, recorder = play(sources)
    check(output, [(album[0], db(-4)), (album[1], db(1)), (album[2], db(-7))])
    assert recorder.indices(sources) == [0, 1, 2]
    assert recorder.errors == []


def test_gapless_with_database_album_gain(album: list[Audio]) -> None:
    sources = [Source(a.path, DATABASE[n], Mode.ALBUM) for n, a in enumerate(album)]
    output, _ = play(sources)
    check(output, [(a, db(-2)) for a in album])


def test_gain_from_tags(album: list[Audio]) -> None:
    """Without values from the database, the files' tags count."""
    sources = [
        Source(album[0].path, None, Mode.TRACK),
        Source(album[1].path, ReplayGain(), Mode.TRACK),
        Source(album[2].path, None, Mode.ALBUM),
    ]
    output, _ = play(sources)
    check(output, [(album[0], db(-6)), (album[1], db(2)), (album[2], db(-3))])


def test_database_and_tags_mixed(album: list[Audio]) -> None:
    sources = [
        Source(album[0].path, None, Mode.TRACK),
        Source(album[1].path, DATABASE[1], Mode.TRACK),
        Source(album[2].path, None, Mode.TRACK),
    ]
    output, _ = play(sources)
    check(output, [(album[0], db(-6)), (album[1], db(1)), (album[2], db(-1))])


def test_off_preamp_and_fallback(tmp_path: Path, album: list[Audio]) -> None:
    untagged = make_flac(tmp_path / "untagged.flac", RATE // 3, 7)
    sources = [
        Source(album[0].path, DATABASE[0], Mode.OFF),
        Source(untagged.path, None, Mode.TRACK),
        Source(album[2].path, DATABASE[2], Mode.TRACK),
    ]
    output, _ = play(sources, preamp=3.0, fallback=-5.0)
    check(output, [(album[0], 1.0), (untagged, db(-2)), (album[2], db(-4))])


def test_clipping_prevention(album: list[Audio]) -> None:
    """The gain stops where the peak would reach full scale."""
    loud = ReplayGain(track_gain=15.0, track_peak=0.25)
    output, _ = play([Source(album[1].path, loud, Mode.TRACK)])
    check(output, [(album[1], 4.0)])


def test_the_same_file_twice(album: list[Audio]) -> None:
    """Streams of the same file are told apart by their order."""
    sources = [
        Source(album[2].path, DATABASE[2], Mode.TRACK),
        Source(album[2].path, ReplayGain(track_gain=0.0), Mode.TRACK),
    ]
    output, recorder = play(sources)
    check(output, [(album[2], db(-7)), (album[2], 1.0)])
    assert recorder.indices(sources) == [0, 1]


def test_stream_ids_without_the_uri(
    album: list[Audio], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If stream-ids weren't the URI's hash, the order would still tell."""
    monkeypatch.setattr("roquette.playback.engine.stream_key", lambda uri: "?")
    sources = [Source(a.path, DATABASE[n]) for n, a in enumerate(album)]
    output, recorder = play(sources)
    check(output, [(album[0], db(-4)), (album[1], db(1)), (album[2], db(-7))])
    assert recorder.indices(sources) == [0, 1, 2]


def test_names_that_need_escaping(tmp_path: Path) -> None:
    """The stream's id is the hash of the URI GStreamer makes of the path."""
    weird = tmp_path / "Björk – It's (live) #1 100% [a&b].flac"
    audio = make_flac(weird, RATE // 4, 3)
    sources = [Source(weird, ReplayGain(track_gain=-6.0))]
    output, recorder = play(sources)
    check(output, [(audio, db(-6))])
    assert recorder.indices(sources) == [0]


def test_gain_reported(tmp_path: Path, album: list[Audio]) -> None:
    """Each track that starts comes with the gain applied to it."""
    untagged = make_flac(tmp_path / "untagged.flac", RATE // 3, 7)
    loud = ReplayGain(track_gain=15.0, track_peak=0.25, album_gain=-2.0)
    sources = [
        Source(album[0].path, DATABASE[0], Mode.ALBUM),
        Source(album[1].path, loud, Mode.TRACK),
        Source(album[2].path, None, Mode.TRACK),
        Source(untagged.path, None, Mode.ALBUM),
        Source(album[0].path, DATABASE[0], Mode.OFF),
    ]
    _, recorder = play(sources, preamp=1.0, fallback=-5.0)
    gains = [gain for _, gain in recorder.started]
    assert recorder.indices(sources) == [0, 1, 2, 3, 4]
    # The factors in dB, then the rest.
    assert [g.db for g in gains[:4]] == pytest.approx(
        [-1.0, 12.04, 0.0, -4.0], abs=0.01
    )
    assert [replace(g, factor=1.0) for g in gains[:4]] == [
        AppliedGain(1.0, Mode.ALBUM, Mode.ALBUM, Origin.BEETS, -1.0, 1.0, 0.5, False),
        # The peak limits +16 dB to +12 dB (4 × 0.25 is full scale).
        AppliedGain(1.0, Mode.TRACK, Mode.TRACK, Origin.BEETS, 16.0, 1.0, 0.25, True),
        AppliedGain(1.0, Mode.TRACK, Mode.TRACK, Origin.TAGS, 0.0, 1.0, 0.25, False),
        AppliedGain(1.0, Mode.ALBUM, None, Origin.FALLBACK, -4.0, 1.0, None, False),
    ]
    assert gains[1].factor == 4.0
    assert gains[4] is OFF
