# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

from roquette.library.index import Track
from roquette.modules.tracks import TrackList, format_length

TRACKS = [
    Track(1, 1, 1, 1, "Speak to Me", "Pink Floyd", "Dark Side", "Pink Floyd", 67),
    Track(2, 1, 1, 2, "Breathe", "Pink Floyd", "Dark Side", "Pink Floyd", 169),
    Track(3, 2, 2, 1, "avril 14th", "Aphex Twin", "Drukqs", "Aphex Twin", 121.6),
]


def test_format_length() -> None:
    assert format_length(0) == "0:00"
    assert format_length(67.4) == "1:07"
    assert format_length(3723) == "1:02:03"


def test_rows() -> None:
    tracks = TrackList(TRACKS)
    assert len(tracks) == 3
    assert tracks.row(0) == ("1", "Speak to Me", "Pink Floyd", "Dark Side", "1:07")
    # Discs after the first are shown.
    assert tracks.row(2).number == "2.01"


def test_scale_repeats_the_library() -> None:
    tracks = TrackList(TRACKS, scale=10)
    assert len(tracks) == 30
    assert tracks.track(29) == TRACKS[2]


def test_sort() -> None:
    tracks = TrackList(TRACKS)
    tracks.sort(1)
    assert [tracks.track(i).title for i in range(3)] == [
        "avril 14th",
        "Breathe",
        "Speak to Me",
    ]
    tracks.sort(1)
    assert not tracks.ascending
    assert tracks.track(0).title == "Speak to Me"
    tracks.sort(4)
    assert [tracks.track(i).id for i in range(3)] == [1, 3, 2]


def test_navigate() -> None:
    tracks = TrackList(TRACKS)
    assert tracks.navigate("down", 10) == 0
    assert tracks.navigate("page-down", 10) == 2
    assert tracks.navigate("up", 10) == 1
    assert tracks.navigate("home", 10) == 0
    assert tracks.navigate("end", 10) == 2
