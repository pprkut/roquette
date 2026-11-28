# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

from roquette.library.index import PlaylistItem, TrackText
from roquette.queue import playlist
from roquette.queue.pane import (
    Entry,
    Group,
    PlaylistPane,
    RowKind,
    Version,
    groups_for,
    stars,
)


def pane() -> PlaylistPane:
    return PlaylistPane(
        [
            Group(
                "Album",
                "Artist",
                2000,
                [
                    Entry(1, "One", "Artist", 100),
                    Entry(
                        2,
                        "Two",
                        "Guest",
                        200,
                        0.5,
                        [Version("Album", "FLAC", 200), Version("Best of", "MP3", 190)],
                    ),
                ],
            )
        ]
    )


def kinds(p: PlaylistPane) -> list[RowKind]:
    return [p.row(i).kind for i in range(len(p))]


def test_stars() -> None:
    assert stars(None) == ""
    assert stars(0.7) == "★★★½☆"
    assert stars(1.0) == "★★★★★"
    assert stars(0.0) == "☆☆☆☆☆"


def test_rows() -> None:
    p = pane()
    assert kinds(p) == [RowKind.RELEASE, RowKind.TRACK, RowKind.TRACK]
    assert p.row(0).detail == "Artist · 2000"
    # The track artist is shown where it isn't the album artist.
    assert p.row(1).detail == "1:40"
    assert p.row(2).detail == "Guest · 3:20"
    assert p.row(2).versions == 2


def test_toggle_and_choose() -> None:
    p = pane()
    assert p.toggle(2) == (3, 2)
    assert kinds(p)[3:] == [RowKind.VERSION, RowKind.VERSION]
    assert p.row(3).chosen
    assert p.choose(4) == [2, 3, 4]
    assert p.row(4).chosen
    assert not p.row(3).chosen
    # The track shows the chosen version's length.
    assert p.row(2).detail == "Guest · 3:10"
    assert p.toggle(2) == (3, -2)
    assert len(p) == 3


def test_navigate() -> None:
    p = pane()
    assert p.navigate("down") == (1, None)
    assert p.navigate("down") == (2, None)
    assert p.navigate("right") == (2, (3, 2))
    assert p.navigate("down") == (2, None)
    assert p.navigate("left") == (2, (3, -2))
    assert p.navigate("up") == (1, None)


def item(id: int, album: int) -> PlaylistItem:
    return PlaylistItem(id, album, 1, id, b"")


def test_groups_for_the_live_playlist() -> None:
    """Runs of a release, singletons of an artist together."""
    entries = [
        playlist.Entry(item(1, 5)),
        playlist.Entry(item(2, 5)),
        playlist.Entry(item(3, 6)),
        playlist.Entry(item(4, 0)),
        playlist.Entry(item(5, 0)),
        playlist.Entry(item(6, 0)),
        playlist.Entry(item(7, 5)),
    ]
    text = {
        1: TrackText("A", "X", "Album", "X", 1, 1, 60),
        2: TrackText("B", "X", "Album", "X", 1, 2, 60),
        3: TrackText("C", "Y", "Other", "Y", 1, 1, 60),
        4: TrackText("D", "Y", "", "Y", 0, 0, 60),
        5: TrackText("E", "Y", "", "Y", 0, 0, 60),
        6: TrackText("F", "Z", "", "Z", 0, 0, 60),
    }
    groups, playing = groups_for(entries, text, entries[4])
    assert [(g.title, len(g.entries)) for g in groups] == [
        ("Album", 2),
        ("Other", 1),
        ("", 2),
        ("", 1),
        # Not read yet.
        ("", 1),
    ]
    assert playing == (2, 1)
    assert groups[2].entries[1].index == 4
    p = PlaylistPane(groups, playing)
    assert kinds(p)[:3] == [RowKind.RELEASE, RowKind.TRACK, RowKind.TRACK]
    assert p.row(7).playing and p.row(7).title == "E"
    assert groups_for(entries, text)[1] is None


def test_set_groups() -> None:
    p = pane()
    p.current = 2
    p.set_groups(pane().groups, (0, 0))
    assert p.current == 2 and p.row(1).playing
    p.set_groups([])
    assert p.current == -1 and len(p) == 0
