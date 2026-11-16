# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""roquette's beets plugin.

Enabled by adding `roquette` to `plugins:` in the beets config. It
holds the defaults of the `roquette:` config section, declares the types
of the fields roquette writes, so that `beet ls play_count:10..` works
without roquette running, runs queries on them in SQL, and, if
`roquette.rating_tag` is set, maps the `FMPS_RATING` tag to the
`fmps_rating` field (docs/PLAN.md, "What lives where").

beets matches queries on flexible attributes in Python, making an Item
for every track of the library: 3 s for the dev library, and the Items'
memory stays behind (docs/decisions/0003-library.md). The plugin's
query classes for its fields run on `item_attributes` in SQL instead,
with beets' meaning: an item without the field matches no range (an
unplayed track has no `play_count`, so `^play_count:1..` finds it, not
`play_count:..0`).

The mapping is off by default. With it, beets reads the field from the
files whenever it reads their tags (`beet import`, `beet update` of a
changed file), and a file without the tag sets the rating to 0.0: a
rating made in roquette would be replaced by the file's, or by "no
stars" (docs/decisions/0003-library.md). It also makes `beet write`
and every `beet modify` that writes tags write the rating to the files.
"""

from collections.abc import Sequence
from typing import Any, ClassVar

import mediafile
from beets.dbcore import query, types
from beets.dbcore.query import FieldQueryType
from beets.plugins import BeetsPlugin

# The defaults of the `roquette:` config section. roquette adds them
# itself when the config doesn't load the plugin.
DEFAULTS: dict[str, Any] = {
    "library": {
        # roquette doesn't write to a library unless this is set, so the
        # live library stays untouched by development builds.
        "writable": False,
    },
    "rating_tag": False,
    # The plugins of the config roquette loads too, for their field
    # types and queries; the rest are `beet`'s business and cost about
    # 50 MiB (docs/decisions/0003-library.md). Only those the config
    # enables are loaded.
    "beets_plugins": ["roquette", "types", "inline", "playlist", "smartplaylist"],
}

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


class _InSQL(query.FieldQuery[str]):
    """A query on an item's flexible attribute, as SQL on
    `item_attributes`; the subclasses' `col_clause` compares `field`,
    the attribute's value as a number."""

    @property
    def field(self) -> str:
        return "CAST(value AS REAL)"

    def clause(self) -> tuple[str | None, Sequence[query.SQLiteType]]:
        where, subvals = self.col_clause()
        return (
            "items.id IN (SELECT entity_id FROM item_attributes"
            f" WHERE key = ? AND {where})",
            [self.field_name, *subvals],
        )


class NumericQuery(_InSQL, query.NumericQuery):
    pass


class DateQuery(_InSQL, query.DateQuery):
    pass


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
    # Queries on them in SQL (the `field:pattern` form; prefixes such as
    # `::` for regular expressions keep beets' own query classes).
    item_queries: ClassVar[dict[str, FieldQueryType]] = {
        "fmps_rating": NumericQuery,
        "play_count": NumericQuery,
        "skip_count": NumericQuery,
        "last_played": DateQuery,
    }

    def __init__(self, name: str = "roquette") -> None:
        super().__init__(name)
        self.config.add(DEFAULTS)
        # MediaFile's fields are global; a second instance (in tests)
        # mustn't add it again.
        if self.config["rating_tag"].get(bool) and not isinstance(
            mediafile.MediaFile.__dict__.get("fmps_rating"), mediafile.MediaField
        ):
            self.add_media_field("fmps_rating", FMPS_RATING)
