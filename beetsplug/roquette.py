# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""roquette's beets plugin.

Enabled by adding `roquette` to `plugins:` in the beets config. It
holds the defaults of the `roquette:` config section, declares the types
of the fields roquette writes, so that `beet ls play_count:10..` works
without roquette running, and, if `roquette.rating_tag` is set, maps the
`FMPS_RATING` tag to the `fmps_rating` field (docs/PLAN.md, "What lives
where").

The mapping is off by default. With it, beets reads the field from the
files whenever it reads their tags (`beet import`, `beet update` of a
changed file), and a file without the tag sets the rating to 0.0: a
rating made in roquette would be replaced by the file's, or by "no
stars" (docs/decisions/0003-library.md). It also makes `beet write`
and every `beet modify` that writes tags write the rating to the files.
"""

from typing import ClassVar

import mediafile
from beets.dbcore import types
from beets.plugins import BeetsPlugin

# The rating, 0 to 1 in steps of 0.1 (half stars), as the FMPS_RATING tag
# of the freedesktop.org Media Player Specifications has it, under the
# names Amarok writes.
FMPS_RATING = mediafile.MediaField(
    mediafile.MP3DescStorageStyle("FMPS_Rating"),
    mediafile.MP4StorageStyle("----:com.apple.iTunes:FMPS_Rating"),
    mediafile.StorageStyle("FMPS_RATING"),
    mediafile.ASFStorageStyle("FMPS/Rating"),
    out_type=float,
)


class RoquettePlugin(BeetsPlugin):
    # The play statistics have the names and types of the mpdstats
    # plugin's; the shared instances of the types, so the two plugins
    # don't conflict.
    item_types: ClassVar[dict[str, types.Type]] = {
        "fmps_rating": types.FLOAT,
        "play_count": types.INTEGER,
        "skip_count": types.INTEGER,
        "last_played": types.DATE,
    }

    def __init__(self, name: str = "roquette") -> None:
        super().__init__(name)
        self.config.add(
            {
                "library": {
                    # roquette doesn't write to a library unless this is
                    # set, so the live library stays untouched by
                    # development builds.
                    "writable": False,
                },
                "rating_tag": False,
            }
        )
        # MediaFile's fields are global; a second instance (in tests)
        # mustn't add it again.
        if self.config["rating_tag"].get(bool) and not isinstance(
            mediafile.MediaFile.__dict__.get("fmps_rating"), mediafile.MediaField
        ):
            self.add_media_field("fmps_rating", FMPS_RATING)
