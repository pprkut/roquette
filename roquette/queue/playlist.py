# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The live playlist's model: entries, the play order and the current
entry, without the engine or the library.

An entry is a recording (docs/PLAN.md, "Recording versions"): the item
that was added, and whether it's pinned to that version. Tracks queued
as part of a whole release are pinned, so gapless playback and album
gain aren't broken by a version from another release; tracks added on
their own aren't. Which version plays is `Playlist.version`: until the
experiment of M11, the item that was added.

The play order is the playlist's, or a shuffled one: of the tracks, or
of the releases (runs of entries of the same release, whose order
stays). It's shuffled once, when shuffle is turned on; entries added
later go to random places after the current entry, and moving entries
in the playlist doesn't change it. "Play next" puts entries right after
the current one in both orders.

The current entry stays when it's removed, detached: what follows it
is what followed it where it was (`_ghost`).
"""

import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum

from roquette.library.index import PlaylistItem


class Repeat(Enum):
    OFF = "off"
    TRACK = "track"
    PLAYLIST = "playlist"


class Shuffle(Enum):
    OFF = "off"
    TRACKS = "tracks"
    RELEASES = "releases"


@dataclass(eq=False, slots=True)
class Entry:
    """A recording in the playlist; compared by identity, so the same
    track added twice is two entries."""

    item: PlaylistItem
    # Pinned to `item`: no other version is swapped in (M11).
    pinned: bool = False


def same_release(a: PlaylistItem, b: PlaylistItem) -> bool:
    return a.album_id != 0 and a.album_id == b.album_id


def in_order(a: PlaylistItem, b: PlaylistItem) -> bool:
    """`b` comes after `a` on the same release."""
    return same_release(a, b) and (a.disc, a.track) < (b.disc, b.track)


def runs(entries: Iterable[Entry]) -> list[list[Entry]]:
    """The entries in runs of the same release; other entries (and
    singletons) alone."""
    blocks: list[list[Entry]] = []
    for entry in entries:
        if blocks and same_release(blocks[-1][-1].item, entry.item):
            blocks[-1].append(entry)
        else:
            blocks.append([entry])
    return blocks


class Playlist:
    def __init__(self, rng: random.Random | None = None) -> None:
        self._entries: list[Entry] = []
        # The shuffled play order, None when it's the playlist's.
        self._order: list[Entry] | None = None
        self._shuffle = Shuffle.OFF
        self._random = rng or random.Random()
        self._current: Entry | None = None
        # The current entry was removed: where it was in the play order
        # (the position of the entry that followed it).
        self._ghost: int | None = None
        self.repeat = Repeat.OFF
        # Stop at the end of the current entry (docs/PLAN.md, M3); the
        # live playlist clears it once it has stopped.
        self.stop_after = False

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Entry]:
        return iter(self._entries)

    def __getitem__(self, index: int) -> Entry:
        return self._entries[index]

    def __contains__(self, entry: object) -> bool:
        return entry in self._entries

    @property
    def entries(self) -> Sequence[Entry]:
        """In the playlist's order; not to be changed."""
        return self._entries

    @property
    def order(self) -> Sequence[Entry]:
        """The play order; not to be changed."""
        return self._order if self._order is not None else self._entries

    def index(self, entry: Entry) -> int:
        """The entry's position in the playlist."""
        return self._entries.index(entry)

    # The current entry

    @property
    def current(self) -> Entry | None:
        return self._current

    @current.setter
    def current(self, entry: Entry | None) -> None:
        if entry is not None and entry not in self._entries:
            raise ValueError("the entry isn't in the playlist")
        self._current = entry
        self._ghost = None

    @property
    def detached(self) -> bool:
        """The current entry was removed from the playlist."""
        return self._ghost is not None

    def version(self, entry: Entry) -> PlaylistItem:
        """The version of the entry's recording that plays: the item that
        was added. M11 adds the best version for entries not pinned."""
        return entry.item

    # The play order

    def first(self) -> Entry | None:
        order = self.order
        return order[0] if order else None

    def following(self, entry: Entry | None, *, manual: bool = False) -> Entry | None:
        """What plays after `entry` (the first entry after None).

        `manual` is the Next button's: it goes on to the next entry when
        the track repeats, and stops at the end unless the playlist
        repeats.
        """
        if entry is None:
            return self.first()
        order = self.order
        if entry is self._current and self._ghost is not None:
            position = self._ghost
        else:
            if self.repeat is Repeat.TRACK and not manual:
                return entry
            position = order.index(entry) + 1
        if position < len(order):
            return order[position]
        if self.repeat is Repeat.PLAYLIST and order:
            return order[0]
        return None

    def preceding(self, entry: Entry) -> Entry | None:
        """What played before `entry`, for the Previous button."""
        order = self.order
        if entry is self._current and self._ghost is not None:
            position = self._ghost - 1
        else:
            position = order.index(entry) - 1
        if position >= 0:
            return order[position]
        if self.repeat is Repeat.PLAYLIST and order:
            return order[-1]
        return None

    def album_gain(self, entry: Entry) -> bool:
        """Whether the entry plays with album gain in the "auto" mode: it
        plays next to another track of its release, in the release's
        order (docs/PLAN.md, "Playback notes")."""
        order = self.order
        position = order.index(entry)
        item = self.version(entry)
        if position > 0 and in_order(self.version(order[position - 1]), item):
            return True
        return position + 1 < len(order) and in_order(
            item, self.version(order[position + 1])
        )

    # Shuffle

    @property
    def shuffle(self) -> Shuffle:
        return self._shuffle

    @shuffle.setter
    def shuffle(self, mode: Shuffle) -> None:
        if mode is self._shuffle:
            return
        anchor = self._anchor()
        self._shuffle = mode
        if mode is Shuffle.OFF:
            self._order = None
            self._restore_ghost(anchor)
            return
        # The current entry (and its release) first, the rest after it.
        current = self._current if self._ghost is None else None
        if mode is Shuffle.TRACKS:
            rest = [e for e in self._entries if e is not current]
            self._random.shuffle(rest)
            self._order = [current, *rest] if current is not None else rest
        else:
            blocks = runs(self._entries)
            first = [b for b in blocks if current is not None and current in b]
            others = [b for b in blocks if not first or b is not first[0]]
            self._random.shuffle(others)
            self._order = [e for block in first + others for e in block]
        if self._ghost is not None:
            self._ghost = 0

    def _anchor(self) -> Entry | None:
        """What follows a detached current entry, for a change of order."""
        if self._ghost is None or self._ghost >= len(self.order):
            return None
        return self.order[self._ghost]

    def _restore_ghost(self, anchor: Entry | None) -> None:
        if self._ghost is not None:
            order = self.order
            self._ghost = order.index(anchor) if anchor is not None else len(order)

    def _after_current(self) -> int:
        """The position in the play order after the current entry."""
        if self._ghost is not None:
            return self._ghost
        if self._current is None:
            return 0
        return self.order.index(self._current) + 1

    def _shuffle_in(self, entries: Sequence[Entry]) -> None:
        """Put new entries at random places after the current one."""
        order = self._order
        assert order is not None
        start = self._after_current()
        if self._shuffle is Shuffle.TRACKS:
            for entry in entries:
                order.insert(self._random.randint(start, len(order)), entry)
            return
        # Not inside a release: past the current one's, then between runs.
        while 0 < start < len(order) and same_release(
            order[start - 1].item, order[start].item
        ):
            start += 1
        for block in runs(entries):
            places = [
                n
                for n in range(start, len(order) + 1)
                if n in (start, len(order))
                or not same_release(order[n - 1].item, order[n].item)
            ]
            at = self._random.choice(places)
            order[at:at] = block

    # Changes

    def insert(self, index: int, entries: Sequence[Entry]) -> None:
        """Insert entries before position `index` of the playlist."""
        index = max(0, min(index, len(self._entries)))
        self._entries[index:index] = entries
        if self._order is not None:
            self._shuffle_in(entries)
        elif self._ghost is not None and index < self._ghost:
            self._ghost += len(entries)

    def append(self, entries: Sequence[Entry]) -> None:
        self.insert(len(self._entries), entries)

    def play_next(self, entries: Sequence[Entry]) -> int:
        """Insert entries to play right after the current one (first if
        there's none); returns their position in the playlist."""
        after = self._after_current()
        order = self.order
        if self._order is None:
            index = after
        elif self._ghost is None:
            current = self._current
            index = 0 if current is None else self._entries.index(current) + 1
        elif after < len(order):
            index = self._entries.index(order[after])
        else:
            index = len(self._entries)
        self._entries[index:index] = entries
        if self._order is not None:
            self._order[after:after] = entries
        return index

    def move(self, indices: Iterable[int], to: int) -> int:
        """Move the entries at `indices` before the one at `to` (the end
        for `len`), in their order; returns the position of the first.
        The shuffled play order stays."""
        positions = sorted({i for i in indices if 0 <= i < len(self._entries)})
        if not positions:
            return max(0, min(to, len(self._entries)))
        anchor = self._anchor() if self._order is None else None
        moving = [self._entries[i] for i in positions]
        moved = set(positions)
        rest = [e for n, e in enumerate(self._entries) if n not in moved]
        at = max(0, min(to, len(self._entries))) - sum(1 for i in positions if i < to)
        rest[at:at] = moving
        self._entries = rest
        if self._order is None:
            self._restore_ghost(anchor)
        return at

    def remove(self, indices: Iterable[int]) -> list[Entry]:
        """Remove the entries at `indices`; returns them. The current entry
        is detached if it's among them."""
        positions = {i for i in indices if 0 <= i < len(self._entries)}
        removed = [self._entries[i] for i in sorted(positions)]
        if not removed:
            return []
        gone = set(map(id, removed))
        order = self.order
        ghost = self._ghost
        if ghost is None and self._current is not None and id(self._current) in gone:
            ghost = order.index(self._current)
        if ghost is not None:
            ghost -= sum(1 for e in order[:ghost] if id(e) in gone)
        if self._order is not None:
            self._order = [e for e in self._order if id(e) not in gone]
        self._entries = [e for e in self._entries if id(e) not in gone]
        self._ghost = ghost
        return removed

    def clear(self) -> None:
        self.remove(range(len(self._entries)))

    def replace(self, entries: Sequence[Entry]) -> None:
        """The playlist becomes `entries`; a current entry is detached."""
        self.clear()
        self.append(entries)
