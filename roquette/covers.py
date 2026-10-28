# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Cover images, decoded off the UI thread at the size they're shown at.

Covers come from the shared freedesktop thumbnail cache ("large", 256 px)
when it has a current thumbnail of the file, and are added to it
otherwise, so file managers and roquette share the work
(https://specifications.freedesktop.org/thumbnail-spec/latest/).
"""

import contextlib
import hashlib
import os
import tempfile
import threading
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, PngImagePlugin

LARGE = 256


@dataclass(frozen=True, slots=True)
class Pixels:
    """RGB pixels, 3 bytes each, row by row."""

    width: int
    height: int
    data: bytes


def file_uri(path: Path) -> str:
    """The URI the thumbnail cache knows `path` by, as GLib makes it.

    The cache names thumbnails by the MD5 of this URI, so it must be
    escaped exactly as other programs escape it: `Path.as_uri()` escapes
    characters such as `'`, `(` and `,` that GLib leaves as they are.
    """
    quoted = urllib.parse.quote(os.fsencode(path.absolute()), safe="/!$&'()*+,:=@~")
    return f"file://{quoted}"


def thumbnail_path(thumbnails: Path, original: Path) -> Path:
    uri = file_uri(original)
    return thumbnails / "large" / f"{hashlib.md5(uri.encode()).hexdigest()}.png"


def load_cover(original: Path, size: int, thumbnails: Path | None) -> Pixels:
    """The cover in `original`, scaled to fit `size` × `size` pixels.

    Raises OSError (or a Pillow error) if it can't be read.
    """
    mtime = int(original.stat().st_mtime)
    image = None
    if thumbnails is not None:
        image = _read_thumbnail(thumbnail_path(thumbnails, original), mtime)
    if image is None:
        with Image.open(original) as full:
            # JPEG decodes straight to a fraction of its size.
            full.draft("RGB", (LARGE, LARGE))
            image = full.convert("RGB")
        image.thumbnail((LARGE, LARGE), Image.Resampling.LANCZOS)
        if thumbnails is not None:
            _write_thumbnail(thumbnails, original, mtime, image)
    image = image.convert("RGB")
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    return Pixels(image.width, image.height, image.tobytes())


def _read_thumbnail(path: Path, mtime: int) -> Image.Image | None:
    try:
        with Image.open(path) as thumbnail:
            if thumbnail.info.get("Thumb::MTime") != str(mtime):
                return None
            thumbnail.load()
            return thumbnail.copy()
    except (OSError, SyntaxError, ValueError):
        return None


def _write_thumbnail(
    thumbnails: Path, original: Path, mtime: int, image: Image.Image
) -> None:
    path = thumbnail_path(thumbnails, original)
    info = PngImagePlugin.PngInfo()
    info.add_text("Thumb::URI", file_uri(original))
    info.add_text("Thumb::MTime", str(mtime))
    info.add_text("Thumb::Size", str(original.stat().st_size))
    info.add_text("Software", "Roquette")
    # Written next to its place and renamed, so other programs never see
    # half a thumbnail; private, as the spec asks.
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, suffix=".png")
    except OSError:
        return
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as file:
            image.save(file, "PNG", pnginfo=info)
        temp.replace(path)
    except OSError:
        temp.unlink(missing_ok=True)


type Deliver[K] = Callable[[K, Pixels | None], None]


class CoverLoader[K: Hashable]:
    """Loads covers on a worker thread at low priority, newest request first.

    Only the newest `max_pending` requests are kept: while the user
    scrolls, covers that scrolled out of view again before their turn are
    dropped. `deliver` is called on the worker thread with the key and the
    pixels (None if the cover can't be read); it hands them to the event
    loop.
    """

    def __init__(
        self,
        size: int,
        thumbnails: Path | None,
        deliver: Deliver[K],
        max_pending: int = 64,
    ) -> None:
        self.size = size
        self._thumbnails = thumbnails
        self._deliver = deliver
        self._max_pending = max_pending
        self._pending: OrderedDict[K, Path] = OrderedDict()
        self._loading: K | None = None
        self._wake = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="roquette covers", daemon=True
        )
        self._thread.start()

    def request(self, key: K, path: Path) -> None:
        with self._wake:
            if key == self._loading:
                return
            self._pending[key] = path
            self._pending.move_to_end(key)
            while len(self._pending) > self._max_pending:
                self._pending.popitem(last=False)
            self._wake.notify()

    def cancel_all(self) -> None:
        with self._wake:
            self._pending.clear()

    def close(self) -> None:
        with self._wake:
            self._closed = True
            self._pending.clear()
            self._wake.notify()
        self._thread.join()

    def _run(self) -> None:
        # Below the UI and GStreamer's threads.
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
        while True:
            with self._wake:
                while not self._pending and not self._closed:
                    self._wake.wait()
                if self._closed:
                    return
                key, path = self._pending.popitem(last=True)
                self._loading = key
            try:
                pixels: Pixels | None = load_cover(path, self.size, self._thumbnails)
            except Exception:
                pixels = None
            # Delivered before it's no longer loading, so a request for it
            # in between can't load it again.
            self._deliver(key, pixels)
            with self._wake:
                self._loading = None


class ImageCache[K: Hashable, V]:
    """A cache of decoded images, bounded by their size in bytes.

    The least recently used image goes first, but at least `min_entries`
    stay, so the images on screen are never evicted.
    """

    def __init__(self, max_bytes: int, min_entries: int = 64) -> None:
        self.max_bytes = max_bytes
        self.min_entries = min_entries
        self.bytes = 0
        self._entries: OrderedDict[K, tuple[V, int]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: K) -> bool:
        return key in self._entries

    def get(self, key: K) -> V | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, key: K, value: V, size: int) -> None:
        old = self._entries.pop(key, None)
        if old is not None:
            self.bytes -= old[1]
        self._entries[key] = (value, size)
        self.bytes += size
        while self.bytes > self.max_bytes and len(self._entries) > self.min_entries:
            _, (_, evicted) = self._entries.popitem(last=False)
            self.bytes -= evicted

    def clear(self) -> None:
        self._entries.clear()
        self.bytes = 0
