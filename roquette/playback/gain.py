# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""ReplayGain values and the gain they give, without GStreamer.

The values come from the beets database (the `replaygain` plugin's
`rg_*` fields) or, for files beets has none for, from the stream's tags.
Which of track and album gain applies is decided per track by whoever
queues it (docs/PLAN.md, "Playback notes"); the engine applies the
result.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Mode(Enum):
    OFF = "off"
    TRACK = "track"
    ALBUM = "album"


@dataclass(frozen=True, slots=True)
class ReplayGain:
    """Gains in dB, peaks as linear sample values (1.0 is full scale)."""

    track_gain: float | None = None
    track_peak: float | None = None
    album_gain: float | None = None
    album_peak: float | None = None

    @property
    def empty(self) -> bool:
        return self.track_gain is None and self.album_gain is None

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> "ReplayGain":
        """From a beets item's (or a row's) `rg_*` fields; unset is None."""
        return cls(
            _number(fields.get("rg_track_gain")),
            _number(fields.get("rg_track_peak")),
            _number(fields.get("rg_album_gain")),
            _number(fields.get("rg_album_peak")),
        )


class Origin(Enum):
    """Where the values of an applied gain came from."""

    # The beets database (the `replaygain` plugin's fields).
    BEETS = "beets"
    # The stream's tags.
    TAGS = "tags"
    # Neither had values: the fallback gain.
    FALLBACK = "fallback"
    # ReplayGain is off.
    OFF = "off"


@dataclass(frozen=True, slots=True)
class AppliedGain:
    """The ReplayGain applied to a track, as the engine reports it.

    For the volume control (docs/PLAN.md, "UI concept"): the factor the
    samples are multiplied with, how it came about, and where the track
    would start to clip.
    """

    # The linear factor applied to the samples.
    factor: float
    # The mode asked for, and the values used: track or album gain, or
    # None for the fallback gain or with ReplayGain off. Album mode uses
    # the track's values where the album's are missing, and the other way
    # round.
    mode: Mode
    used: Mode | None
    origin: Origin
    # The gain in dB the values (or the fallback) give with the pre-amp,
    # before the peak limits it; 0 with ReplayGain off.
    gain: float
    preamp: float
    # The peak of the values used (linear, 1.0 is full scale), if known.
    peak: float | None
    # Whether the peak lowered the gain, so the track doesn't clip.
    limited: bool

    @property
    def db(self) -> float:
        """The factor applied, in dB."""
        return 20 * math.log10(self.factor)

    @property
    def clip_volume(self) -> float | None:
        """The linear volume above which the track's peak would clip, if
        the peak is known (the gain and the volume multiply)."""
        if self.peak is None or self.peak <= 0:
            return None
        return 1 / (self.factor * self.peak)


OFF = AppliedGain(1.0, Mode.OFF, None, Origin.OFF, 0.0, 0.0, None, False)


def apply(
    replaygain: ReplayGain | None,
    mode: Mode,
    preamp: float = 0.0,
    fallback: float = 0.0,
    origin: Origin = Origin.BEETS,
) -> AppliedGain:
    """The gain to apply, and how it came about.

    Album mode falls back to the track's values and track mode to the
    album's when the preferred ones are missing; without either, the
    `fallback` gain in dB applies. The pre-amp in dB is added to the gain,
    and the result is lowered where the peak would clip. `origin` is
    where `replaygain` came from.
    """
    if mode is Mode.OFF:
        return OFF
    gain, peak, used = None, None, None
    if replaygain is not None:
        track = (replaygain.track_gain, replaygain.track_peak, Mode.TRACK)
        album = (replaygain.album_gain, replaygain.album_peak, Mode.ALBUM)
        first, second = (album, track) if mode is Mode.ALBUM else (track, album)
        gain, peak, used = first if first[0] is not None else second
    if gain is None:
        total = fallback + preamp
        return AppliedGain(
            10 ** (total / 20), mode, None, Origin.FALLBACK, total, preamp, None, False
        )
    total = gain + preamp
    linear = 10 ** (total / 20)
    limited = False
    if peak is not None and peak > 0 and linear > 1 / peak:
        linear, limited = 1 / peak, True
    elif peak is not None and peak <= 0:
        peak = None
    return AppliedGain(linear, mode, used, origin, total, preamp, peak, limited)


def factor(
    replaygain: ReplayGain | None,
    mode: Mode,
    preamp: float = 0.0,
    fallback: float = 0.0,
) -> float:
    """The linear factor to multiply the samples with (see `apply`)."""
    return apply(replaygain, mode, preamp, fallback).factor


def _number(value: Any) -> float | None:
    # beets keeps unset fields as None; 0.0 from older imports is a real
    # value for a gain, but not for a peak, which the check above skips.
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None
