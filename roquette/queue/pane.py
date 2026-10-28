# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The playlist pane's rows: releases, their tracks, and the versions of
a track's recording when they're expanded.

S1's placeholder: the entries are made up, and tracks dropped on the pane
are added as entries without versions. M3 replaces the entries with the
live playlist; the layout stays a view model like this.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import NamedTuple

from roquette.library.index import Track
from roquette.modules.tracks import format_length


class RowKind(IntEnum):
    RELEASE = 0
    TRACK = 1
    VERSION = 2


@dataclass(slots=True)
class Version:
    release: str
    format: str
    length: float


@dataclass(slots=True)
class Entry:
    number: int
    title: str
    artist: str
    length: float
    # 0–1 in steps of 0.1 (half stars), as `fmps_rating`.
    rating: float | None = None
    versions: list[Version] = field(default_factory=list)
    chosen: int = 0
    expanded: bool = False


@dataclass(slots=True)
class Group:
    title: str
    albumartist: str
    year: int
    entries: list[Entry]


class Row(NamedTuple):
    kind: RowKind
    title: str
    detail: str
    number: str
    rating: str
    versions: int
    expanded: bool
    chosen: bool
    playing: bool


# Where a row comes from: group, entry (-1 for a release) and version (-1
# for anything but a version).
class _Ref(NamedTuple):
    group: int
    entry: int
    version: int


def stars(rating: float | None) -> str:
    if rating is None:
        return ""
    halves = round(rating * 10)
    return "★" * (halves // 2) + "½" * (halves % 2) + "☆" * ((10 - halves) // 2)


def placeholder_groups() -> list[Group]:
    """A few entries to show multi-line rows and versions with."""
    side = "The Dark Side of the Moon"
    return [
        Group(
            side,
            "Pink Floyd",
            1973,
            [
                Entry(1, "Speak to Me", "Pink Floyd", 67, 0.8),
                Entry(2, "Breathe (In the Air)", "Pink Floyd", 169, 0.9),
                Entry(3, "On the Run", "Pink Floyd", 225, 0.7),
                Entry(
                    4,
                    "Time",
                    "Pink Floyd",
                    413,
                    1.0,
                    [
                        Version(side, "FLAC 16/44.1", 413),
                        Version(f"{side} (2011 remaster)", "FLAC 24/96", 421),
                        Version("Echoes: The Best of Pink Floyd", "MP3 320", 397),
                    ],
                ),
                Entry(5, "The Great Gig in the Sky", "Pink Floyd", 283, 0.9),
            ],
        ),
        Group(
            "Selected Ambient Works 85–92",
            "Aphex Twin",
            1992,
            [
                Entry(1, "Xtal", "Aphex Twin", 291, 0.8),
                Entry(2, "Tha", "Aphex Twin", 541, 0.6),
            ],
        ),
    ]


class PlaylistPane:
    def __init__(self, groups: list[Group]) -> None:
        self.groups = groups
        # The entry playing, as (group, entry).
        self.playing: tuple[int, int] | None = (0, 1) if groups else None
        self.current = -1
        self._refs: list[_Ref] = []
        self._layout()

    def __len__(self) -> int:
        return len(self._refs)

    def row(self, index: int) -> Row:
        ref = self._refs[index]
        group = self.groups[ref.group]
        if ref.entry < 0:
            detail = group.albumartist
            if group.year:
                detail += f" · {group.year}"
            return Row(
                RowKind.RELEASE, group.title, detail, "", "", 0, False, False, False
            )
        entry = group.entries[ref.entry]
        if ref.version >= 0:
            version = entry.versions[ref.version]
            return Row(
                RowKind.VERSION,
                version.release,
                f"{version.format} · {format_length(version.length)}",
                "",
                "",
                0,
                False,
                ref.version == entry.chosen,
                False,
            )
        length = entry.versions[entry.chosen].length if entry.versions else entry.length
        detail = format_length(length)
        if entry.artist != group.albumartist:
            detail = f"{entry.artist} · {detail}"
        return Row(
            RowKind.TRACK,
            entry.title,
            detail,
            str(entry.number) if entry.number else "",
            stars(entry.rating),
            len(entry.versions),
            entry.expanded,
            False,
            self.playing == (ref.group, ref.entry),
        )

    def toggle(self, index: int) -> tuple[int, int]:
        """Expand or collapse the versions of the track in row `index`.

        Returns where rows were inserted (a positive count) or removed (a
        negative one).
        """
        ref = self._refs[index]
        entry = self.groups[ref.group].entries[ref.entry]
        entry.expanded = not entry.expanded
        self._layout()
        count = len(entry.versions) if entry.expanded else -len(entry.versions)
        if self.current > index:
            self.current += count
        return index + 1, count

    def choose(self, index: int) -> list[int]:
        """Play the version in row `index`; returns the rows that changed."""
        ref = self._refs[index]
        entry = self.groups[ref.group].entries[ref.entry]
        track_row = index - ref.version - 1
        changed = [track_row, track_row + 1 + entry.chosen, index]
        entry.chosen = ref.version
        return changed

    def navigate(self, key: str) -> tuple[int, tuple[int, int] | None]:
        """Moves the selection between tracks (up, down) or expands (right)
        and collapses (left) the selected one's versions.

        Returns the selected row and, if versions were expanded or
        collapsed, what `toggle` returns.
        """
        tracks = [
            i for i, ref in enumerate(self._refs) if ref.entry >= 0 and ref.version < 0
        ]
        if not tracks:
            return self.current, None
        if self.current not in tracks:
            self.current = tracks[0]
            return self.current, None
        position = tracks.index(self.current)
        if key == "up":
            self.current = tracks[max(0, position - 1)]
        elif key == "down":
            self.current = tracks[min(len(tracks) - 1, position + 1)]
        elif key in ("left", "right"):
            ref = self._refs[self.current]
            entry = self.groups[ref.group].entries[ref.entry]
            if entry.versions and entry.expanded == (key == "left"):
                return self.current, self.toggle(self.current)
        return self.current, None

    def append(self, tracks: Sequence[Track]) -> tuple[int, int]:
        """Add tracks at the end; returns the first new row and the count."""
        start = len(self._refs)
        for track in tracks:
            last = self.groups[-1] if self.groups else None
            if last is None or (last.title, last.albumartist) != (
                track.album,
                track.albumartist,
            ):
                last = Group(track.album, track.albumartist, 0, [])
                self.groups.append(last)
            last.entries.append(
                Entry(track.track, track.title, track.artist, track.length)
            )
        self._layout()
        return start, len(self._refs) - start

    def _layout(self) -> None:
        refs = []
        for g, group in enumerate(self.groups):
            refs.append(_Ref(g, -1, -1))
            for e, entry in enumerate(group.entries):
                refs.append(_Ref(g, e, -1))
                if entry.expanded:
                    refs.extend(_Ref(g, e, v) for v in range(len(entry.versions)))
        self._refs = refs
