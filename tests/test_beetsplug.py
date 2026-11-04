# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import beets.library
import mediafile
import mutagen
import mutagen.id3
import mutagen.mp4
import pytest
from beets import config, plugins
from beets.dbcore import types
from beets.util import cached_classproperty

from beetsplug.roquette import RoquettePlugin


@pytest.fixture
def loaded(monkeypatch: pytest.MonkeyPatch) -> Iterator[RoquettePlugin]:
    """The plugin as `load_plugins()` leaves it, with the field types of
    beets' models recomputed."""
    plugin = RoquettePlugin()
    monkeypatch.setattr(plugins, "_instances", [plugin])
    cached_classproperty.cache.clear()
    yield plugin
    cached_classproperty.cache.clear()


@pytest.fixture
def rating_tag() -> Iterator[None]:
    config["roquette"]["rating_tag"] = True
    yield
    config["roquette"]["rating_tag"] = False
    # MediaFile's fields are global.
    if "fmps_rating" in mediafile.MediaFile.__dict__:
        delattr(mediafile.MediaFile, "fmps_rating")
    beets.library.Item._media_fields.discard("fmps_rating")


def test_library_is_read_only_by_default() -> None:
    plugin = RoquettePlugin()
    assert plugin.config["library"]["writable"].get(bool) is False
    assert config["roquette"]["library"]["writable"].get(bool) is False


def test_settings_from_the_beets_config(tmp_path: Path) -> None:
    """The user's config overrides the plugin's defaults, as for any
    beets plugin."""
    user = tmp_path / "config.yaml"
    user.write_text("roquette:\n  library:\n    writable: yes\n", encoding="utf-8")
    RoquettePlugin()
    config.set_file(str(user))
    try:
        assert config["roquette"]["library"]["writable"].get(bool) is True
        assert config["roquette"]["rating_tag"].get(bool) is False
    finally:
        config.clear()
        config.read(user=False)


@pytest.mark.usefixtures("loaded")
def test_field_types(tmp_path: Path) -> None:
    assert beets.library.Item._types["play_count"] is types.INTEGER
    library = beets.library.Library(str(tmp_path / "library.db"), str(tmp_path))
    for n, play_count in enumerate([None, 3, 12]):
        item = beets.library.Item(title=f"t{n}", path=os.fsencode(tmp_path / f"{n}"))
        if play_count is not None:
            item["play_count"] = play_count
        library.add(item)
    # Numeric, not text: 12 is more than 3.
    assert [i.title for i in library.items("play_count:4..")] == ["t2"]


def test_no_conflict_with_mpdstats_and_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plugins declaring the same fields use the same types."""
    from beetsplug.types import TypesPlugin

    config["types"].set({"fmps_rating": "float", "play_count": "int"})
    try:
        others: list[plugins.BeetsPlugin] = [TypesPlugin()]
        try:
            from beetsplug.mpdstats import MPDStatsPlugin
        except ImportError:  # python-mpd2 isn't a dependency
            pass
        else:
            others.append(MPDStatsPlugin())
        monkeypatch.setattr(plugins, "_instances", [RoquettePlugin(), *others])
        assert plugins.types(beets.library.Item)["last_played"] is types.DATE
    finally:
        config["types"].set({})


def test_no_rating_tag_by_default() -> None:
    RoquettePlugin()
    assert "fmps_rating" not in beets.library.Item._media_fields


def make_audio(directory: Path, suffix: str) -> Path:
    """A short file with an FMPS_RATING tag as Amarok writes it."""
    path = directory / f"rated{suffix}"
    if suffix == ".m4a":
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            pytest.skip("no AAC encoder (ffmpeg)")
        subprocess.run(
            [ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=0.2", path],
            check=True,
        )
    else:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        encoder = {
            ".flac": "flacenc",
            ".mp3": "lamemp3enc ! id3v2mux",
            ".ogg": "vorbisenc ! oggmux",
            ".opus": "audioresample ! opusenc ! oggmux",
        }[suffix]
        pipeline = Gst.parse_launch(
            f"audiotestsrc num-buffers=10 ! audioconvert ! {encoder} ! "
            f"filesink location={path}"
        )
        pipeline.set_state(Gst.State.PLAYING)
        pipeline.get_bus().timed_pop_filtered(
            Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR
        )
        pipeline.set_state(Gst.State.NULL)
    if suffix == ".mp3":
        id3 = mutagen.id3.ID3(path)  # type: ignore[no-untyped-call]
        rating = mutagen.id3.TXXX(encoding=3, desc="FMPS_Rating", text=["0.8"])  # type: ignore[no-untyped-call]
        id3.add(rating)  # type: ignore[no-untyped-call]
        id3.save()
    elif suffix == ".m4a":
        mp4 = mutagen.mp4.MP4(path)  # type: ignore[no-untyped-call]
        mp4["----:com.apple.iTunes:FMPS_Rating"] = [b"0.8"]
        mp4.save()  # type: ignore[no-untyped-call]
    else:
        tagged = mutagen.File(path)
        tagged["FMPS_RATING"] = "0.8"
        tagged.save()
    return path


@pytest.mark.usefixtures("rating_tag")
@pytest.mark.parametrize("suffix", [".flac", ".mp3", ".ogg", ".opus", ".m4a"])
def test_rating_tag(tmp_path: Path, suffix: str) -> None:
    RoquettePlugin()
    item = beets.library.Item.from_path(make_audio(tmp_path, suffix))
    assert item.fmps_rating == pytest.approx(0.8)


@pytest.mark.usefixtures("rating_tag", "loaded")
def test_rating_tag_without_tag_resets_rating(tmp_path: Path) -> None:
    """Why the mapping is off by default: reading a file without the tag
    turns a rating in beets into 0.0."""
    path = make_audio(tmp_path, ".flac")
    tagged = mutagen.File(path)
    del tagged["FMPS_RATING"]
    tagged.save()
    library = beets.library.Library(str(tmp_path / "library.db"), str(tmp_path))
    item = beets.library.Item.from_path(path)
    item.fmps_rating = 0.6
    library.add(item)
    item.read()
    item.store()
    assert item.id is not None
    stored = library.get_item(item.id)
    assert stored is not None
    assert stored.fmps_rating == 0.0
