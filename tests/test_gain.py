# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""ReplayGain values and the gain they give, without GStreamer."""

import pytest

from roquette.playback.gain import OFF, Mode, Origin, ReplayGain, apply, factor


def db(gain: float) -> float:
    return float(10 ** (gain / 20))


def test_apply() -> None:
    gains = ReplayGain(-6.0, 0.5, -3.0, 0.9)
    assert apply(gains, Mode.OFF) is OFF
    track = apply(gains, Mode.TRACK, preamp=2.0)
    assert track.factor == pytest.approx(db(-4))
    assert track.db == pytest.approx(-4.0)
    assert (track.used, track.gain, track.peak, track.limited) == (
        Mode.TRACK,
        -4.0,
        0.5,
        False,
    )
    # Clipping at a volume of 1 / (factor × peak).
    assert track.clip_volume == pytest.approx(1 / (db(-4) * 0.5))
    # Album mode with only the track's values, and the other way round.
    assert apply(ReplayGain(track_gain=-6.0), Mode.ALBUM).used is Mode.TRACK
    assert apply(ReplayGain(album_gain=-3.0), Mode.TRACK).used is Mode.ALBUM
    assert apply(ReplayGain(track_gain=-6.0), Mode.TRACK).clip_volume is None
    tags = apply(gains, Mode.TRACK, origin=Origin.TAGS)
    assert tags.origin is Origin.TAGS
    # A peak of 0 from an old import is no peak.
    assert apply(ReplayGain(-6.0, 0.0), Mode.TRACK).peak is None


def test_factor() -> None:
    gains = ReplayGain(-6.0, 0.5, -3.0, 0.9)
    assert factor(gains, Mode.OFF) == 1.0
    assert factor(gains, Mode.TRACK) == pytest.approx(db(-6))
    assert factor(gains, Mode.ALBUM) == pytest.approx(db(-3))
    assert factor(gains, Mode.TRACK, preamp=18.0) == pytest.approx(2.0)
    assert factor(ReplayGain(track_gain=-6.0), Mode.ALBUM) == pytest.approx(db(-6))
    assert factor(ReplayGain(album_gain=-3.0), Mode.TRACK) == pytest.approx(db(-3))
    assert factor(None, Mode.TRACK, fallback=-6.0) == pytest.approx(db(-6))
    assert factor(ReplayGain(), Mode.TRACK, preamp=6.0) == pytest.approx(db(6))


def test_replaygain_from_fields() -> None:
    fields = {"rg_track_gain": -6.5, "rg_track_peak": 0.98, "rg_album_gain": None}
    assert ReplayGain.from_fields(fields) == ReplayGain(-6.5, 0.98, None, None)
    assert ReplayGain.from_fields({}).empty
