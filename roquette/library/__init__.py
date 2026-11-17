# SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
# SPDX-License-Identifier: MIT

"""beets access (docs/decisions/0003-library.md).

- `config`: the beets config, roquette's settings in it, the plugins.
- `database`: the libraries roquette reads and writes through.
- `index`: the resident index, as columns of numbers; text by id.
- `grouping`, `recordings`: releases and media, recording versions.
- `query`, `search`: beets queries as ids from SQL, incremental search.
- `writes`: writing while `beet` writes.
- `workers`, `watch`, `service`: the reader and writer threads, the
  watch on the database, and what the rest of roquette uses.
"""


class LibraryError(Exception):
    """The beets library can't be used; the message is for the user."""
