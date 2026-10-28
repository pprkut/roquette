# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Which ReplayGain applies to each track of what's queued.

S2's simple version of the rule in docs/PLAN.md ("Playback notes"): album
gain for tracks played next to another track of the same release, track
gain otherwise. M3 decides with the playlist's order and its versions.
"""

from collections.abc import Sequence

from roquette.library.index import Playable
from roquette.playback.gain import Mode
from roquette.playback.source import Source


def auto_modes(album_ids: Sequence[int]) -> list[Mode]:
    """The mode for each track, given the release (album id) of each.

    Singletons have the album id 0 and get track gain.
    """
    modes = []
    for n, album in enumerate(album_ids):
        before = n > 0 and album_ids[n - 1] == album
        after = n + 1 < len(album_ids) and album_ids[n + 1] == album
        modes.append(Mode.ALBUM if album and (before or after) else Mode.TRACK)
    return modes


def make_sources(
    playables: Sequence[Playable], mode: str = "auto", gain_source: str = "beets"
) -> list[Source]:
    """Sources for the engine.

    `mode` is "auto" (see `auto_modes`) or a `Mode`'s value; `gain_source`
    "beets" for the database's values or "tags" for the files'.
    """
    if mode == "auto":
        modes = auto_modes([p.album_id for p in playables])
    else:
        modes = [Mode(mode)] * len(playables)
    return [
        Source(p.path, p.replaygain if gain_source == "beets" else None, m, p.length)
        for p, m in zip(playables, modes, strict=True)
    ]
