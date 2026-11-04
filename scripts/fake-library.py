#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Make a fake beets library, for running roquette without the dev library.

Where there's no `just dev-library` (CI, cloud sessions), this makes a
library of the same size and shape: by default 800 releases with 11,800
tracks, generated names and generated JPEG covers (some releases have
none). The items have the fields an imported library has (MusicBrainz
ids, technical data, ReplayGain, lyrics for three quarters of them) and
a few flexible attributes (ratings, play counts, the lyrics plugin's).
As in the dev library, about 9% of the tracks are versions of a
recording that exists more than once (on a compilation, a live album, a
re-release in another format); some tracks are singletons, and some
releases are a single with the name of an album.

The items' music files don't exist, so nothing can be played.

Use it like the development copy, with BEETSDIR=build/fake-library/beets;
its config sets `roquette.library.writable`.
"""

import argparse
import os
import random
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parent.parent

# fmt: off
SYLLABLES = [
    "ka", "ri", "mo", "ne", "ta", "lu", "vi", "so", "ra", "en", "el", "an",
    "dor", "mi", "sa", "ko", "be", "th", "ul", "ar", "is", "on", "ve", "ly",
    "ro", "na", "de", "li", "ga", "zu", "or", "ex", "qu", "io", "ph", "st",
]
WORDS = [
    "night", "light", "river", "echo", "glass", "dream", "winter", "fire",
    "song", "stone", "heart", "blue", "silver", "garden", "machine", "ocean",
    "shadow", "city", "storm", "golden", "moon", "electric", "paper", "velvet",
    "hollow", "signal", "wild", "secret", "distant", "broken", "summer", "rain",
]
GENRES = [
    "Rock", "Pop", "Electronic", "Jazz", "Classical", "Metal", "Folk",
    "Hip-Hop", "Ambient", "Soul", "Blues", "Punk", "Indie", "Soundtrack",
]
LABELS = [f"{w.title()} Records" for w in WORDS[:12]]
COUNTRIES = ["XW", "GB", "US", "DE", "NL", "JP", "FR", "SE"]
# albumtype, albumtypes, weight
RELEASE_TYPES = [
    ("album", "album", 70),
    ("ep", "ep", 8),
    ("album", "album; live", 5),
    ("compilation", "album; compilation", 6),
    ("soundtrack", "album; soundtrack", 3),
    ("album", "album; remix", 2),
    ("single", "single", 10),
]
# format, bitdepth, samplerate, bitrate, weight: almost all FLAC, as in
# the dev library.
FORMATS = [
    ("FLAC", 16, 44100, 900_000, 85),
    ("FLAC", 24, 96000, 2_800_000, 8),
    ("FLAC", 24, 44100, 1_400_000, 5),
    ("MP3", 0, 44100, 320_000, 1),
    ("AAC", 0, 44100, 256_000, 1),
]
# fmt: on


def name(rng: random.Random, syllables: int) -> str:
    return "".join(rng.choice(SYLLABLES) for _ in range(syllables)).capitalize()


def title(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 4))).title()


def lyrics(rng: random.Random) -> str:
    """About 1,500 characters: 30 to 50 lines of a few words."""
    lines = (
        " ".join(rng.choice(WORDS) for _ in range(rng.randint(3, 8))).capitalize()
        for _ in range(rng.randint(30, 50))
    )
    return "\n".join(lines)


def mbid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def cover(rng: random.Random, path: Path, size: int) -> None:
    """A gradient with shapes and noise, so it compresses like a photo."""
    base = tuple(rng.randrange(256) for _ in range(3))
    image = Image.new("RGB", (size, size), base)
    draw = ImageDraw.Draw(image)
    for _ in range(12):
        x, y = rng.randrange(size), rng.randrange(size)
        r = rng.randrange(size // 16, size // 3)
        colour = tuple(rng.randrange(256) for _ in range(3))
        draw.ellipse((x - r, y - r, x + r, y + r), fill=colour)
    noise = Image.effect_noise((size, size), 48).convert("RGB")
    Image.blend(image, noise, 0.25).save(path, "JPEG", quality=90)


@dataclass(frozen=True, slots=True)
class Artist:
    name: str
    sort: str
    mbid: str


@dataclass(frozen=True, slots=True)
class Recording:
    """What versions of one recording share."""

    mbid: str
    title: str
    artist: Artist
    length: float
    work: str
    isrc: str


def track_fields(
    rng: random.Random, recording: Recording, fmt: tuple[str, int, int, int, int]
) -> dict[str, Any]:
    """The fields of one track (one version of `recording`)."""
    fields: dict[str, Any] = {
        "title": recording.title,
        "artist": recording.artist.name,
        "artist_sort": recording.artist.sort,
        "artists": [recording.artist.name],
        "artists_sort": [recording.artist.sort],
        "mb_artistid": recording.artist.mbid,
        "mb_artistids": [recording.artist.mbid],
        "mb_trackid": recording.mbid,
        "mb_releasetrackid": mbid(rng),
        "isrc": recording.isrc,
        # Versions differ by a few seconds.
        "length": recording.length + rng.uniform(-2, 2),
        "format": fmt[0],
        "bitdepth": fmt[1],
        "samplerate": fmt[2],
        "bitrate": int(fmt[3] * rng.uniform(0.8, 1.2)),
        "channels": 2,
        "encoder": "reference libFLAC 1.4.3" if fmt[0] == "FLAC" else "LAME3.100",
        "rg_track_gain": rng.uniform(-12, 2),
        "rg_track_peak": rng.uniform(0.5, 1.0),
        "comments": "",
        "acoustid_id": mbid(rng),
        "mtime": time.time() - rng.uniform(0, 10 * 365 * 86400),
    }
    if recording.work:
        fields["mb_workid"] = recording.work
        fields["work"] = recording.title
    if rng.random() < 0.3:
        composer = recording.artist.name
        fields["composers"] = [composer]
        fields["composer_sort"] = recording.artist.sort
    return fields


def flex_fields(rng: random.Random, fields: dict[str, Any]) -> None:
    """Flexible attributes: ratings, play statistics, the lyrics plugin's."""
    if rng.random() < 0.75:
        fields["lyrics"] = lyrics(rng)
        fields["lyrics_backend"] = rng.choice(["lrclib", "genius", "tekstowo"])
        fields["lyrics_url"] = f"https://lyrics.example/{rng.randrange(10**9)}"
        fields["lyrics_language"] = "EN"
    if rng.random() < 0.4:
        rating = rng.randint(1, 10)
        fields["fmps_rating"] = rating / 10
        fields["amarok_rating"] = rating
    if rng.random() < 0.3:
        fields["play_count"] = rng.randint(1, 60)
        fields["last_played"] = time.time() - rng.uniform(0, 5 * 365 * 86400)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--target",
        type=Path,
        default=REPO / "build" / "fake-library",
        help="where to make it (default: %(default)s)",
    )
    parser.add_argument("--releases", type=int, default=800)
    parser.add_argument("--tracks", type=int, default=11800)
    parser.add_argument(
        "--singletons",
        type=float,
        default=0.02,
        help="the share of tracks that are singletons (default: %(default)s)",
    )
    parser.add_argument(
        "--versions",
        type=float,
        default=0.05,
        help="the chance that a track is another version of a recording "
        "already in the library (default: %(default)s)",
    )
    parser.add_argument(
        "--cover-size", type=int, default=1000, help="pixels (default: %(default)s)"
    )
    parser.add_argument(
        "--no-covers", action="store_true", help="make no covers (faster)"
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--force", action="store_true", help="replace an existing one")
    args = parser.parse_args()

    target: Path = args.target.resolve()
    if target.exists():
        if not args.force:
            print(f"{target} exists; --force replaces it", file=sys.stderr)
            return 1
        shutil.rmtree(target)
    beets_dir = target / "beets"
    covers = target / "covers"
    music = target / "music"
    beets_dir.mkdir(parents=True)
    covers.mkdir()

    config = {
        "library": str(beets_dir / "library.db"),
        "directory": str(music),
        "pluginpath": [str(REPO / "beetsplug")],
        "plugins": ["roquette"],
        "roquette": {"library": {"writable": True}},
    }
    with (beets_dir / "config.yaml").open("w", encoding="utf-8") as config_file:
        config_file.write("# A fake library, made by scripts/fake-library.py.\n")
        yaml.safe_dump(config, config_file, sort_keys=False)

    # beets reads its config when it's first used: this one, not yours.
    os.environ["BEETSDIR"] = str(beets_dir)
    import beets.library

    rng = random.Random(args.seed)
    artists = []
    for _ in range(250):
        first, last = name(rng, 2), name(rng, rng.randint(1, 3))
        artists.append(Artist(f"{first} {last}", f"{last}, {first}", mbid(rng)))
    various = Artist("Various Artists", "Various Artists", mbid(rng))
    recordings: list[Recording] = []
    by_artist: dict[Artist, list[Recording]] = {}
    versions = 0

    def recording(artist: Artist) -> Recording:
        """A recording of `artist`'s (any artist's on a compilation),
        sometimes one that is in the library already."""
        nonlocal versions
        pool = recordings if artist is various else by_artist.get(artist)
        if pool and rng.random() < args.versions:
            versions += 1
            return rng.choice(pool)
        new = Recording(
            mbid(rng),
            title(rng),
            rng.choice(artists) if artist is various else artist,
            rng.uniform(90, 480),
            mbid(rng) if rng.random() < 0.6 else "",
            f"XX{rng.randrange(10**10):010}",
        )
        recordings.append(new)
        by_artist.setdefault(new.artist, []).append(new)
        return new

    def path(*parts: str) -> bytes:
        return os.fsencode(music.joinpath(*parts))

    library = beets.library.Library(config["library"], config["directory"])
    singletons = round(args.tracks * args.singletons)
    remaining = args.tracks - singletons
    added = time.time() - 6 * 365 * 86400
    types = rng.choices(RELEASE_TYPES, [t[2] for t in RELEASE_TYPES], k=args.releases)
    sizes = [rng.randint(1, 3) if t[0] == "single" else 0 for t in types]
    others = sizes.count(0)
    remaining -= sum(sizes)
    with library.transaction():
        for release, (albumtype, albumtypes, _) in enumerate(types):
            count = sizes[release]
            if not count:
                count = max(4, round(remaining / others))
                remaining -= count
                others -= 1
            albumartist = various if albumtype == "compilation" else rng.choice(artists)
            fmt = rng.choices(FORMATS, [f[4] for f in FORMATS])[0]
            year = rng.randint(1960, 2026)
            original_year = year if rng.random() < 0.8 else rng.randint(1960, year)
            discs = 2 if count > 20 else 1
            tracks = [recording(albumartist) for _ in range(count)]
            album = title(rng)
            if albumtype == "single":
                # A single named after its first track, which may be on an
                # album of the same name too.
                album = tracks[0].title
            # Imported over the last six years.
            added += rng.uniform(0, 2 * 6 * 365 * 86400 / args.releases)
            release_fields: dict[str, Any] = {
                "album": album,
                "albumartist": albumartist.name,
                "albumartist_sort": albumartist.sort,
                "albumartists": [albumartist.name],
                "albumartists_sort": [albumartist.sort],
                "mb_albumartistid": albumartist.mbid,
                "mb_albumartistids": [albumartist.mbid],
                "mb_albumid": mbid(rng),
                "mb_releasegroupid": mbid(rng),
                "release_group_title": album,
                "albumtype": albumtype,
                "albumtypes": albumtypes.split("; "),
                "albumstatus": "Official",
                "comp": albumartist is various,
                "year": year,
                "month": rng.randint(1, 12),
                "day": rng.randint(1, 28),
                "original_year": original_year,
                "label": rng.choice(LABELS),
                "catalognum": f"CAT{rng.randrange(10**5):05}",
                "barcode": f"{rng.randrange(10**12):012}",
                "asin": f"B0{rng.randrange(10**8):08}",
                "country": rng.choice(COUNTRIES),
                "media": "Digital Media" if fmt[0] != "FLAC" else "CD",
                "script": "Latn",
                "language": "eng",
                "genres": rng.sample(GENRES, rng.randint(1, 3)),
                "disctotal": discs,
                "rg_album_gain": rng.uniform(-12, 2),
                "rg_album_peak": rng.uniform(0.8, 1.0),
                "added": added,
            }
            per_disc = -(-count // discs)
            items = []
            for n, rec in enumerate(tracks):
                disc, track = divmod(n, per_disc)
                fields = release_fields | track_fields(rng, rec, fmt)
                fields |= {
                    "disc": disc + 1,
                    "track": track + 1,
                    "tracktotal": min(per_disc, count - disc * per_disc),
                    "path": path(albumartist.name, album, f"{n + 1:02} {rec.title}"),
                }
                flex_fields(rng, fields)
                items.append(beets.library.Item(**fields))
            stored = library.add_album(items)
            stored["data_source"] = "MusicBrainz"
            if not args.no_covers and rng.random() < 0.95:
                cover_path = covers / f"{stored.id}.jpg"
                cover(rng, cover_path, args.cover_size)
                stored.artpath = os.fsencode(cover_path)
            stored.store()
            if release % 100 == 99:
                print(f"{release + 1} releases", file=sys.stderr)
        for _ in range(singletons):
            artist = rng.choice(artists)
            rec = recording(artist)
            fields = track_fields(rng, rec, FORMATS[0])
            fields |= {
                "album": "",
                "albumartist": artist.name,
                "year": rng.randint(1960, 2026),
                "genres": [rng.choice(GENRES)],
                "added": added,
                "path": path("Non-Album", artist.name, rec.title),
            }
            flex_fields(rng, fields)
            library.add(beets.library.Item(**fields))

    # beets backs up the database before each migration, even a new one.
    for backup in beets_dir.glob("library.db-before-*.bak"):
        backup.unlink()

    print(f"Made {target}: {args.tracks} tracks, {versions} of them versions.")
    print(f"Use it with BEETSDIR={beets_dir.relative_to(Path.cwd(), walk_up=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
