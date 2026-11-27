# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The playlist pane's rows: releases, their tracks, and the versions of
a track's recording when they're expanded.

The live playlist's entries, grouped into runs of the same release, with
the text the window has read for them. Versions are shown from M11 on
(docs/PLAN.md, "Recording versions"); M7 and M10 make the pane what the
plan has, and only rebuild what changed.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import NamedTuple

from roquette.library.index import TrackText
from roquette.modules.tracks import format_length
from roquette.queue import playlist


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
    # The entry's position in the live playlist.
    index: int = -1


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


def groups_for(
    entries: Sequence[playlist.Entry],
    text: Mapping[int, TrackText],
    current: playlist.Entry | None = None,
) -> tuple[list[Group], tuple[int, int] | None]:
    """The live playlist's entries in groups of a release (singletons of
    an artist together); and where the current entry is, as (group,
    entry). Entries without text yet show without it."""
    groups: list[Group] = []
    keys: list[tuple[int, str]] = []
    playing = None
    empty = TrackText("", "", "", "", 0, 0, 0.0)
    for n, entry in enumerate(entries):
        item = entry.item
        track = text.get(item.id, empty)
        key = (item.album_id, "" if item.album_id else track.albumartist)
        if not keys or keys[-1] != key:
            keys.append(key)
            groups.append(Group(track.album, track.albumartist, 0, []))
        group = groups[-1]
        if entry is current:
            playing = (len(groups) - 1, len(group.entries))
        group.entries.append(
            Entry(track.track, track.title, track.artist, track.length, index=n)
        )
    return groups, playing


class PlaylistPane:
    def __init__(
        self, groups: list[Group], playing: tuple[int, int] | None = None
    ) -> None:
        self.groups = groups
        # The entry playing, as (group, entry).
        self.playing = playing
        self.current = -1
        self._refs: list[_Ref] = []
        self._layout()

    def set_groups(
        self, groups: list[Group], playing: tuple[int, int] | None = None
    ) -> None:
        """Show other groups; the selected row stays where it was, if
        there are as many rows."""
        self.groups = groups
        self.playing = playing
        self._layout()
        if self.current >= len(self._refs):
            self.current = -1

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

    def _layout(self) -> None:
        refs = []
        for g, group in enumerate(self.groups):
            refs.append(_Ref(g, -1, -1))
            for e, entry in enumerate(group.entries):
                refs.append(_Ref(g, e, -1))
                if entry.expanded:
                    refs.extend(_Ref(g, e, v) for v in range(len(entry.versions)))
        self._refs = refs
