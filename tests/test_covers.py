# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

import os
import threading
from pathlib import Path

import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from roquette.covers import (
    CoverLoader,
    ImageCache,
    Pixels,
    file_uri,
    load_cover,
    thumbnail_path,
)


@pytest.fixture
def cover(tmp_path: Path) -> Path:
    path = tmp_path / "cover.jpg"
    Image.new("RGB", (1000, 800), (200, 30, 30)).save(path)
    return path


def test_load_scales_to_fit(cover: Path) -> None:
    pixels = load_cover(cover, 160, None)
    assert (pixels.width, pixels.height) == (160, 128)
    assert len(pixels.data) == 160 * 128 * 3
    # JPEG is lossy.
    pixel = pixels.data[:3]
    assert all(abs(a - b) < 8 for a, b in zip(pixel, (200, 30, 30), strict=True))


def test_uri_is_escaped_as_glib_does() -> None:
    # GLib.filename_to_uri() of the same path.
    path = Path("/Music/Guns N' Roses/Appetite (Deluxe), 1 & 2;é/cover.jpg")
    assert file_uri(path) == (
        "file:///Music/Guns%20N'%20Roses/Appetite%20(Deluxe),%201%20&%202%3B%C3%A9"
        "/cover.jpg"
    )


def test_thumbnail_is_written_and_used(cover: Path, tmp_path: Path) -> None:
    thumbnails = tmp_path / "thumbnails"
    load_cover(cover, 160, thumbnails)
    thumbnail = thumbnail_path(thumbnails, cover)
    with Image.open(thumbnail) as image:
        assert max(image.size) == 256
        assert image.info["Thumb::URI"] == file_uri(cover)
        assert image.info["Thumb::MTime"] == str(int(cover.stat().st_mtime))
    assert thumbnail.stat().st_mode & 0o777 == 0o600

    # The thumbnail is what's read the next time: a green one shows.
    with Image.open(thumbnail) as image:
        green = Image.new("RGB", image.size, (0, 200, 0))
        green.save(thumbnail, pnginfo=_info(image))
    assert load_cover(cover, 64, thumbnails).data[:3] == bytes((0, 200, 0))


def test_outdated_thumbnail_is_replaced(cover: Path, tmp_path: Path) -> None:
    thumbnails = tmp_path / "thumbnails"
    load_cover(cover, 160, thumbnails)
    Image.new("RGB", (500, 500), (0, 0, 200)).save(cover)
    stat = cover.stat()
    os.utime(cover, (stat.st_atime, stat.st_mtime + 10))
    pixel = load_cover(cover, 64, thumbnails).data[:3]
    assert all(abs(a - b) < 8 for a, b in zip(pixel, (0, 0, 200), strict=True))


def _info(image: Image.Image) -> PngInfo:
    info = PngInfo()
    for key, value in image.info.items():
        if isinstance(key, str) and isinstance(value, str):
            info.add_text(key, value)
    return info


def test_loader_delivers(cover: Path, tmp_path: Path) -> None:
    delivered: dict[str, Pixels | None] = {}
    done = threading.Event()

    def deliver(key: str, pixels: Pixels | None) -> None:
        delivered[key] = pixels
        if len(delivered) == 2:
            done.set()

    loader: CoverLoader[str] = CoverLoader(32, None, deliver)
    loader.request("good", cover)
    loader.request("missing", tmp_path / "missing.jpg")
    assert done.wait(10)
    loader.close()
    good = delivered["good"]
    assert good is not None and good.width == 32
    assert delivered["missing"] is None


@pytest.mark.parametrize(("max_pending", "loaded"), [(None, 64), (100, 100)])
def test_loader_keeps_the_newest(
    cover: Path, max_pending: int | None, loaded: int
) -> None:
    busy, release, done = threading.Event(), threading.Event(), threading.Event()
    delivered: list[int] = []

    def deliver(key: int, pixels: Pixels | None) -> None:
        if key < 0:
            busy.set()
            release.wait(10)
        else:
            delivered.append(key)
            if key == 100 - loaded:
                done.set()

    loader: CoverLoader[int] = CoverLoader(8, None, deliver)
    if max_pending is not None:
        loader.set_max_pending(max_pending)
    # Requests made while the worker is busy queue up.
    loader.request(-1, cover)
    assert busy.wait(10)
    for key in range(100):
        loader.request(key, cover)
    release.set()
    # Newest first: the oldest kept is the last.
    assert done.wait(10)
    loader.close()
    assert sorted(delivered) == list(range(100 - loaded, 100))


def test_cache_is_bounded_by_size() -> None:
    cache: ImageCache[int, str] = ImageCache(max_bytes=300, min_entries=1)
    for key in range(5):
        cache.put(key, f"image {key}", 100)
    assert len(cache) == 3
    assert cache.bytes == 300
    assert 0 not in cache and 1 not in cache
    # Used recently, so not the next to go.
    assert cache.get(2) == "image 2"
    cache.put(5, "image 5", 100)
    assert 2 in cache and 3 not in cache


def test_cache_keeps_its_minimum() -> None:
    cache: ImageCache[int, str] = ImageCache(max_bytes=100, min_entries=3)
    for key in range(3):
        cache.put(key, "big", 1000)
    assert len(cache) == 3
