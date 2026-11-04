# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The recording index: which tracks are versions of the same recording.

Tracks are versions of one recording when they share `mb_trackid`
(docs/PLAN.md, "Recording versions"). Only recordings with more than one
version are indexed, about 1,000 of the dev library's 11,800 tracks; the
rest are their own group and cost nothing. The index maps each of those
tracks to the ids of all versions, best first.

Prototype of S3, which measures it (scripts/measure-library.py).
"""

from collections.abc import Iterable
from typing import NamedTuple

import beets.library

# Formats as beets (mediafile) names them.
LOSSLESS = frozenset({"FLAC", "ALAC", "WAVE", "AIFF", "APE", "WavPack", "TTA", "DSF"})


class Version(NamedTuple):
    id: int
    recording: str
    format: str
    bitdepth: int
    samplerate: int
    bitrate: int
    length: float


def rank(version: Version) -> tuple[bool, int, int, int, float]:
    """The sort key of the best version first: lossless over lossy, then
    bit depth, sample rate, bitrate and length, each highest first."""
    return (
        version.format not in LOSSLESS,
        -version.bitdepth,
        -version.samplerate,
        -version.bitrate,
        -version.length,
    )


_VERSIONS = """
    SELECT id, mb_trackid, format, bitdepth, samplerate, bitrate, length
    FROM items
    WHERE mb_trackid IN (
        SELECT mb_trackid FROM items
        WHERE mb_trackid != ''
        GROUP BY mb_trackid HAVING count(*) > 1
    )
"""


def load_versions(library: beets.library.Library) -> list[Version]:
    """The tracks of recordings with more than one version; blocking."""
    with library.transaction() as tx:
        return [
            Version(
                row[0],
                row[1],
                row[2] or "",
                row[3] or 0,
                row[4] or 0,
                row[5] or 0,
                row[6] or 0.0,
            )
            for row in tx.query(_VERSIONS)
        ]


def index_versions(versions: Iterable[Version]) -> dict[int, tuple[int, ...]]:
    """Each track id to the ids of its recording's versions, best first.

    The tuple is shared by the versions of one recording.
    """
    by_recording: dict[str, list[Version]] = {}
    for version in versions:
        by_recording.setdefault(version.recording, []).append(version)
    index: dict[int, tuple[int, ...]] = {}
    for group in by_recording.values():
        if len(group) < 2:
            continue
        ids = tuple(v.id for v in sorted(group, key=rank))
        for track_id in ids:
            index[track_id] = ids
    return index
