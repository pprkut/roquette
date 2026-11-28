# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""The live playlist's model: changes, the play order, the current entry."""

import random

import pytest

from roquette.library.index import PlaylistItem
from roquette.queue.playlist import Entry, Playlist, Repeat, Shuffle, runs


def entry(id: int, album: int = 0, track: int = 0, disc: int = 1) -> Entry:
    return Entry(PlaylistItem(id, album, disc, track, f"/m/{id}.flac".encode()))


def ids(entries: object) -> list[int]:
    return [e.item.id for e in entries]  # type: ignore[attr-defined]


def playlist(*entries: Entry, seed: int = 1) -> Playlist:
    p = Playlist(random.Random(seed))
    p.append(entries)
    return p


def walk(p: Playlist, count: int, *, manual: bool = False) -> list[int]:
    """The ids that play from the current entry on."""
    played = []
    entry = p.current
    for _ in range(count):
        entry = p.following(entry, manual=manual)
        if entry is None:
            break
        played.append(entry.item.id)
    return played


def test_changes() -> None:
    a, b, c, d = (entry(n) for n in range(1, 5))
    p = playlist(a, b)
    p.insert(1, [c])
    assert ids(p) == [1, 3, 2]
    p.insert(99, [d])
    assert ids(p) == [1, 3, 2, 4]
    # The same track twice is two entries.
    twin = entry(1)
    p.insert(0, [twin])
    assert ids(p) == [1, 1, 3, 2, 4] and p.index(twin) == 0 and p.index(a) == 1
    assert p.remove([0, 2, 42]) == [twin, c]
    assert ids(p) == [1, 2, 4]
    p.clear()
    assert len(p) == 0


@pytest.mark.parametrize(
    ("indices", "to", "result", "at"),
    [
        ([0], 2, [2, 1, 3, 4], 1),
        ([3], 0, [4, 1, 2, 3], 0),
        ([0, 2], 4, [2, 4, 1, 3], 2),
        ([1, 3], 1, [1, 2, 4, 3], 1),
        ([2], 2, [1, 2, 3, 4], 2),
        ([], 1, [1, 2, 3, 4], 1),
    ],
)
def test_move(indices: list[int], to: int, result: list[int], at: int) -> None:
    p = playlist(*(entry(n) for n in range(1, 5)))
    assert p.move(indices, to) == at
    assert ids(p) == result


def test_following_and_preceding() -> None:
    a, b, c = (entry(n) for n in range(1, 4))
    p = playlist(a, b, c)
    assert walk(p, 5) == [1, 2, 3]
    p.current = b
    assert p.preceding(b) is a and p.preceding(a) is None
    p.repeat = Repeat.PLAYLIST
    assert walk(p, 4) == [3, 1, 2, 3]
    assert p.preceding(a) is c
    p.repeat = Repeat.TRACK
    assert walk(p, 2) == [2, 2]
    # Next goes on, and stops at the end.
    assert walk(p, 5, manual=True) == [3]
    with pytest.raises(ValueError, match="isn't in the playlist"):
        p.current = entry(9)


def test_play_next() -> None:
    a, b, c, x, y = (entry(n) for n in (1, 2, 3, 8, 9))
    p = playlist(a, b, c)
    # Nothing current: first.
    assert p.play_next([x]) == 0
    assert ids(p) == [8, 1, 2, 3]
    p.current = a
    assert p.play_next([y]) == 2
    assert ids(p) == [8, 1, 9, 2, 3]
    assert walk(p, 2) == [9, 2]


def test_removing_the_current_entry() -> None:
    """It stays current; what followed it follows it."""
    a, b, c, d = (entry(n) for n in range(1, 5))
    p = playlist(a, b, c, d)
    p.current = b
    p.remove([1])
    assert p.current is b and p.detached
    assert walk(p, 5) == [3, 4] and p.preceding(b) is a
    # What followed it removed too, and something before it.
    p.remove([0, 1])
    assert walk(p, 5) == [4] and p.preceding(b) is None
    # Added where it was: right after it.
    x = entry(8)
    p.insert(0, [x])
    assert walk(p, 5) == [8, 4]
    p.play_next([entry(9)])
    assert walk(p, 5) == [9, 8, 4]
    p.current = x
    assert not p.detached


def test_removing_the_last_current_entry() -> None:
    a, b = entry(1), entry(2)
    p = playlist(a, b)
    p.current = b
    p.remove([1])
    assert walk(p, 3) == []
    p.repeat = Repeat.PLAYLIST
    assert walk(p, 2) == [1, 1]
    p.repeat = Repeat.OFF
    p.append([entry(3)])
    assert walk(p, 3) == [3]


def test_replace_while_playing() -> None:
    a = entry(1)
    p = playlist(a, entry(2))
    p.current = a
    p.replace([entry(5), entry(6)])
    assert p.current is a and walk(p, 3) == [5, 6]
    # Repeat track doesn't repeat what isn't in the playlist any more.
    p.repeat = Repeat.TRACK
    assert walk(p, 1) == [5]


def test_moving_keeps_a_detached_entry_in_place() -> None:
    a, b, c, d = (entry(n) for n in range(1, 5))
    p = playlist(a, b, c, d)
    p.current = b
    p.remove([1])
    p.move([1], 0)
    assert ids(p) == [3, 1, 4]
    assert walk(p, 3) == [3, 1, 4]


def test_shuffle_tracks() -> None:
    entries = [entry(n) for n in range(1, 21)]
    p = playlist(*entries)
    p.current = entries[4]
    p.shuffle = Shuffle.TRACKS
    order = ids(p.order)
    # The current entry first, then all the others.
    assert order[0] == 5 and sorted(order) == list(range(1, 21))
    assert order != list(range(1, 21))
    assert ids(p) == list(range(1, 21))
    # Moving in the playlist leaves the play order.
    p.move([0], 10)
    assert ids(p.order) == order
    # Added after the current entry, anywhere after it.
    p.append([entry(99)])
    assert ids(p.order)[0] == 5 and 99 in ids(p.order)
    removed = p.remove([p.index(entries[7])])
    assert 8 not in ids(p.order) and removed == [entries[7]]
    p.shuffle = Shuffle.OFF
    assert p.order is p.entries


def test_shuffle_without_a_current_entry() -> None:
    p = playlist(*(entry(n) for n in range(1, 11)), seed=3)
    p.shuffle = Shuffle.TRACKS
    assert sorted(ids(p.order)) == list(range(1, 11))
    assert p.first() is p.order[0]


def test_shuffle_releases() -> None:
    """Runs of a release stay together and in order."""
    releases = [[entry(r * 10 + t, r, t) for t in range(1, 4)] for r in range(1, 6)]
    singles = [entry(91), entry(92)]
    p = playlist(*[e for r in releases for e in r], *singles)
    p.current = releases[2][1]
    p.shuffle = Shuffle.RELEASES
    order = ids(p.order)
    assert order[:3] == [31, 32, 33]
    blocks = [ids(b) for b in runs(p.order)]
    assert sorted(blocks) == sorted(
        [[r * 10 + t for t in range(1, 4)] for r in range(1, 6)] + [[91], [92]]
    )
    # A release added goes between the runs after the current one's.
    p.append([entry(61, 6, 1), entry(62, 6, 2)])
    order = ids(p.order)
    at = order.index(61)
    assert at >= 3 and order[at + 1] == 62
    assert order[at - 1] % 10 == 3 or order[at - 1] > 90
    # Played next: right after the current entry, inside its release.
    p.play_next([entry(70)])
    assert ids(p.order)[:3] == [31, 32, 70]


def test_shuffle_with_a_detached_entry() -> None:
    entries = [entry(n) for n in range(1, 7)]
    p = playlist(*entries)
    p.current = entries[2]
    p.remove([2])
    p.shuffle = Shuffle.TRACKS
    assert sorted(walk(p, 10)) == [1, 2, 4, 5, 6]
    following = walk(p, 1)[0]
    # In the playlist's order again: on from the entry that followed it.
    p.shuffle = Shuffle.OFF
    rest = [1, 2, 4, 5, 6]
    assert walk(p, 10) == rest[rest.index(following) :]


def test_album_gain() -> None:
    """Next to another track of its release, in its order."""
    a1, a2, a3 = entry(1, 7, 1), entry(2, 7, 2), entry(3, 7, 3)
    b1 = entry(4, 8, 1)
    single = entry(5, 0, 1)
    other_disc = entry(6, 7, 1, disc=2)
    p = playlist(a1, a2, b1, a3, single, entry(9, 0, 2), other_disc)
    assert [p.album_gain(e) for e in p] == [True, True, False, False] + [False] * 3
    p.move([3], 2)
    assert [p.album_gain(e) for e in p][:4] == [True, True, True, False]
    # Backwards isn't the release's order; the next disc is.
    p2 = playlist(a2, a1)
    assert not p2.album_gain(a1)
    p3 = playlist(a3, other_disc)
    assert p3.album_gain(a3) and p3.album_gain(other_disc)
