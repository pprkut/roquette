# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""What the engine plays, what it asks for and what it reports, without
GStreamer."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from roquette.playback.gain import Mode, ReplayGain


class State(Enum):
    STOPPED = "stopped"
    PAUSED = "paused"
    PLAYING = "playing"


@dataclass(frozen=True, slots=True, eq=False)
class Source:
    """A file to play and how loud.

    Sources are compared by identity: the same file twice in a row is two
    sources.
    """

    path: Path
    # From the beets database; None (or empty) uses the stream's tags.
    replaygain: ReplayGain | None = None
    mode: Mode = Mode.TRACK
    # For display; the engine doesn't need it.
    length: float = 0.0


class Upcoming(Protocol):
    """What plays after a source: the live playlist (M3), or a list.

    Called on GStreamer's streaming threads as well as the event loop's,
    without the engine's lock held: it must be thread-safe, mustn't block
    and mustn't call the engine.
    """

    def next_source(self, after: Source) -> Source | None:
        """The source that plays after `after`, or None to stop there."""
        ...


class Problem(Enum):
    """Why a source couldn't be played."""

    # The file doesn't exist.
    MISSING = "missing"
    # It exists, but can't be read: permissions, a directory, I/O errors.
    UNREADABLE = "unreadable"
    # Not audio, or in a format there's no GStreamer plugin for.
    UNSUPPORTED = "unsupported"
    # Audio whose data is broken.
    DECODE = "decode"
    # Anything else GStreamer reports.
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PlaybackError:
    # The source that failed, if the engine could tell.
    source: Source | None
    problem: Problem
    # GStreamer's message.
    message: str
    # What plays instead (the source after the failed one), or None when
    # playback stops (nothing after it, or too many failures in a row).
    skipped_to: Source | None = None


class SourceList:
    """A fixed list of sources, played in order: the engine's provider
    until the live playlist (M3)."""

    def __init__(self, sources: tuple[Source, ...] = ()) -> None:
        self.sources = sources

    def __len__(self) -> int:
        return len(self.sources)

    def index(self, source: Source) -> int | None:
        for n, candidate in enumerate(self.sources):
            if candidate is source:
                return n
        return None

    def next_source(self, after: Source) -> Source | None:
        # The tuple is never changed, only replaced: safe from any thread.
        sources = self.sources
        for n, candidate in enumerate(sources):
            if candidate is after:
                return sources[n + 1] if n + 1 < len(sources) else None
        return None

    def offset(self, source: Source, offset: int) -> Source | None:
        """The source `offset` places from `source`, if any."""
        index = self.index(source)
        if index is None or not 0 <= index + offset < len(self.sources):
            return None
        return self.sources[index + offset]
