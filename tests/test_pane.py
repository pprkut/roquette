# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

from roquette.library.index import Track
from roquette.queue.pane import (
    Entry,
    Group,
    PlaylistPane,
    RowKind,
    Version,
    placeholder_groups,
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


def test_append_groups_by_release() -> None:
    p = PlaylistPane([])
    tracks = [
        Track(1, 1, 1, 1, "A", "X", "Album", "X", 60),
        Track(2, 1, 1, 2, "B", "X", "Album", "X", 60),
        Track(3, 2, 1, 1, "C", "Y", "Other", "Y", 60),
    ]
    assert p.append(tracks) == (0, 5)
    assert kinds(p) == [
        RowKind.RELEASE,
        RowKind.TRACK,
        RowKind.TRACK,
        RowKind.RELEASE,
        RowKind.TRACK,
    ]


def test_placeholder() -> None:
    p = PlaylistPane(placeholder_groups())
    assert p.playing == (0, 1)
    assert p.row(2).playing
