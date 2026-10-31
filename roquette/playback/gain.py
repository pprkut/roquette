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


def factor(
    replaygain: ReplayGain | None,
    mode: Mode,
    preamp: float = 0.0,
    fallback: float = 0.0,
) -> float:
    """The linear factor to multiply the samples with.

    Album mode falls back to the track's values and track mode to the
    album's when the preferred ones are missing; without either, the
    `fallback` gain in dB applies. The pre-amp in dB is added to the gain,
    and the result is lowered where the peak would clip.
    """
    if mode is Mode.OFF:
        return 1.0
    gain, peak = None, None
    if replaygain is not None:
        track = (replaygain.track_gain, replaygain.track_peak)
        album = (replaygain.album_gain, replaygain.album_peak)
        first, second = (album, track) if mode is Mode.ALBUM else (track, album)
        gain, peak = first if first[0] is not None else second
    if gain is None:
        return 10 ** ((fallback + preamp) / 20)
    linear = 10 ** ((gain + preamp) / 20)
    if peak is not None and peak > 0:
        linear = min(linear, 1 / peak)
    return linear


def _number(value: Any) -> float | None:
    # beets keeps unset fields as None; 0.0 from older imports is a real
    # value for a gain, but not for a peak, which the check above skips.
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None
