#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""Make a fake beets library, for running roquette without the dev library.

Where there's no `just dev-library` (CI, cloud sessions), this makes a
library of the same size: by default 800 releases with 11,800 tracks,
generated names and generated JPEG covers (some releases have none). The
items' music files don't exist, so nothing can be played.

Use it like the development copy, with BEETSDIR=build/fake-library/beets;
its config sets `roquette.library.writable`.
"""

import argparse
import os
import random
import shutil
import sys
from pathlib import Path

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
# fmt: on


def name(rng: random.Random, syllables: int) -> str:
    return "".join(rng.choice(SYLLABLES) for _ in range(syllables)).capitalize()


def title(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 4))).title()


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
        "--cover-size", type=int, default=1000, help="pixels (default: %(default)s)"
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
    artists = [f"{name(rng, 2)} {name(rng, rng.randint(1, 3))}" for _ in range(250)]
    library = beets.library.Library(config["library"], config["directory"])
    remaining = args.tracks
    with library.transaction():
        for release in range(args.releases):
            count = round(remaining / (args.releases - release))
            remaining -= count
            various = rng.random() < 0.05
            albumartist = "Various Artists" if various else rng.choice(artists)
            album = title(rng)
            year = rng.randint(1960, 2026)
            discs = 2 if count > 20 else 1
            items = []
            for n in range(count):
                disc, track = divmod(n, -(-count // discs))
                items.append(
                    beets.library.Item(
                        title=title(rng),
                        artist=rng.choice(artists) if various else albumartist,
                        albumartist=albumartist,
                        album=album,
                        year=year,
                        disc=disc + 1,
                        disctotal=discs,
                        track=track + 1,
                        length=rng.uniform(90, 480),
                        format="FLAC",
                        mb_trackid=f"fake-recording-{rng.randrange(10**9)}",
                        path=os.fsencode(
                            music / albumartist / album / f"{n + 1:02}.flac"
                        ),
                    )
                )
            stored = library.add_album(items)
            if rng.random() < 0.95:
                path = covers / f"{stored.id}.jpg"
                cover(rng, path, args.cover_size)
                stored.artpath = os.fsencode(path)
                stored.store()
            if release % 100 == 99:
                print(f"{release + 1} releases", file=sys.stderr)

    # beets backs up the database before each migration, even a new one.
    for backup in beets_dir.glob("library.db-before-*.bak"):
        backup.unlink()

    print(f"Made {target}.")
    print(f"Use it with BEETSDIR={beets_dir.relative_to(Path.cwd(), walk_up=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
