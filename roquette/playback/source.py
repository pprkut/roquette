# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""What the engine plays and the states it's in, without GStreamer."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from roquette.playback.gain import Mode, ReplayGain


class State(Enum):
    STOPPED = "stopped"
    PAUSED = "paused"
    PLAYING = "playing"


@dataclass(frozen=True, slots=True, eq=False)
class Source:
    """A file to play and how loud."""

    path: Path
    # From the beets database; None (or empty) uses the stream's tags.
    replaygain: ReplayGain | None = None
    mode: Mode = Mode.TRACK
    # For display; the engine doesn't need it.
    length: float = 0.0
