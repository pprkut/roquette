<!--
SPDX-FileCopyrightText: Copyright 2026  Heinz Wiesinger, Amsterdam, The Netherlands
SPDX-License-Identifier: MIT
-->

# 0003 — beets as the only backend (spike S3)

**Status:** proposed (2026-11-04); the measurements on the dev library
are still to be made (see "Still to do on the desktop").

## Context

roquette has no database of its own: everything it knows about the
music is in the beets library, read and written in-process through
beets' API (docs/PLAN.md, "Goals"). S3 checks what that costs and where
it breaks: loading and grouping the whole library, the recording index,
typical queries, the whole library as beets `Item`s against a compact
index, per-thread connections, loading the configured plugins, writing
flexible attributes while `beet` writes to the same database, the
`FMPS_RATING` tag through the roquette plugin, the `roquette:` settings
and the `playlist` plugin's M3U files.

S3 was done in a cloud session, without the dev library: the figures
below are from the fake library of `just fake-library`, which was made
to look like the dev library for this (see "What was built"). The same
scripts run on the dev copy; their results are to be added.

## What was built

- **`scripts/fake-library.py`** now makes items with the fields an
  imported library has (MusicBrainz ids, technical data, ReplayGain,
  label, genres; lyrics with the `lyrics` plugin's flexible attributes
  for 75% of the items; ratings and play counts as flexible attributes
  for some), recordings in several versions (572 recordings in 1,170
  tracks, against the dev library's 491 in 1,064), singletons (2%),
  singles named after an album's track, and `--no-covers` for large
  libraries. Ten times the library is
  `--releases 8000 --tracks 118000 --no-covers`.
- **`just measure-library`** (`scripts/measure-library.py`): time and
  memory of opening the library with its plugins, the whole library as
  `Item`s and `Album`s, the compact index through beets' API and through
  narrow SQL, the recording index and the grouping, typical queries,
  per-thread connections. Every scenario runs in a process of its own.
- **`just library-locking`** (`scripts/library-locking.py`): roquette
  writing play counts and reading items while `beet modify`, an
  `mbsync`-like or an `update`-like job writes to a copy of the library,
  with beets' defaults and with the write strategy below, in SQLite's
  rollback-journal and WAL modes; checks that no write was lost.
- **`just beets-plugins`** (`scripts/beets-plugins.py`): for each
  plugin of the config (or `--all` that beets ships), the time and
  memory of loading it, its side effects meanwhile (through an audit
  hook: files written, network, subprocesses, threads), the events it
  listens to and what it does on the ones roquette sends.
- Prototypes in `roquette/library/`: the recording index
  (`recordings.py`), the release/medium grouping (`grouping.py`), the
  write path (`writes.py`); with tests.
- **The roquette plugin** declares the types of `fmps_rating`,
  `play_count`, `skip_count` and `last_played`, and maps the
  `FMPS_RATING` tag to `fmps_rating` if `roquette.rating_tag` is set.
- `just dev-library` now also redirects the `smartplaylist` plugin's
  `playlist_dir` into the copy (found along the way, see below).

## Findings

### The whole library: Items against the compact index

`just measure-library` on the fake library (11,800 tracks, 800
releases, 54,864 flexible attributes, a 36 MiB database), in the cloud
container (Intel Xeon @ 2.10 GHz, 4 cores; Python 3.13.18, beets
2.14.1, SQLite 3.53.1), median of 3 runs. Memory is RSS above the
opened library; "kept" is what stays after a garbage collection and
`malloc_trim()` while the result is still referenced.

| what | ms | peak MiB | kept MiB |
|---|---|---|---|
| all Items (`list(lib.items())`) | 1,296 | 124.7 | 123.6 |
| all Albums (`list(lib.albums())`) | 27 | 4.4 | 4.4 |
| the index through beets' API (an Item per row, dropped) | 1,665 | 127.5 | 52.9 |
| the index, narrow SQL (S1's `load_index`) | 45 | 9.5 | 9.2 |
| the index + recording index + grouping | 164 | 9.5 | 9.5 |
| the index as columns of numbers (no text) | 29 | 3.2 | 2.4 |

Ten times the library (a fake library of 118,000 tracks, 8,000 releases,
a 364 MiB database; one run):

| what | ms | peak MiB | kept MiB |
|---|---|---|---|
| all Items | 25,601 | 1,222 | 1,212 |
| the index through beets' API | 23,142 | 1,253 | 478 |
| the index, narrow SQL | 539 | 74.9 | 74.1 |
| the index + recording index + grouping | 1,135 | 74.8 | 74.8 |
| the index as columns of numbers (no text) | 304 | 8.0 | 5.1 |

- **An `Item` costs about 10.7 KiB** here (97 fixed fields, a dict of
  flexible attributes, a `LazyDict` for the album's), so the whole
  library as Items is 124 MiB, more than half the idle budget.
- **The compact index can't be built through beets' API.** Iterating
  `lib.items()` and keeping only the index's tuples takes 37 times as
  long as the narrow query and still peaks at the Items' 127 MiB: beets'
  `Results` fetches all rows (and all flexible attributes) with
  `fetchall()` before the first Item is made. Worse, 53 MiB stay behind
  once the Items are gone. That isn't live objects (tracemalloc counts
  3 MiB of Python objects kept): it's memory the allocators can't give
  back because the index's small objects are spread over the arenas the
  Items used. **Any operation that makes an Item per track, even
  briefly, leaves its memory behind for good.** The index stays one
  narrow query per table (S1), and beets' API is for the items on
  screen or being changed.
- **The recording index**: 30 ms and 0.3 MiB for the 1,170 tracks in
  572 recordings with versions, with a narrow query that selects only
  recordings with more than one version (`roquette/library/recordings.py`).
  Each track maps to a tuple of its recording's versions, best first,
  shared by them. Tracks without versions cost nothing.
- **The grouping** (release → medium → track, singletons per album
  artist): 3 ms and no memory worth measuring. As the index is sorted in
  the collection's order, a release and a medium are ranges of the track
  list: a `Group` per release holds four integers and its media
  (`roquette/library/grouping.py`). A same-named album and single stay
  apart (they're grouped by `album_id`).
- **Load and group the whole library**: 164 ms on the worker, 9.5 MiB,
  of which S1's index is 45 ms. The rest is the index's second query
  (releases) and the versions.
- **S1's index doesn't scale to the budget.** Its tuples with the text
  of each row cost about 660 bytes a track: 9 MiB for the library, but
  74 MiB for ten times the library, where the budget allows 10 MiB more
  than for the library. (S1's ten-times run showed the same 11,800
  tracks ten times over, so it didn't see this.) The same index as
  columns of numbers in the collection's order (`array`s of id,
  `album_id`, disc, track, length; `columns` in
  `scripts/measure-library.py`) costs 2.4 MiB and 29 ms for the
  library, 5.1 MiB and 304 ms for ten times the library, and the text of
  a screenful of rows (60, by id) takes 0.1 ms to read. The text the
  views show is read for the rows on screen, which the lazy models of S1
  already ask for one at a time.

### Queries

The time to get what a beets query matches, as Items
(`list(lib.items(q))`) and as ids only (the query's SQL from beets'
parser, `query.clause()`, in a narrow `SELECT items.id`):

| query | in SQL | matches | Items ms | ids ms |
|---|---|---|---|---|
| `albumartist:Various` | yes | 546 | 74 | 10 |
| a word of a title (any field) | yes | 1,684 | 199 | 14 |
| the word and `year:1990..1999` | yes | 172 | 44 | 14 |
| `year:1990..1999` | yes | 1,526 | 166 | 11 |
| `format:MP3` | yes | 108 | 30 | 10 |
| `albumtype:single` | yes | 138 | 32 | 9 |
| `singleton:true` | yes | 236 | 21 | 0.2 |
| `added:-1y..` | yes | 11,800 | 1,383 | 14 |
| `title::^The` (regular expression) | yes | 0 | 34 | 17 |
| `fmps_rating:0.8..` | **no** | 1,494 | 2,610 | 2,586 |
| `play_count:1..` | **no** | 3,527 | 2,727 | 2,669 |
| `last_played:-1y..` | **no** | 692 | 2,680 | 2,647 |

At ten times the library (one run), the queries in SQL take 90–220 ms
as ids (1 ms for `singleton:true`) and up to 21 s as Items (`added:-1y..`,
all 118,000); the queries on flexible attributes take 31–33 s in beets
and 36–49 ms in SQL.

- **Queries on fixed fields run in SQL**, including free text, ranges,
  dates and regular expressions (beets registers `regexp` with SQLite).
  As ids, every one of them is under 20 ms: fast enough for incremental
  search on every key press, on a worker thread. As Items they cost
  0.11 ms per match, so a query that matches much is never turned into
  Items.
- **Queries on flexible attributes don't**: beets makes an Item for
  every track and matches it in Python, 2.6 s here, with the Items'
  memory left behind (see above). These are exactly the fields roquette
  writes (ratings, play statistics), which the Statistics module and
  smart playlists query. The same queries as SQL on `item_attributes`
  (`entity_id` where `key = ?` and `CAST(value AS REAL) >= ?`) take
  3 ms and find the same items.
- **Without the field types, those queries are wrong**, not only slow:
  with no type declared, `play_count:1..` is a substring match on text
  and finds nothing. The roquette plugin now declares the types (see
  below).
- `beet` sorts and limits flexible attributes in Python too (`sort.is_slow()`);
  sorting by rating or play count needs the same treatment.

### Connections and threads

- beets makes **a connection per thread** (keyed by the thread's id)
  and keeps it until the library is closed: 8 threads that each read
  the index left 8 connections, about 4 MiB each (SQLite's page cache of
  up to 2 MiB per connection, and the thread's arenas). They aren't
  closed when a thread ends; as thread ids are reused, a new thread
  may inherit a dead one's connection. A small, fixed pool of workers
  is what fits.
- **One transaction at a time per `Library`**: beets serialises them
  with a lock of its own (`Database._db_lock`), whatever the thread.
  Four threads reading the index through one `Library` take 221 ms,
  four times one read (51 ms).
- **Reading in parallel is much worse**: four threads with a `Library`
  each took 3.2 s for the same four reads, 15 times as long. Python's
  `sqlite3` releases the GIL for every row it steps through, and the
  threads hand it back and forth. Reads go through one worker, one
  after the other; parallel connections only for writes (see below).
- `Library.music_dir_context()` is needed for paths on other threads
  (S1); `Library(set_music_dir=False)` keeps a second `Library` from
  changing the process' music directory.

### Writing while `beet` writes

`just library-locking` on a copy of the fake library: roquette's
writer thread adds 1 to a random item's `play_count` every 100 ms
(read and write in one transaction), its reader thread asks for one item
every 50 ms; meanwhile `beet modify --nowrite` of every item (one
transaction, as `beet` does it; most of its 20 s is spent before it),
an mbsync-like job (100 releases, 0.2 s "network" each, then a
transaction per release) or an update-like job (one transaction over the
library, 0.5 ms "stat" per item, every twentieth item changed, 10 s).
"beets" is one `Library` for both threads with beets' timeout of 5 s;
"roquette" is the write strategy below.

| job | strategy | journal | writes | failed | retries | write ms p50 / max | read ms p50 / p99 / max | lost |
|---|---|---|---|---|---|---|---|---|
| modify | beets | rollback | 188 | 0 | 0 | 4 / 934 | 0.6 / 28 / 933 | 0 |
| modify | roquette | rollback | 191 | 0 | 5 | 4 / 1,222 | 0.5 / 19 / 1,187 | 0 |
| mbsync | beets | rollback | 211 | 0 | 0 | 4 / 19 | 0.6 / 6 / 10 | 0 |
| mbsync | roquette | rollback | 211 | 0 | 0 | 4 / 33 | 0.6 / 17 / 41 | 0 |
| update | beets | rollback | 15 | **1** | 0 | 3 / 5,018 | 1.3 / 5,759 / 5,017 | 0 |
| update | roquette | rollback | 7 | 0 | 14 | 7 / 9,940 | 0.5 / 1,235 / 2,536 | 0 |
| modify | beets | WAL | 197 | 0 | 0 | 2 / 934 | 0.5 / 3 / 895 | 0 |
| modify | roquette | WAL | 183 | 0 | 2 | 2 / 644 | 0.5 / 1 / 3 | 0 |
| mbsync | beets | WAL | 213 | 0 | 0 | 2 / 26 | 0.6 / 5 / 19 | 0 |
| mbsync | roquette | WAL | 213 | 0 | 0 | 2 / 11 | 0.5 / 1 / 3 | 0 |
| update | beets | WAL | 18 | **1** | 0 | 2 / 5,016 | 0.7 / 5,630 / 5,015 | 0 |
| update | roquette | WAL | 9 | 0 | 13 | 2 / 9,935 | 0.5 / 3 / 3 | 0 |

In every run, all of `beet`'s changes arrived, and no play count was
lost or counted twice ("lost" compares each item's play count with the
writes that succeeded).

- **No lost updates**: beets' `store()` writes only the fields that
  changed (`_dirty`), so `beet modify` storing `comments` doesn't touch
  a `play_count` roquette wrote meanwhile, even though it read its items
  long before. Only two writers of the same field could lose data, and
  roquette is the only writer of its fields.
- **Short transactions (modify, mbsync)** are harmless: a write waits
  at most about a second, while `beet modify` commits.
- **A long transaction (update)** holds the write lock for its whole
  length. With beets' defaults, a write waits the timeout (5 s) and
  fails, and **every read of that `Library` waits with it**: the waiting
  writer holds beets' lock, so the reader thread stalled for 5.8 s, in
  WAL mode too. A UI that waits for its reads would freeze.
- With **the write strategy** (a `Library` of its own for writes, a
  0.2 s timeout, retries while the lock is free), no write failed: they
  waited for the job (up to 9.9 s) without holding anything up. Reads
  then only wait for SQLite itself: up to 2.5 s in the rollback journal
  (while the job's changes outgrow its page cache, `beet` holds the
  exclusive lock), not at all in WAL mode (3 ms).
- **beets 2.14 hangs a `Library` when a commit fails.**
  `Transaction.__exit__` commits, and if that raises ("database is
  locked", when another connection is reading for longer than the
  timeout), the lock it took on entering is never released: the next
  query of that `Library`, from any thread, waits forever. The first
  version of the write strategy found it (a 0.1 s timeout hung the
  writer). `roquette/library/writes.py` works around it with a
  `Transaction` that always releases the lock and rolls back on errors;
  `tests/test_library_s3.py` checks that beets still has the bug, to
  notice when the workaround can go. To report upstream.
- **WAL mode** would be better for roquette's reads, but it's a
  property of the database file: it changes how `beet` and every other
  program use the user's library, and isn't roquette's to switch.
  Documented as an option for users.

### Opening the library read-only

A `Library` whose connections are opened with SQLite's `mode=ro` URI
opens a database that is at the installed beets' schema without writing
anything (10 ms; beets' `CREATE TABLE IF NOT EXISTS` and the recorded
migrations need no write), and fails with "attempt to write a readonly
database" when beets would migrate it, which is what M1's read-only
guard needs (S1 found that opening a library migrates it). A store
through it fails the same way, without hanging (the failure is in the
statement, not the commit). If the schema is older than the installed
beets, roquette has to ask the user to run any `beet` command first.

### Plugins

`just beets-plugins --all` loaded each of the 79 plugins beets 2.14.1
ships in a process of its own, with the optional dependencies of most
installed in a scratch environment and `replaygain` with the GStreamer
backend. 71 loaded; `absubmit`, `autobpm`, `beatport`, `bpsync`,
`discogs` and `titlecase` lacked a program, a dependency or
credentials, `listenbrainz` and `spotify` see below.

- **Loading registers, and nothing more, for all but three.** No plugin
  starts a thread, writes a file, or contacts the network when it is
  loaded, except:
  - `spotify` asks Spotify's server for a token while loading (it
    failed here behind the proxy, so the plugin didn't load);
  - `thumbnails` runs ImageMagick (`magick`, `convert`) to find its
    backend and creates `~/.cache/thumbnails/{normal,large}`, the cache
    roquette uses anyway;
  - `sonosupdate` runs `ldconfig` (through `ctypes.util.find_library`).
  `listenbrainz` refuses to load without `listenbrainz.token` in the
  config.
- **The events roquette sends** are `pluginload` (from
  `load_plugins()`) and `database_change` (from every `store()`).
  `pluginload` is used by `mbpseudo` and `advancedrewrite` to finish
  their setup. `database_change` is used by `smartplaylist`,
  `mpdupdate`, `plexupdate`, `kodiupdate`, `embyupdate`,
  `subsonicupdate` and `sonosupdate`, which note the change and act on
  `cli_exit`, which only `beet` sends: in roquette they cost a
  fraction of a millisecond per store (`smartplaylist` matches the item
  against its playlists' queries until each playlist is known to
  change). roquette never sends `library_opened` (only `loadext` uses
  it, to load SQLite extensions), `cli_exit` or the import events.
- **Memory is the cost.** Loading a plugin on its own takes 0.3 MiB
  for most, but 8–29 MiB for those that import beets' autotagger,
  `requests` and MusicBrainz' client: `fetchart` 29, `lyrics` 28,
  `musicbrainz`, `mbpseudo`, `albumtypes` 24, `chroma` 21, `deezer` 22,
  `replaygain` 15, `lastgenre` 11, `mbsync` 13. They share most of it,
  so what counts is the config's plugins together (the "all of them"
  row of `just beets-plugins`); to measure on the desktop.
- Types are declared by `acousticbrainz`, `deezer`, `lastimport`,
  `lyrics`, `metasync` (Amarok's fields), `mpdstats`, `tidal` and
  `types`. `mpdstats` declares `play_count`, `skip_count` and
  `last_played` with the same type instances as the roquette plugin,
  and `types` uses the same instances too, so they don't conflict
  (beets compares types by identity and refuses to start otherwise).

### `FMPS_RATING` through the roquette plugin

The plugin maps the `FMPS_RATING` tag to `fmps_rating` with a
`mediafile` field under the names Amarok writes (Vorbis comment
`FMPS_RATING`, ID3 `TXXX:FMPS_Rating`, MP4 `----:com.apple.iTunes:FMPS_Rating`,
ASF `FMPS/Rating`). Reading works for FLAC, MP3, Ogg Vorbis, Opus and
AAC in MP4 (tested with generated files). But with the mapping in
place, beets reads the field whenever it reads a file's tags (`beet
import`, `beet update` of a changed file), and a file without the tag
**sets the rating in beets to 0.0**, which is "no stars", not "not
rated". A rating made in roquette is replaced by the file's whenever a
file is re-read. And every `beet modify` or `beet write` that writes
tags then writes the rating into the files. So the mapping is **off by
default**, behind `roquette.rating_tag`; M9's one-time import reads the
tag with the mapping on (or with `mediafile` directly). Written back,
the value has two decimals ("0.30").

### Settings

The plugin's defaults (`roquette.library.writable`,
`roquette.rating_tag`) are part of `beets.config` once the plugin is
created, and the user's config overrides them as for any plugin
(tested). They exist only if the plugin is created, so roquette has to
add them itself when the user's config doesn't load the plugin, before
reading any setting.

### The `playlist` plugin's M3U files

The `playlist:` query is **broken in beets 2.14.1**: it makes the M3U's
paths absolute and compares them with the paths beets stores since 2.14,
relative to the music directory, so `beet ls playlist:name` finds
nothing, with relative paths in the playlist and with absolute ones.
Besides, it compares the first byte of each line with the string `"#"`,
which is never equal, so comment and `#EXTINF` lines aren't skipped
(harmless: they match no path); `playlist:name` only looks for
`name.m3u` in `playlist_dir`, not `.m3u8`; trailing whitespace of a
path is stripped. `tests/test_library_s3.py` notices when the query is
fixed. To report upstream. For M13: roquette writes `.m3u` files with
paths relative to `relative_to` (the music directory by default) so
that `playlist:` works once fixed, and reads them itself.

### Found along the way

- `just dev-library` copied the `playlist` plugin's playlists, but not
  the `smartplaylist` plugin's: with its `auto` (on by default), every
  `beet` command on the dev copy that changes the database would have
  rewritten the live smart playlists. Both are copied now.
- `Library(":memory:")` writes backups of the empty database into the
  current directory before its migrations (`:memory:-before-*.bak`);
  tests use a file in a temporary directory.
- `Item._types` is computed once, when it's first used: plugins must be
  loaded before anything parses a query or reads a typed flexible
  attribute.

### `just measure`

S3 doesn't change the window. `SCENARIOS="startup idle" just measure
femtovg software` in this container under Xvfb (scale 1, llvmpipe), with
the new fake library and, for comparison, with the one S2's code made
(same code, same container):

| variant | what | fake library | window created ms | library loaded ms | RSS MiB | PSS MiB | CPU % |
|---|---|---|---|---|---|---|---|
| femtovg | window, idle | S3's | 313.7 | 485.3 | 247.0 | 223.8 | 0.0 |
| femtovg | window, idle | S2's | 354.8 | 655.7 | 243.9 | 219.7 | 0.0 |
| software | floor | S3's | 288.4 | – | 83.8 | 78.5 | 0.0 |
| software | window, idle | S3's | 322.8 | 404.7 | 123.4 | 118.0 | 0.0 |
| software | window, idle | S2's | 338.3 | 418.5 | 122.9 | 117.4 | 0.0 |

The richer fake library costs the window 0.5–3 MiB (its larger
database file); the startup times vary by more than that between runs.
Against S2's record (in another container: 227.0 and 110.7 MiB RSS for
the idle window), both libraries are 12–20 MiB higher here, so that
difference is the container (this one has 4 cores, llvmpipe's threads
and arenas grow with them), not S3.

## Decision

- **beets stays the only backend.** Nothing S3 found needs a database
  of roquette's own: reading, querying and writing through beets work,
  with the workarounds below, and `beet` can run at the same time.
- **The library layer's structure (M1):**
  - *The resident index is columns of numbers*, read with narrow SQL:
    the tracks' ids, `album_id`, disc, track and length in the
    collection's order (`array`s), the releases likewise. Text (titles,
    artists, albums) is read by id for the rows on screen, through the
    lazy models, and kept only while they are. This replaces S1's tuples
    to stay within the ten-times budget. Other orders (a table sorted by
    a column) are a narrow `ORDER BY` returning ids.
  - *Releases and media are ranges* of the index (`grouping.py`), and
    *recordings with versions* a map from track id to its versions' ids,
    best first (`recordings.py`), loaded with one narrow query each.
  - *Queries go through beets' parser* (with the config's plugins
    loaded, so that their types and queries count) and run as SQL for
    ids; the index is filtered by those ids. Items are made only for
    what is shown in detail or changed, never for the whole library.
  - *Flexible attributes get SQL queries*: the roquette plugin provides
    query classes for its fields (`item_queries`) that run on
    `item_attributes` in SQL, keeping beets' meaning (an item without
    the field has the type's null value, so `play_count:..0` finds the
    unplayed). Queries beets can only run in Python (other plugins'
    flexible attributes) run on the reader worker in chunks of ids, so
    their Items never all exist at once; sorting by a flexible attribute
    likewise.
  - *Threads*: one worker thread for reads with its own `Library`, one
    for writes with the write library; no parallel reads. Each keeps its
    connection for the life of the process.
  - *Read-only by default*: without `roquette.library.writable`, the
    library is opened with `mode=ro` connections; a database beets would
    migrate is refused with a message to run `beet` first.
  - *Plugins*: the config's plugins are loaded before anything parses a
    query or reads a typed field; roquette sends no events of its own
    (`library_opened`, `cli_exit`).
- **The write strategy:** writes go through a `Library` of their own
  (`WriteLibrary`) whose transactions always release beets' lock and
  roll back on errors, with a 0.2 s SQLite timeout; a write that finds
  the database locked is tried again with growing pauses (up to 1 s)
  with the lock released, for as long as `beet` keeps it. One
  transaction per change, reading the item and writing its fields in it,
  through beets' `store()` (types and the `database_change` event as for
  any beets write). roquette doesn't change the database's journal mode;
  WAL is documented as an option for users who want it.
- **`FMPS_RATING`**: the roquette plugin maps the tag only if
  `roquette.rating_tag` is set (off by default), as the mapping turns
  ratings into the files' (or into 0.0) whenever beets reads tags.
- **Settings** are read through `beets.config` as planned; roquette adds
  the plugin's defaults itself when the plugin isn't loaded.

## Consequences

- M1 builds the layer as decided: the columnar index and the lazy
  models reading text by id (the Tracks module's model changes with it),
  queries as ids, the plugin's query classes for its fields, the two
  workers, the read-only opening, the write library from
  `roquette/library/writes.py` (with M9 as its first user), and a file
  watch on the database for outside changes.
- S2's `index.query_ids` (for `--play`) makes an Item for everything
  a query finds; M1 replaces it with ids from SQL.
- M9's import of the ratings reads `FMPS_RATING` with the mapping on, or
  with `mediafile` directly; the mapping stays an option for users who
  sync ratings to other players through tags, with its caveat
  documented.
- M13 reads and writes the M3U files itself: `.m3u` in `playlist_dir`,
  paths relative to the `playlist` plugin's `relative_to`, as beets
  expects once its query is fixed.
- The Statistics module (phase 5) and smart playlists rely on the
  plugin's SQL queries for ratings and play statistics.
- To report upstream (beets): `Transaction.__exit__` keeping its lock
  when a commit fails; the `playlist:` query comparing absolute with
  relative paths and not skipping comments; backups of `:memory:`
  databases in the current directory; queries on typed flexible
  attributes evaluated in Python.
- The plan's budgets stand. The library's index costs about 2.4 MiB at
  the library's size and 5 MiB at ten times, as columns, against 9 and
  74 MiB as S1's tuples. The memory of the configured plugins (8–29 MiB
  each on their own, much of it shared) is part of the idle budget and
  to be measured on the desktop.
- Open issues:
  - The dev library's figures (see below).
  - The memory of the config's plugins together, and whether some are
    worth not loading (a `roquette.plugins` setting) if they're heavy
    and roquette needs nothing from them.
  - Whether the reader worker should yield between chunks of a long
    query so that a short one (incremental search) isn't kept waiting.
  - `smartplaylist` registers a `cli_exit` listener per changed
    playlist in roquette's process, which is never called; harmless.

## Still to do on the desktop

On the dev copy (`just dev-library` first if it's older than the
`smartplaylist` fix), each run printing a Markdown summary to add here:

- `just measure-library` (about 5 minutes);
- `just library-locking` and `just library-locking --wal` (each about
  2 minutes; they work on a copy in build/locking/);
- `just beets-plugins` (the config's plugins one by one and together);
- optionally a real `BEETSDIR=dev/beets beet mbsync` (it needs
  MusicBrainz) while roquette plays, for the locking behaviour with the
  real plugin.
