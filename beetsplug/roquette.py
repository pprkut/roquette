# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""roquette's beets plugin.

Enabled by adding `roquette` to `plugins:` in the beets config. It
holds the defaults of the `roquette:` config section; later it declares
the types of the fields roquette writes (docs/PLAN.md).
"""

from beets.plugins import BeetsPlugin


class RoquettePlugin(BeetsPlugin):
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
            }
        )
